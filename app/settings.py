import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")
FRONTEND_URL = os.getenv("FRONTEND_URL", "").strip().rstrip("/")
CORS_ORIGINS = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:5174",
    "http://127.0.0.1:5174",
]
if FRONTEND_URL and FRONTEND_URL not in CORS_ORIGINS:
    CORS_ORIGINS.append(FRONTEND_URL)
SARVAM_KEY = os.getenv("SARVAM_API_KEY") or os.getenv("SARVAM_AI_API", "")
SARVAM_MODEL = os.getenv("SARVAM_MODEL", "sarvam-105b")
DATABASE_URL = os.getenv("DATABASE_URL", "")
STORAGE_BACKEND = os.getenv("STORAGE_BACKEND", "postgres" if DATABASE_URL else "sqlite")
SQLITE_PATH = os.getenv("SQLITE_PATH", str(ROOT / "data" / "procurement.db"))
