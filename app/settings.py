import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")
SARVAM_KEY = os.getenv("SARVAM_API_KEY") or os.getenv("SARVAM_AI_API", "")
SARVAM_MODEL = os.getenv("SARVAM_MODEL", "sarvam-105b")
DATABASE_URL = os.getenv("DATABASE_URL", "")
STORAGE_BACKEND = os.getenv("STORAGE_BACKEND", "postgres" if DATABASE_URL else "sqlite")
SQLITE_PATH = os.getenv("SQLITE_PATH", str(ROOT / "data" / "procurement.db"))
