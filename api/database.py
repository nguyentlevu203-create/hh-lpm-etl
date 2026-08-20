import os
import psycopg2
from fastapi import HTTPException


def _require_db_password() -> str:
    value = os.getenv("ETL_DB_PASSWORD")
    if not value:
        raise RuntimeError(
            "Missing ETL_DB_PASSWORD environment variable. Set it in your environment "
            "or .env (see .env.example) before starting the API."
        )
    return value


DB_CONFIG = {
    "dbname": os.getenv("ETL_DB_NAME", "DuLieu"),
    "user": os.getenv("ETL_DB_USER", "postgres"),
    "password": _require_db_password(),
    "host": os.getenv("ETL_DB_HOST", "localhost"),
    "port": os.getenv("ETL_DB_PORT", "5433"),
}


def get_db_connection():
    try:
        return psycopg2.connect(**DB_CONFIG)
    except Exception as e:
        print(f"❌ Lỗi kết nối DB: {e}")
        raise HTTPException(status_code=500, detail="Không thể kết nối đến Database DuLieu")
