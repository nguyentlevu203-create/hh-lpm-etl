#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Send approved CEO Control Tower HTML report by Gmail SMTP with audit log."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import smtplib
import sys
from datetime import datetime
from email.message import EmailMessage
from email.utils import make_msgid
from pathlib import Path
from typing import Optional

import psycopg2
from psycopg2.extras import RealDictCursor, Json

REPORT_TYPE = "CEO_DAILY_CONTROL_TOWER"


def env_first(*names: str, default: str = "") -> str:
    for name in names:
        value = os.getenv(name)
        if value:
            return value
    return default


def connect(cursor_factory=None):
    return psycopg2.connect(
        host=env_first("PGHOST", "ETL_DB_HOST", "DB_HOST", default="localhost"),
        port=int(env_first("PGPORT", "ETL_DB_PORT", "DB_PORT", default="5433")),
        dbname=env_first("PGDATABASE", "ETL_DB_NAME", "DB_NAME", default="DuLieu"),
        user=env_first("PGUSER", "ETL_DB_USER", "DB_USER", default="postgres"),
        password=env_first("PGPASSWORD", "ETL_DB_PASSWORD", "DB_PASSWORD", default=""),
        cursor_factory=cursor_factory,
    )


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def normalize_recipients(value: str) -> str:
    if not value:
        return ""
    return ", ".join(x.strip() for x in re.split(r"[;,]", value) if x.strip())


def split_recipients(value: str) -> list[str]:
    if not value:
        return []
    return [x.strip() for x in re.split(r"[;,]", value) if x.strip()]


def sanitize_html_for_email(raw_html: str) -> str:
    cleaned = re.sub(r"<script\b[^<]*(?:(?!</script>)<[^<]*)*</script>", "", raw_html, flags=re.I | re.S)
    cleaned = re.sub(r"\son\w+\s*=\s*(['\"]).*?\1", "", cleaned, flags=re.I | re.S)
    cleaned = re.sub(r"\son\w+\s*=\s*[^\s>]+", "", cleaned, flags=re.I)
    return cleaned


def make_text_fallback(report_date: str, subject: str) -> str:
    return (
        f"{subject}\n\n"
        f"Bao cao CEO Control Tower ngay {report_date} duoc gui duoi dang HTML.\n"
        "Neu email client khong hien thi dung, vui long mo file HTML dinh kem.\n"
    )


def build_message(
    smtp_user: str,
    to: str,
    cc: str,
    bcc: str,
    subject: str,
    html_body: str,
    text_fallback: str,
    attach_html: bool,
    html_path: Path,
    message_id: Optional[str] = None,
) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = smtp_user
    msg["To"] = normalize_recipients(to)
    if cc:
        msg["Cc"] = normalize_recipients(cc)
    msg["Subject"] = subject
    msg["Message-ID"] = message_id or make_msgid(domain="hhdistribution.vn")
    msg["X-HH-Report-Type"] = REPORT_TYPE
    msg.set_content(text_fallback)
    msg.add_alternative(html_body, subtype="html")
    if attach_html:
        msg.add_attachment(
            html_body.encode("utf-8"),
            maintype="text",
            subtype="html",
            filename=html_path.name,
        )
    return msg


def write_eml(msg: EmailMessage, out_dir: Path, report_date: str) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    safe_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = out_dir / f"ceo_control_tower_email_{report_date}_{safe_ts}.eml"
    path.write_bytes(bytes(msg))
    return path


def load_package_checksum(package_path: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    if not package_path:
        return None, None
    path = Path(package_path)
    if not path.exists():
        return str(path), None
    return str(path), sha256_bytes(path.read_bytes())


def make_idempotency_key(report_date: str, html_checksum: str, to: str, cc: str, bcc: str) -> str:
    normalized = "|".join([
        REPORT_TYPE,
        report_date,
        html_checksum,
        normalize_recipients(to).lower(),
        normalize_recipients(cc).lower(),
        normalize_recipients(bcc).lower(),
    ])
    return f"{REPORT_TYPE}:{report_date}:{hashlib.sha256(normalized.encode('utf-8')).hexdigest()}"


def insert_or_skip_log(
    report_date: str,
    subject: str,
    to: str,
    cc: str,
    bcc: str,
    html_path: str,
    html_checksum: str,
    html_size_bytes: int,
    package_path: Optional[str],
    package_checksum: Optional[str],
    idempotency_key: str,
    dry_run: bool,
    force_send: bool,
    approved_by: str,
    smtp_user: str,
) -> tuple[str, Optional[int]]:
    conn = connect(cursor_factory=RealDictCursor)
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, sent_status FROM control_tower.sent_report_logs WHERE idempotency_key = %s",
                    (idempotency_key,),
                )
                existing = cur.fetchone()
                if existing and existing["sent_status"] == "SENT" and not force_send:
                    return "SKIPPED_DUPLICATE", int(existing["id"])

                status = "DRY_RUN" if dry_run else "PENDING"
                idempotency_key_to_insert = idempotency_key
                if existing and force_send:
                    idempotency_key_to_insert = f"{idempotency_key}:force:{datetime.now().strftime('%Y%m%d%H%M%S')}"

                cur.execute(
                    """
                    INSERT INTO control_tower.sent_report_logs (
                        report_date, report_type, subject, recipient_to, recipient_cc, recipient_bcc,
                        html_path, html_checksum, html_size_bytes, package_path, package_checksum,
                        idempotency_key, sent_status, dry_run, force_send, sent_by, smtp_user, metadata
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s, %s
                    )
                    RETURNING id
                    """,
                    (
                        report_date, REPORT_TYPE, subject, normalize_recipients(to),
                        normalize_recipients(cc), normalize_recipients(bcc),
                        html_path, html_checksum, html_size_bytes, package_path, package_checksum,
                        idempotency_key_to_insert, status, dry_run, force_send, approved_by, smtp_user,
                        Json({"approved_by": approved_by, "script": "send_ceo_control_tower_report.py"}),
                    ),
                )
                return status, int(cur.fetchone()["id"])
    finally:
        conn.close()


def update_log_status(log_id: int, status: str, gmail_message_id: Optional[str] = None, error: Optional[str] = None) -> None:
    conn = connect(cursor_factory=RealDictCursor)
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE control_tower.sent_report_logs
                    SET sent_status = %s,
                        gmail_message_id = COALESCE(%s, gmail_message_id),
                        error_message = %s,
                        sent_at = CASE WHEN %s = 'SENT' THEN now() ELSE sent_at END,
                        updated_at = now()
                    WHERE id = %s
                    """,
                    (status, gmail_message_id, error, status, log_id),
                )
    finally:
        conn.close()


def send_smtp(msg: EmailMessage, smtp_user: str, smtp_password: str, to: str, cc: str, bcc: str) -> None:
    recipients = split_recipients(to) + split_recipients(cc) + split_recipients(bcc)
    if not recipients:
        raise ValueError("No recipients provided.")
    smtp_host = env_first("GMAIL_SMTP_HOST", "SMTP_HOST", default="smtp.gmail.com")
    smtp_port = int(env_first("GMAIL_SMTP_PORT", "SMTP_PORT", default="587"))
    with smtplib.SMTP(smtp_host, smtp_port, timeout=60) as server:
        server.ehlo()
        server.starttls()
        server.ehlo()
        server.login(smtp_user, smtp_password)
        server.send_message(msg, from_addr=smtp_user, to_addrs=recipients)


def main() -> int:
    ap = argparse.ArgumentParser(description="Send approved CEO Control Tower HTML report by Gmail SMTP with audit log.")
    ap.add_argument("--date", required=True, help="Report date YYYY-MM-DD")
    ap.add_argument("--html", required=True, help="Path to approved HTML file")
    ap.add_argument("--to", required=True, help="To recipients, comma/semicolon separated")
    ap.add_argument("--cc", default="", help="Cc recipients, comma/semicolon separated")
    ap.add_argument("--bcc", default="", help="Bcc recipients, comma/semicolon separated")
    ap.add_argument("--subject", required=True, help="Email subject")
    ap.add_argument("--package", default="", help="Optional package JSON path for checksum audit")
    ap.add_argument("--approved-by", default=os.getenv("USERNAME") or os.getenv("USER") or "operator")
    ap.add_argument("--dry-run", action="store_true", help="Do not send. Write .eml preview and audit DRY_RUN.")
    ap.add_argument("--force", action="store_true", help="Force resend even if same checksum was already sent.")
    ap.add_argument("--no-attach-html", action="store_true", help="Do not attach HTML file; send inline only.")
    ap.add_argument("--eml-out-dir", default="outbox/email_drafts")
    args = ap.parse_args()

    html_path = Path(args.html)
    if not html_path.exists():
        raise SystemExit(f"HTML file not found: {html_path}")

    raw_html = html_path.read_bytes().decode("utf-8", errors="replace")
    email_html = sanitize_html_for_email(raw_html)
    html_checksum = sha256_bytes(email_html.encode("utf-8"))
    html_size_bytes = len(email_html.encode("utf-8"))

    package_path, package_checksum = load_package_checksum(args.package or None)

    smtp_user = env_first("GMAIL_SMTP_USER", "SMTP_USER", default="")
    smtp_password = env_first("GMAIL_SMTP_APP_PASSWORD", "SMTP_PASSWORD", default="")
    if not smtp_user:
        raise SystemExit("Missing GMAIL_SMTP_USER env var.")
    if not smtp_password and not args.dry_run:
        raise SystemExit("Missing GMAIL_SMTP_APP_PASSWORD env var. Use Gmail App Password, not normal password.")

    idempotency_key = make_idempotency_key(args.date, html_checksum, args.to, args.cc, args.bcc)
    message_id = make_msgid(domain="hhdistribution.vn")
    msg = build_message(
        smtp_user=smtp_user,
        to=args.to,
        cc=args.cc,
        bcc=args.bcc,
        subject=args.subject,
        html_body=email_html,
        text_fallback=make_text_fallback(args.date, args.subject),
        attach_html=not args.no_attach_html,
        html_path=html_path,
        message_id=message_id,
    )

    status, log_id = insert_or_skip_log(
        report_date=args.date,
        subject=args.subject,
        to=args.to,
        cc=args.cc,
        bcc=args.bcc,
        html_path=str(html_path),
        html_checksum=html_checksum,
        html_size_bytes=html_size_bytes,
        package_path=package_path,
        package_checksum=package_checksum,
        idempotency_key=idempotency_key,
        dry_run=args.dry_run,
        force_send=args.force,
        approved_by=args.approved_by,
        smtp_user=smtp_user,
    )

    if status == "SKIPPED_DUPLICATE":
        print(json.dumps({
            "status": "skipped_duplicate",
            "message": "Report already sent with same date/checksum/recipients. Use --force to resend.",
            "log_id": log_id,
            "idempotency_key": idempotency_key,
        }, ensure_ascii=False, indent=2))
        return 0

    eml_path = write_eml(msg, Path(args.eml_out_dir), args.date)

    if args.dry_run:
        print(json.dumps({
            "status": "dry_run",
            "log_id": log_id,
            "eml_path": str(eml_path),
            "html_checksum": html_checksum,
            "html_size_bytes": html_size_bytes,
            "message": "Dry-run only. Email was not sent.",
        }, ensure_ascii=False, indent=2))
        return 0

    try:
        update_log_status(log_id, "SENDING")
        send_smtp(msg, smtp_user, smtp_password, args.to, args.cc, args.bcc)
        update_log_status(log_id, "SENT", gmail_message_id=message_id)
        print(json.dumps({
            "status": "sent",
            "log_id": log_id,
            "gmail_message_id": message_id,
            "html_checksum": html_checksum,
            "html_size_bytes": html_size_bytes,
            "eml_path": str(eml_path),
        }, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        update_log_status(log_id, "FAILED", error=str(exc))
        print(json.dumps({
            "status": "failed",
            "log_id": log_id,
            "error": str(exc),
            "eml_path": str(eml_path),
        }, ensure_ascii=False, indent=2), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
