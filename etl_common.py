#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Common production ETL utilities for DuLieu database.

All ETL scripts in this project share exactly the same parsing rules,
database config, batch audit and error logging.  The database DDL is assumed
already installed from du_lieu_production_schema_v2.sql.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import unicodedata
from decimal import Decimal, InvalidOperation
from contextlib import contextmanager
from datetime import datetime, date
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import pandas as pd
import psycopg2
from psycopg2.extras import RealDictCursor, Json

DB_CONFIG = {
    "dbname": os.getenv("ETL_DB_NAME", "DuLieu"),
    "user": os.getenv("ETL_DB_USER", "postgres"),
    "password": os.getenv("ETL_DB_PASSWORD", "Vu123"),
    "host": os.getenv("ETL_DB_HOST", "localhost"),
    "port": os.getenv("ETL_DB_PORT", "5433"),
}

SUPPORTED_EXTS = (".xlsx", ".xls", ".csv")

REQUIRED_TABLES = [
    "etl.batch_runs", "etl.import_errors", "dim.product_costs", "dim.product_costs_stage",
    "nhanh.orders", "nhanh.order_items", "nhanh.ads_costs",
    "shopee.orders", "shopee.order_items", "shopee.ads_costs", "shopee.settlements",
    "tiktok.orders_pnl", "tiktok.order_items", "tiktok.ads_costs", "tiktok.live_costs",
    "mtgt.sales_lines", "mtgt.ads_costs", "mtgt.customer_channel_map", "mtgt.employee_channel_map",
]


def connect(cursor_factory=None):
    return psycopg2.connect(**DB_CONFIG, cursor_factory=cursor_factory)


@contextmanager
def db_cursor(cursor_factory=None):
    conn = connect(cursor_factory=cursor_factory)
    cur = conn.cursor()
    try:
        yield conn, cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        conn.close()


def assert_production_schema() -> None:
    """Fail fast if DDL has not been installed or wrong database is selected."""
    with db_cursor() as (_conn, cur):
        missing = []
        for full_name in REQUIRED_TABLES:
            schema_name, table_name = full_name.split(".", 1)
            cur.execute(
                """
                SELECT EXISTS (
                    SELECT 1 FROM information_schema.tables
                    WHERE table_schema = %s AND table_name = %s
                )
                """,
                (schema_name, table_name),
            )
            if not cur.fetchone()[0]:
                missing.append(full_name)
        if missing:
            raise RuntimeError(
                "Database is not ready. Missing tables: " + ", ".join(missing) +
                ". Run du_lieu_production_schema_v2.sql first."
            )

        # Guard against the v2 bug where util.norm_code stripped accents,
        # hyphens, inner spaces and letter case. If the DB function is old,
        # every ETL lookup can silently write wrong product codes.
        samples = ["HỮ-SC46955", "1 BÁNH-XP", "Túi quai nhựa", "luoc6k", "TUICOGAI-63"]
        cur.execute("SELECT util.norm_code(%s), util.norm_code(%s), util.norm_code(%s), util.norm_code(%s), util.norm_code(%s)", samples)
        got = list(cur.fetchone())
        if got != samples:
            raise RuntimeError(
                "Database util.norm_code is still using the old destructive normalization. "
                "Run fix_exact_code_import_v3.sql once, then reset and re-import dim.product_costs."
            )


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def begin_batch(cur, source_system: str, job_name: str, source_file: Optional[str] = None, source_file_hash: Optional[str] = None) -> int:
    """Create a batch row. If a same file hash was loaded before, keep a new rerun log with NULL hash.

    The DDL has a uniqueness guard on file hash. This function avoids making reruns fail while still
    preserving source file name and batch audit.
    """
    normalized_source = source_system.upper()
    hash_to_insert = source_file_hash
    if source_file_hash:
        cur.execute(
            """
            SELECT batch_id FROM etl.batch_runs
            WHERE source_system = %s AND job_name = %s AND source_file_hash = %s
            LIMIT 1
            """,
            (normalized_source, job_name, source_file_hash),
        )
        if cur.fetchone():
            hash_to_insert = None

    cur.execute(
        """
        INSERT INTO etl.batch_runs(source_system, job_name, source_file, source_file_hash, status)
        VALUES (%s, %s, %s, %s, 'running')
        RETURNING batch_id
        """,
        (normalized_source, job_name, source_file, hash_to_insert),
    )
    return int(cur.fetchone()[0])


def finish_batch(cur, batch_id: int, status: str, rows_read: int = 0, rows_loaded: int = 0, rows_error: int = 0, error_message: Optional[str] = None) -> None:
    cur.execute(
        """
        UPDATE etl.batch_runs
        SET status = %s,
            finished_at = now(),
            rows_read = COALESCE(%s, rows_read),
            rows_loaded = COALESCE(%s, rows_loaded),
            rows_error = COALESCE(%s, rows_error),
            error_message = %s
        WHERE batch_id = %s
        """,
        (status, rows_read, rows_loaded, rows_error, error_message, batch_id),
    )


def log_import_error(
    cur,
    batch_id: Optional[int],
    source_system: str,
    target_table: str,
    source_file: Optional[str],
    row_number: Optional[int],
    error_code: str,
    error_message: str,
    raw_row: Optional[Dict[str, Any]] = None,
) -> None:
    cur.execute(
        """
        INSERT INTO etl.import_errors(
            batch_id, source_system, target_table, source_file, row_number,
            error_code, error_message, raw_row
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (batch_id, source_system.upper(), target_table, source_file, row_number, error_code, error_message, Json(json_safe(raw_row or {}))),
    )


def normalize_header(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value).replace("\ufeff", " ").replace("\n", " ")).strip()


def strip_accents_for_match(text: Any) -> str:
    # Keep dependency-free matching. Vietnamese headers are also matched by original keywords elsewhere.
    s = normalize_header(text).lower()
    replacements = {
        "à":"a","á":"a","ạ":"a","ả":"a","ã":"a","â":"a","ầ":"a","ấ":"a","ậ":"a","ẩ":"a","ẫ":"a","ă":"a","ằ":"a","ắ":"a","ặ":"a","ẳ":"a","ẵ":"a",
        "è":"e","é":"e","ẹ":"e","ẻ":"e","ẽ":"e","ê":"e","ề":"e","ế":"e","ệ":"e","ể":"e","ễ":"e",
        "ì":"i","í":"i","ị":"i","ỉ":"i","ĩ":"i",
        "ò":"o","ó":"o","ọ":"o","ỏ":"o","õ":"o","ô":"o","ồ":"o","ố":"o","ộ":"o","ổ":"o","ỗ":"o","ơ":"o","ờ":"o","ớ":"o","ợ":"o","ở":"o","ỡ":"o",
        "ù":"u","ú":"u","ụ":"u","ủ":"u","ũ":"u","ư":"u","ừ":"u","ứ":"u","ự":"u","ử":"u","ữ":"u",
        "ỳ":"y","ý":"y","ỵ":"y","ỷ":"y","ỹ":"y","đ":"d",
    }
    for a, b in replacements.items():
        s = s.replace(a, b)
    return s


def _plain_number_text(value: Any) -> Optional[str]:
    """Return an Excel/CSV numeric cell as plain text without scientific notation.

    This is only for cells that are actually numeric. Text codes are never
    uppercased, de-accented, stripped of hyphens, or otherwise rewritten.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return ""
        d = Decimal(str(value))
    else:
        return None

    if d == d.to_integral_value():
        return str(int(d))
    text = format(d.normalize(), "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _plain_scientific_text(text: str) -> str:
    """Convert textual scientific notation to plain text when Excel/CSV exports it."""
    if not re.fullmatch(r"[+-]?\d+(?:\.\d+)?[Ee][+-]?\d+", text):
        return text
    try:
        d = Decimal(text)
    except (InvalidOperation, ValueError):
        return text
    if d == d.to_integral_value():
        return str(int(d))
    out = format(d.normalize(), "f")
    return out.rstrip("0").rstrip(".") if "." in out else out


def clean_code(value: Any, max_len: int = 255) -> str:
    """Clean a lookup/code cell WITHOUT changing the actual code characters.

    Production rule: product codes and order identifiers must be preserved
    exactly enough for lookup. Therefore this function intentionally keeps:
    - Vietnamese accents, e.g. "HỮ-SC46955"
    - hyphens and repeated hyphens, e.g. "TUICOGAI-63", "A--B"
    - spaces inside codes, e.g. "1 BÁNH-XP"
    - original upper/lower case, e.g. "luoc6k"

    It only removes invisible boundary noise and converts true numeric Excel
    cells such as 3574661818054.0 or 2.000000000015E+12 into plain text.
    """
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except Exception:
        pass

    if hasattr(value, "item") and not isinstance(value, (str, bytes)):
        try:
            value = value.item()
        except Exception:
            pass

    numeric_text = _plain_number_text(value)
    if numeric_text is not None:
        s = numeric_text
    else:
        s = str(value)

    s = unicodedata.normalize("NFC", s)
    s = s.replace("\ufeff", "").replace("\u200b", "")
    s = s.replace("\xa0", " ")
    s = s.replace("\r", " ").replace("\n", " ").replace("\t", " ")
    s = s.strip()

    if s.upper() in {"", "NAN", "NONE", "NULL", "NAT"}:
        return ""

    # Common Excel/CSV artifacts for numeric codes represented as text.
    if re.fullmatch(r"[+-]?\d+\.0", s):
        s = s[:-2]
    else:
        s = _plain_scientific_text(s)

    return s[:max_len]


def safe_text(value: Any, max_len: Optional[int] = None) -> str:
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except Exception:
        pass
    s = normalize_header(value)
    if s.upper() in {"NAN", "NONE", "NULL", "NAT"}:
        return ""
    return s[:max_len] if max_len else s


def parse_money_one(value: Any) -> float:
    if value is None:
        return 0.0
    try:
        if pd.isna(value):
            return 0.0
    except Exception:
        pass
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        num = float(value)
        return 0.0 if math.isnan(num) or math.isinf(num) else num
    s = str(value).strip()
    if not s or s.lower() in {"nan", "none", "null", "nat", "-"}:
        return 0.0
    negative = False
    if s.startswith("(") and s.endswith(")"):
        negative = True
        s = s[1:-1]
    if s.startswith("-"):
        negative = True
    s = re.sub(r"[^0-9,\.\-]", "", s).replace("-", "")
    if not s or s in {".", ","}:
        return 0.0

    if "." in s and "," in s:
        if s.rfind(".") > s.rfind(","):
            s = s.replace(",", "")
        else:
            s = s.replace(".", "").replace(",", ".")
    elif "," in s:
        parts = s.split(",")
        if len(parts) > 2 or len(parts[-1]) == 3:
            s = s.replace(",", "")
        else:
            s = s.replace(",", ".")
    elif "." in s:
        parts = s.split(".")
        if len(parts) > 2:
            s = s.replace(".", "")
        elif len(parts[-1]) == 3 and len(parts[0]) <= 3:
            s = s.replace(".", "")
    try:
        num = float(s)
    except Exception:
        return 0.0
    if math.isnan(num) or math.isinf(num):
        return 0.0
    return -num if negative else num


def money_series(values: Any) -> pd.Series:
    if values is None:
        return pd.Series(dtype="float64")
    if isinstance(values, pd.Series):
        return values.map(parse_money_one).astype(float)
    if isinstance(values, (list, tuple)):
        return pd.Series(values).map(parse_money_one).astype(float)
    return pd.Series([parse_money_one(values)], dtype="float64")


def safe_num(value: Any) -> float:
    return parse_money_one(value)


def _parse_excel_serial_date(text: str) -> Optional[datetime]:
    if not re.fullmatch(r"\d+(?:\.\d+)?", text):
        return None
    try:
        # Excel stores dates as days since 1899-12-30. Values below 1000 are
        # usually IDs/codes, not dates, so do not treat them as dates.
        serial = float(text)
        if serial < 1000:
            return None
        return (pd.to_datetime("1899-12-30") + pd.to_timedelta(serial, unit="D")).to_pydatetime()
    except Exception:
        return None


def _parse_datetime_value(value: Any, *, prefer_dayfirst: bool = True) -> Optional[datetime]:
    """Parse dates safely for Vietnamese exports.

    Production rule:
    - Ambiguous slash/dash dates such as 10/05/2026 are ALWAYS dd/mm/yyyy.
    - ISO dates such as 2026-05-10 are yyyy-mm-dd.
    - Excel serial dates are supported.
    - We intentionally do NOT fall back to month-first parsing because it can
      silently flip Nhanh.vn dates into future months.
    """
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except Exception:
        pass

    if isinstance(value, pd.Timestamp):
        if pd.isna(value):
            return None
        return value.to_pydatetime().replace(tzinfo=None)
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    if isinstance(value, date):
        return datetime.combine(value, datetime.min.time())

    s = str(value).strip()
    if not s or s.lower() in {"nan", "none", "nat", "null", "-"}:
        return None

    # Remove timezone suffixes often seen in exports. The business date is local
    # date already, so we store naive local time.
    s = re.sub(r"\s*\(UTC[+-]?\d+\)\s*$", "", s, flags=re.IGNORECASE).strip()
    s = re.sub(r"\s+UTC[+-]?\d+(?::?\d{2})?\s*$", "", s, flags=re.IGNORECASE).strip()

    excel_dt = _parse_excel_serial_date(s)
    if excel_dt is not None:
        return excel_dt.replace(tzinfo=None)

    # Strict Vietnamese and ISO formats first. This avoids pandas warnings and
    # prevents 10/05/2026 from becoming 2026-10-05.
    explicit_formats = [
        "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%d/%m/%Y",
        "%d-%m-%Y %H:%M:%S", "%d-%m-%Y %H:%M", "%d-%m-%Y",
        "%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M", "%d.%m.%Y",
        "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
        "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M", "%Y/%m/%d",
    ]
    for fmt in explicit_formats:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            pass

    # Last fallback: still force dayfirst=True. Never attempt month-first.
    dt = pd.to_datetime(s, errors="coerce", dayfirst=True)
    if pd.notna(dt):
        try:
            return dt.to_pydatetime().replace(tzinfo=None)
        except AttributeError:
            return pd.Timestamp(dt).to_pydatetime().replace(tzinfo=None)
    return None


def parse_date(value: Any) -> Optional[date]:
    dt = _parse_datetime_value(value, prefer_dayfirst=True)
    return dt.date() if dt is not None else None


def parse_datetime(value: Any) -> Optional[datetime]:
    return _parse_datetime_value(value, prefer_dayfirst=True)


def read_file(path: Path, dtype=str, header=0) -> pd.DataFrame:
    suffix = path.suffix.lower()
    if suffix == ".csv":
        last_error = None
        for enc in ("utf-8-sig", "utf-8", "cp1258", "latin1"):
            try:
                return pd.read_csv(path, dtype=dtype, header=header, encoding=enc, low_memory=False)
            except Exception as e:
                last_error = e
        raise last_error
    return pd.read_excel(path, dtype=dtype, header=header)


def read_smart_table(path: Path, header_keywords: Sequence[str], max_scan_rows: int = 30) -> pd.DataFrame:
    """Read CSV/Excel and detect header row if export has preamble rows."""
    try:
        df = read_file(path, dtype=str, header=0)
    except Exception:
        df = read_file(path, dtype=str, header=None)

    normalized_keywords = [strip_accents_for_match(k) for k in header_keywords]
    if any(any(k in strip_accents_for_match(c) for k in normalized_keywords) for c in df.columns):
        df.columns = [normalize_header(c) for c in df.columns]
        return df.loc[:, ~pd.Index(df.columns).duplicated()].copy()

    raw = read_file(path, dtype=str, header=None)
    best_idx = None
    for i in range(min(max_scan_rows, len(raw))):
        row_text = " ".join(strip_accents_for_match(x) for x in raw.iloc[i].values if pd.notna(x))
        if any(k in row_text for k in normalized_keywords):
            best_idx = i
            break
    if best_idx is None:
        df.columns = [normalize_header(c) for c in df.columns]
        return df.loc[:, ~pd.Index(df.columns).duplicated()].copy()

    header = [normalize_header(c) for c in raw.iloc[best_idx].values]
    data = raw.iloc[best_idx + 1 :].reset_index(drop=True).copy()
    data.columns = header
    data = data.loc[:, ~pd.Index(data.columns).duplicated()].copy()
    data = data.loc[:, [c for c in data.columns if c and str(c).lower() not in {"nan", "none"}]]
    return data


def find_col(columns: Iterable[Any], keywords: Sequence[str], exact: Optional[str] = None) -> Optional[str]:
    cols = list(columns)
    if exact is not None:
        exact_norm = strip_accents_for_match(exact)
        for c in cols:
            if strip_accents_for_match(c) == exact_norm:
                return c
    keys = [strip_accents_for_match(k) for k in keywords]
    for c in cols:
        c_norm = strip_accents_for_match(c)
        if c_norm in keys:
            return c
    for c in cols:
        c_norm = strip_accents_for_match(c)
        if any(k in c_norm for k in keys):
            return c
    return None


def get_input_files(folder: Path) -> List[Path]:
    folder.mkdir(parents=True, exist_ok=True)
    files = [p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in SUPPORTED_EXTS and not p.name.startswith("~")]
    return sorted(files, key=lambda p: (p.stat().st_mtime, p.name.lower()))


def archive_file(src: Path, dest_folder: Path) -> Path:
    dest_folder.mkdir(parents=True, exist_ok=True)
    dest = dest_folder / src.name
    if dest.exists():
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        dest = dest_folder / f"{src.stem}_{stamp}{src.suffix}"
    shutil.move(str(src), str(dest))
    return dest


def json_safe(value: Any) -> Any:
    """Convert arbitrary ETL values to JSON-serializable Python objects.

    psycopg2.extras.Json cannot serialize date/datetime, Decimal, pandas NA,
    numpy scalars, or nested structures containing those types. This function is
    recursive and is used for every JSONB payload so one bad raw field can never
    break a whole batch.
    """
    if value is None:
        return None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, float):
        return None if math.isnan(value) or math.isinf(value) else value
    if isinstance(value, (str, int, bool)):
        return value
    try:
        if pd.isna(value):
            return None
    except Exception:
        pass
    if hasattr(value, "item") and not isinstance(value, (str, bytes)):
        try:
            return json_safe(value.item())
        except Exception:
            pass
    if isinstance(value, dict):
        return {safe_text(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [json_safe(v) for v in value]
    return safe_text(value)


def row_to_json(row: Any) -> Dict[str, Any]:
    if hasattr(row, "to_dict"):
        raw = row.to_dict()
    elif isinstance(row, dict):
        raw = row
    else:
        return {"value": json_safe(row)}
    safe = json_safe(raw)
    return safe if isinstance(safe, dict) else {"value": safe}


def load_cost_map(cur) -> Dict[str, float]:
    cur.execute("SELECT ma, gia_von FROM dim.product_costs")
    return {clean_code(ma): float(gia_von or 0) for ma, gia_von in cur.fetchall() if clean_code(ma)}


def lookup_unit_cost(code: Any, costs: Dict[str, float]) -> float:
    """Lookup one unit cost using the exact code characters.

    Do not uppercase, remove accents, remove hyphens, or collapse inner spaces.
    Supports bundle code A+B by summing each exact part after boundary cleanup.
    """
    text = clean_code(code)
    if not text:
        return 0.0
    parts = [clean_code(p) for p in re.split(r"\+", text) if clean_code(p)]
    if not parts:
        return 0.0
    if len(parts) == 1:
        return float(costs.get(parts[0], 0.0))
    return float(sum(costs.get(p, 0.0) for p in parts))


def coalesce(*values, default=""):
    for v in values:
        if v is None:
            continue
        try:
            if pd.isna(v):
                continue
        except Exception:
            pass
        if str(v).strip() and str(v).strip().lower() not in {"nan", "none", "null", "nat"}:
            return v
    return default
