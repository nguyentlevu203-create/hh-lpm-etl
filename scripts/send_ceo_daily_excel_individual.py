#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Send one CEO Daily Excel report as separate Gmail messages to each recipient.

Safety/production contract:
- Each enabled recipient gets a distinct email; no shared To/Cc/Bcc list.
- Gmail credentials come from environment variables only.
- The Excel attachment is validated before sending.
- Optional package JSON gate blocks send when data_quality.can_send is false.
- Local JSONL audit log prevents accidental duplicate sends unless --force is used.
- --dry-run writes one .eml preview per recipient and sends nothing.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import os
import re
import smtplib
import ssl
import sys
import time
import zipfile
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from xml.etree import ElementTree as ET


REPORT_TYPE = "CEO_DAILY_PNL_EXCEL_INDIVIDUAL"
EXPECTED_SHEETS = [
    "00_Tong_quan",
    "01_GT",
    "02_MT",
    "03_Nhanh",
    "04_Shopee",
    "05_TikTok",
    "06_Ton_kho_Can_date",
]
MAX_ATTACHMENT_BYTES = 18 * 1024 * 1024  # conservative Gmail/MIME safety limit
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class SendError(RuntimeError):
    pass


def env_first(*names: str, default: str = "") -> str:
    for name in names:
        value = os.getenv(name)
        if value:
            return value
    return default


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def parse_bool(value: Any) -> bool:
    return str(value or "").strip().casefold() in {"1", "true", "yes", "y", "on", "x"}


def vi_date(report_date: str) -> str:
    try:
        return datetime.strptime(report_date, "%Y-%m-%d").strftime("%d/%m/%Y")
    except ValueError as exc:
        raise SendError("--date must be YYYY-MM-DD") from exc


def safe_file_part(value: str) -> str:
    out = re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip())
    return out.strip("._") or "recipient"


def fmt_money(value: Any) -> str:
    if value is None:
        return ""
    try:
        return f"{float(value):,.0f}"
    except Exception:
        return ""


def fmt_pct_ratio(value: Any) -> str:
    """Package ratios are stored as 0.3024 for 30.24%."""
    if value is None:
        return ""
    try:
        return f"{float(value) * 100:.1f}%"
    except Exception:
        return ""


def read_sheet_names_from_xlsx(path: Path) -> List[str]:
    with zipfile.ZipFile(path, "r") as zf:
        try:
            xml_bytes = zf.read("xl/workbook.xml")
        except KeyError as exc:
            raise SendError("Invalid .xlsx: missing xl/workbook.xml") from exc
    root = ET.fromstring(xml_bytes)
    ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    return [node.attrib.get("name", "") for node in root.findall(".//m:sheets/m:sheet", ns)]


def validate_xlsx(path: Path) -> Dict[str, Any]:
    if not path.exists():
        raise SendError(f"Excel file not found: {path}")
    if path.suffix.lower() != ".xlsx":
        raise SendError(f"Expected .xlsx file, got: {path.suffix}")
    size = path.stat().st_size
    if size <= 0:
        raise SendError("Excel file is empty.")
    if size > MAX_ATTACHMENT_BYTES:
        raise SendError(
            f"Excel file is {size / 1024 / 1024:.1f} MB; exceeds safe Gmail attachment limit "
            f"of {MAX_ATTACHMENT_BYTES / 1024 / 1024:.0f} MB."
        )
    if not zipfile.is_zipfile(path):
        raise SendError("Excel file is not a valid XLSX/ZIP container.")

    with zipfile.ZipFile(path, "r") as zf:
        bad_entry = zf.testzip()
        if bad_entry:
            raise SendError(f"Corrupt XLSX entry: {bad_entry}")

        total_uncompressed = sum(info.file_size for info in zf.infolist())
        if total_uncompressed > 120 * 1024 * 1024:
            raise SendError("Workbook uncompressed size is unexpectedly large; refusing to send.")

        # Search raw workbook XML/formulas/charts/rels without mutating the workbook.
        refs_99_data: List[str] = []
        ebitda_hits: List[str] = []
        for info in zf.infolist():
            name = info.filename
            if not (name.endswith(".xml") or name.endswith(".rels")):
                continue
            text = zf.read(name).decode("utf-8", errors="ignore")
            folded = text.casefold()
            if "99_data" in folded:
                refs_99_data.append(name)
            if "ebitda" in folded:
                ebitda_hits.append(name)

    sheet_names = read_sheet_names_from_xlsx(path)
    if sheet_names != EXPECTED_SHEETS:
        raise SendError(
            "Workbook sheet contract failed.\n"
            f"Expected: {EXPECTED_SHEETS}\n"
            f"Actual:   {sheet_names}"
        )
    if refs_99_data:
        raise SendError(f"Workbook still references 99_Data in: {sorted(set(refs_99_data))}")
    if ebitda_hits:
        raise SendError(f"EBITDA is forbidden; found in workbook XML: {sorted(set(ebitda_hits))}")

    return {
        "path": str(path),
        "size_bytes": size,
        "sha256": sha256_bytes(path.read_bytes()),
        "sheet_names": sheet_names,
        "refs_99_data": 0,
        "ebitda_hits": 0,
    }


def load_package(path: Optional[Path], report_date: str) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
    if not path:
        return None, {"provided": False}
    if not path.exists():
        raise SendError(f"Package JSON not found: {path}")
    try:
        package = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise SendError(f"Cannot parse package JSON: {path}") from exc

    metadata = package.get("metadata") or {}
    package_date = (
        metadata.get("report_date")
        or package.get("report_date")
        or (package.get("reporting_period") or {}).get("report_date")
    )
    if package_date and str(package_date) != report_date:
        raise SendError(
            f"Package report date mismatch: package={package_date}, requested={report_date}"
        )

    dq = package.get("data_quality") or {}
    if dq.get("can_send") is False:
        errors = dq.get("errors") or []
        raise SendError(
            "Package data_quality.can_send=false. Email send is blocked. "
            + ("Errors: " + " | ".join(str(x) for x in errors[:10]) if errors else "")
        )

    return package, {
        "provided": True,
        "path": str(path),
        "sha256": sha256_bytes(path.read_bytes()),
        "package_version": metadata.get("package_version") or metadata.get("version") or package.get("package_version"),
        "data_quality_status": dq.get("status"),
        "can_send": dq.get("can_send"),
    }


def load_recipients(path: Path, only_emails: Iterable[str]) -> List[Dict[str, str]]:
    if not path.exists():
        raise SendError(f"Recipients CSV not found: {path}")

    only = {x.strip().casefold() for x in only_emails if x.strip()}
    rows: List[Dict[str, str]] = []
    seen: set[str] = set()

    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        required = {"name", "email", "enabled"}
        fields = {str(x or "").strip() for x in (reader.fieldnames or [])}
        missing = required - fields
        if missing:
            raise SendError(f"Recipients CSV missing columns: {sorted(missing)}")

        for idx, raw in enumerate(reader, start=2):
            name = str(raw.get("name") or "").strip()
            email_addr = str(raw.get("email") or "").strip()
            enabled = parse_bool(raw.get("enabled"))
            if not enabled:
                continue
            if not name:
                raise SendError(f"Recipients CSV row {idx}: name is blank.")
            if not EMAIL_RE.match(email_addr):
                raise SendError(f"Recipients CSV row {idx}: invalid email {email_addr!r}.")
            if "," in email_addr or ";" in email_addr:
                raise SendError(
                    f"Recipients CSV row {idx}: each row must contain exactly one email address."
                )
            key = email_addr.casefold()
            if key in seen:
                raise SendError(f"Duplicate recipient email in CSV: {email_addr}")
            seen.add(key)
            if only and key not in only:
                continue
            rows.append({"name": name, "email": email_addr})

    if only:
        found = {r["email"].casefold() for r in rows}
        missing_only = sorted(only - found)
        if missing_only:
            raise SendError(f"--only-email not found/enabled in recipients CSV: {missing_only}")

    if not rows:
        raise SendError("No enabled recipients selected.")
    return rows


def package_headline(package: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not package:
        return {}
    target = package.get("target_progress") or {}
    total = target.get("total") or {}
    dq = package.get("data_quality") or {}
    return {
        "mtd_net_sales": total.get("mtd_net_sales"),
        "monthly_target_net_sales": total.get("monthly_target_net_sales"),
        "achievement_mtd_pct": total.get("achievement_mtd_pct"),
        "remaining_target_net_sales": total.get("remaining_target_net_sales"),
        "forecast_month_end_net_sales": total.get("forecast_month_end_net_sales"),
        "forecast_achievement_pct": total.get("forecast_achievement_pct"),
        "dq_status": dq.get("status"),
    }


def make_text_body(
    recipient_name: str,
    report_date: str,
    xlsx_filename: str,
    headline: Dict[str, Any],
) -> str:
    lines = [
        f"Kính gửi {recipient_name},",
        "",
        f"Em gửi Báo cáo CEO Daily Control Tower ngày {vi_date(report_date)}.",
    ]
    if headline:
        lines.extend([
            "",
            f"Doanh số lũy kế: {fmt_money(headline.get('mtd_net_sales'))}",
            f"Chỉ tiêu tháng: {fmt_money(headline.get('monthly_target_net_sales'))}",
            f"% thực hiện: {fmt_pct_ratio(headline.get('achievement_mtd_pct'))}",
            f"Còn thiếu: {fmt_money(headline.get('remaining_target_net_sales'))}",
            f"Dự báo cuối tháng: {fmt_money(headline.get('forecast_month_end_net_sales'))}",
        ])
    lines.extend([
        "",
        f"File Excel chi tiết được đính kèm: {xlsx_filename}",
        "",
        "Trân trọng,",
        "HOÀNG HÀ DISTRIBUTION",
    ])
    return "\n".join(lines)


def kpi_card(label: str, value: str) -> str:
    if not value:
        value = "-"
    return f"""
      <td width="20%" valign="top" style="padding:6px;">
        <div style="border:1px solid #d8e1ec;border-radius:8px;padding:12px;background:#f8fafc;">
          <div style="font-size:11px;color:#64748b;text-transform:uppercase;font-weight:700;">{html.escape(label)}</div>
          <div style="font-size:17px;color:#152238;font-weight:700;margin-top:5px;">{html.escape(value)}</div>
        </div>
      </td>
    """


def make_html_body(
    recipient_name: str,
    report_date: str,
    subject: str,
    xlsx_filename: str,
    headline: Dict[str, Any],
) -> str:
    safe_name = html.escape(recipient_name)
    safe_date = html.escape(vi_date(report_date))
    safe_subject = html.escape(subject)
    safe_file = html.escape(xlsx_filename)

    kpis = ""
    if headline:
        kpis = f"""
        <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="margin:8px 0 14px 0;">
          <tr>
            {kpi_card("Doanh số lũy kế", fmt_money(headline.get("mtd_net_sales")))}
            {kpi_card("Chỉ tiêu tháng", fmt_money(headline.get("monthly_target_net_sales")))}
            {kpi_card("% thực hiện", fmt_pct_ratio(headline.get("achievement_mtd_pct")))}
            {kpi_card("Còn thiếu", fmt_money(headline.get("remaining_target_net_sales")))}
            {kpi_card("Dự báo cuối tháng", fmt_money(headline.get("forecast_month_end_net_sales")))}
          </tr>
        </table>
        """

    dq_line = ""
    dq_status = str(headline.get("dq_status") or "").strip() if headline else ""
    if dq_status:
        dq_color = "#a16207" if dq_status.upper() == "WARNING" else "#166534"
        dq_bg = "#fef9c3" if dq_status.upper() == "WARNING" else "#dcfce7"
        dq_line = (
            f'<p style="margin:12px 0 0 0;padding:8px 10px;border-radius:6px;'
            f'background:{dq_bg};color:{dq_color};font-size:12px;">'
            f'Data Quality: <b>{html.escape(dq_status)}</b></p>'
        )

    return f"""<!doctype html>
<html lang="vi">
<head><meta charset="utf-8"><title>{safe_subject}</title></head>
<body style="margin:0;padding:0;background:#f4f6f8;font-family:Arial,Helvetica,sans-serif;color:#172033;">
  <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#f4f6f8;">
    <tr>
      <td align="center" style="padding:24px 12px;">
        <table role="presentation" width="720" cellspacing="0" cellpadding="0"
               style="max-width:720px;background:#ffffff;border:1px solid #d8e1ec;border-radius:10px;">
          <tr>
            <td style="background:#152238;color:#ffffff;padding:22px 24px;border-radius:10px 10px 0 0;">
              <div style="font-size:12px;letter-spacing:.5px;color:#cbd5e1;font-weight:700;">HOÀNG HÀ DISTRIBUTION</div>
              <h1 style="margin:5px 0 0 0;font-size:22px;line-height:1.3;">CEO DAILY CONTROL TOWER</h1>
              <p style="margin:8px 0 0 0;color:#d8e2f0;font-size:14px;">Ngày báo cáo: {safe_date}</p>
            </td>
          </tr>
          <tr>
            <td style="padding:22px 24px;font-size:14px;line-height:1.55;">
              <p style="margin:0 0 12px 0;">Kính gửi <b>{safe_name}</b>,</p>
              <p style="margin:0 0 12px 0;">Em gửi Báo cáo CEO Daily Control Tower ngày <b>{safe_date}</b>.</p>
              {kpis}
              <p style="margin:12px 0 0 0;"><b>File Excel chi tiết:</b> {safe_file}</p>
              {dq_line}
              <p style="margin:18px 0 0 0;color:#64748b;">Trân trọng,<br><b>HOÀNG HÀ DISTRIBUTION</b></p>
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>"""


def build_message(
    sender_email: str,
    sender_name: str,
    recipient: Dict[str, str],
    subject: str,
    report_date: str,
    xlsx_path: Path,
    xlsx_bytes: bytes,
    headline: Dict[str, Any],
) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = formataddr((sender_name, sender_email))
    msg["To"] = recipient["email"]  # exactly one visible recipient
    msg["Subject"] = subject
    msg["Message-ID"] = make_msgid(domain="hhdistribution.vn")
    msg["X-HH-Report-Type"] = REPORT_TYPE
    msg["X-HH-Report-Date"] = report_date

    text_body = make_text_body(recipient["name"], report_date, xlsx_path.name, headline)
    html_body = make_html_body(recipient["name"], report_date, subject, xlsx_path.name, headline)
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")
    msg.add_attachment(
        xlsx_bytes,
        maintype="application",
        subtype="vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=xlsx_path.name,
    )
    return msg


def audit_key(report_date: str, xlsx_sha256: str, recipient_email: str) -> str:
    raw = "|".join([
        REPORT_TYPE,
        report_date,
        xlsx_sha256,
        recipient_email.strip().casefold(),
    ])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def load_sent_keys(log_path: Path) -> set[str]:
    keys: set[str] = set()
    if not log_path.exists():
        return keys
    with log_path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            if row.get("status") == "SENT" and row.get("audit_key"):
                keys.add(str(row["audit_key"]))
    return keys


def append_log(log_path: Path, row: Dict[str, Any]) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        **row,
    }
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, ensure_ascii=False) + "\n")


def write_eml(msg: EmailMessage, out_dir: Path, report_date: str, email_addr: str) -> Path:
    target_dir = out_dir / report_date
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / f"{safe_file_part(email_addr)}.eml"
    path.write_bytes(bytes(msg))
    return path


def connect_smtp() -> Tuple[smtplib.SMTP, str]:
    host = env_first("GMAIL_SMTP_HOST", "SMTP_HOST", default="smtp.gmail.com")
    port = int(env_first("GMAIL_SMTP_PORT", "SMTP_PORT", default="587"))
    user = env_first("GMAIL_SMTP_USER", "SMTP_USER", default="")
    password = env_first("GMAIL_SMTP_APP_PASSWORD", "SMTP_PASSWORD", default="")
    if not user:
        raise SendError("Missing GMAIL_SMTP_USER (or SMTP_USER) environment variable.")
    if not password:
        raise SendError(
            "Missing GMAIL_SMTP_APP_PASSWORD (or SMTP_PASSWORD). "
            "Use a Gmail App Password; do not hard-code it in this script."
        )

    server = smtplib.SMTP(host, port, timeout=60)
    server.ehlo()
    server.starttls(context=ssl.create_default_context())
    server.ehlo()
    server.login(user, password)
    return server, user


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Send one CEO Daily Excel report as a separate Gmail message to each enabled recipient."
    )
    ap.add_argument("--date", required=True, help="Report date YYYY-MM-DD")
    ap.add_argument("--xlsx", required=True, help="Approved CEO Daily .xlsx file")
    ap.add_argument("--recipients", required=True, help="CSV with columns: name,email,enabled")
    ap.add_argument("--package", default="", help="Optional validated CEO package JSON for DQ gate and KPI summary")
    ap.add_argument("--subject", default="", help="Optional subject. Default: [CEO DAILY] Báo cáo ngày DD/MM/YYYY")
    ap.add_argument("--from-name", default=env_first("GMAIL_FROM_NAME", "SMTP_FROM_NAME", default="HOÀNG HÀ DISTRIBUTION"))
    ap.add_argument("--dry-run", action="store_true", help="Write one .eml per recipient; send nothing")
    ap.add_argument("--validate-only", action="store_true", help="Validate workbook/recipients/package and exit")
    ap.add_argument("--force", action="store_true", help="Resend even if same report file/date was already SENT to recipient")
    ap.add_argument("--only-email", action="append", default=[], help="Send/preview only this email; may be repeated")
    ap.add_argument("--delay-seconds", type=float, default=1.0, help="Delay between individual sends; default 1 second")
    ap.add_argument("--eml-out-dir", default="outbox/email_drafts/ceo_daily_individual")
    ap.add_argument("--log-path", default="outbox/email_logs/ceo_daily_individual.jsonl")
    args = ap.parse_args()

    report_date = args.date
    display_date = vi_date(report_date)
    xlsx_path = Path(args.xlsx)
    recipients_path = Path(args.recipients)
    package_path = Path(args.package) if args.package else None
    log_path = Path(args.log_path)

    workbook_meta = validate_xlsx(xlsx_path)
    package, package_meta = load_package(package_path, report_date)
    recipients = load_recipients(recipients_path, args.only_email)
    headline = package_headline(package)
    subject = args.subject.strip() or f"[CEO DAILY] Báo cáo ngày {display_date}"

    print(json.dumps({
        "validation": "PASS",
        "report_date": report_date,
        "xlsx": workbook_meta,
        "package": package_meta,
        "recipient_count": len(recipients),
        "recipients": [r["email"] for r in recipients],
        "individual_delivery": True,
    }, ensure_ascii=False, indent=2))

    if args.validate_only:
        print("VALIDATE ONLY: no email was sent.")
        return 0

    xlsx_bytes = xlsx_path.read_bytes()
    sent_keys = load_sent_keys(log_path)
    dry_sender = env_first("GMAIL_SMTP_USER", "SMTP_USER", default="dry-run@localhost")
    sender_name = args.from_name

    smtp: Optional[smtplib.SMTP] = None
    sender_email = dry_sender
    if not args.dry_run:
        smtp, sender_email = connect_smtp()

    sent_count = 0
    skipped_count = 0
    failed_count = 0

    try:
        for index, recipient in enumerate(recipients, start=1):
            key = audit_key(report_date, workbook_meta["sha256"], recipient["email"])
            if key in sent_keys and not args.force:
                skipped_count += 1
                print(f"SKIP DUPLICATE {recipient['email']} ({index}/{len(recipients)})")
                append_log(log_path, {
                    "status": "SKIPPED_DUPLICATE",
                    "audit_key": key,
                    "report_date": report_date,
                    "recipient_name": recipient["name"],
                    "recipient_email": recipient["email"],
                    "xlsx_path": str(xlsx_path),
                    "xlsx_sha256": workbook_meta["sha256"],
                })
                continue

            msg = build_message(
                sender_email=sender_email,
                sender_name=sender_name,
                recipient=recipient,
                subject=subject,
                report_date=report_date,
                xlsx_path=xlsx_path,
                xlsx_bytes=xlsx_bytes,
                headline=headline,
            )

            if args.dry_run:
                eml_path = write_eml(msg, Path(args.eml_out_dir), report_date, recipient["email"])
                print(f"DRY RUN {recipient['email']} -> {eml_path}")
                append_log(log_path, {
                    "status": "DRY_RUN",
                    "audit_key": key,
                    "report_date": report_date,
                    "recipient_name": recipient["name"],
                    "recipient_email": recipient["email"],
                    "subject": subject,
                    "message_id": msg["Message-ID"],
                    "xlsx_path": str(xlsx_path),
                    "xlsx_sha256": workbook_meta["sha256"],
                    "package_sha256": package_meta.get("sha256"),
                    "eml_path": str(eml_path),
                })
                sent_count += 1
                continue

            assert smtp is not None
            try:
                smtp.send_message(
                    msg,
                    from_addr=sender_email,
                    to_addrs=[recipient["email"]],  # one recipient only
                )
                sent_count += 1
                sent_keys.add(key)
                print(f"SENT {recipient['email']} ({index}/{len(recipients)})")
                append_log(log_path, {
                    "status": "SENT",
                    "audit_key": key,
                    "report_date": report_date,
                    "recipient_name": recipient["name"],
                    "recipient_email": recipient["email"],
                    "subject": subject,
                    "message_id": msg["Message-ID"],
                    "xlsx_path": str(xlsx_path),
                    "xlsx_sha256": workbook_meta["sha256"],
                    "package_sha256": package_meta.get("sha256"),
                })
            except Exception as exc:
                failed_count += 1
                print(f"FAILED {recipient['email']}: {exc}", file=sys.stderr)
                append_log(log_path, {
                    "status": "FAILED",
                    "audit_key": key,
                    "report_date": report_date,
                    "recipient_name": recipient["name"],
                    "recipient_email": recipient["email"],
                    "subject": subject,
                    "message_id": msg["Message-ID"],
                    "xlsx_path": str(xlsx_path),
                    "xlsx_sha256": workbook_meta["sha256"],
                    "package_sha256": package_meta.get("sha256"),
                    "error": str(exc)[:2000],
                })

            if index < len(recipients) and args.delay_seconds > 0:
                time.sleep(args.delay_seconds)
    finally:
        if smtp is not None:
            try:
                smtp.quit()
            except Exception:
                smtp.close()

    print(json.dumps({
        "status": "DONE" if failed_count == 0 else "DONE_WITH_ERRORS",
        "report_date": report_date,
        "individual_delivery": True,
        "sent_or_previewed": sent_count,
        "skipped_duplicate": skipped_count,
        "failed": failed_count,
        "audit_log": str(log_path),
    }, ensure_ascii=False, indent=2))

    return 1 if failed_count else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SendError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
