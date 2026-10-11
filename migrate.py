"""Run versioned, additive migrations with a direct Postgres connection."""
import os
from pathlib import Path

import psycopg

from app import settings

if __name__ == "__main__":
    url = os.getenv("MIGRATION_DATABASE_URL") or os.getenv("DATABASE_URL_UNPOOLED") or settings.DATABASE_URL
    if not url:
        raise SystemExit("Set DATABASE_URL_UNPOOLED or MIGRATION_DATABASE_URL first.")
    with psycopg.connect(url, connect_timeout=15) as conn:
        for path in sorted((settings.ROOT / "migrations").glob("*.sql")):
            conn.execute(path.read_text())
            print(f"Applied {path.name}")
