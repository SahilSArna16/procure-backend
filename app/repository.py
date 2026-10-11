import json
import sqlite3
from pathlib import Path

from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from . import settings
from .models import Workspace


class ConflictError(Exception):
    pass


class Repository:
    def __init__(self):
        self.pool = None
        if settings.STORAGE_BACKEND == "postgres":
            self.pool = ConnectionPool(settings.DATABASE_URL, min_size=0, max_size=5, timeout=15, kwargs={"connect_timeout": 10, "prepare_threshold": None}, open=False)
            self.pool.open()
        else:
            Path(settings.SQLITE_PATH).parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(settings.SQLITE_PATH) as conn:
                conn.execute("CREATE TABLE IF NOT EXISTS workspaces (token TEXT PRIMARY KEY, payload TEXT NOT NULL, revision INTEGER NOT NULL)")

    def close(self):
        if self.pool:
            self.pool.close()

    def get(self, token: str) -> Workspace | None:
        if self.pool:
            with self.pool.connection() as conn:
                row = conn.execute("SELECT payload FROM procurement.workspaces WHERE token = %s", (token,)).fetchone()
            return Workspace.model_validate(row[0]) if row else None
        with sqlite3.connect(settings.SQLITE_PATH) as conn:
            row = conn.execute("SELECT payload FROM workspaces WHERE token = ?", (token,)).fetchone()
        return Workspace.model_validate_json(row[0]) if row else None

    def save(self, workspace: Workspace, *, new: bool = False):
        previous = workspace.revision
        saved = workspace.model_copy(update={"revision": previous if new else previous + 1})
        if self.pool:
            with self.pool.connection() as conn:
                if new:
                    conn.execute("INSERT INTO procurement.workspaces (token, payload, revision) VALUES (%s, %s, %s)", (saved.id, Jsonb(saved.model_dump(mode="json")), saved.revision))
                else:
                    result = conn.execute("UPDATE procurement.workspaces SET payload = %s, revision = %s, updated_at = now() WHERE token = %s AND revision = %s", (Jsonb(saved.model_dump(mode="json")), saved.revision, saved.id, previous))
                    if result.rowcount != 1:
                        raise ConflictError("This comparison changed in another tab. Reload before saving.")
        else:
            with sqlite3.connect(settings.SQLITE_PATH) as conn:
                if new:
                    conn.execute("INSERT INTO workspaces VALUES (?, ?, ?)", (saved.id, saved.model_dump_json(), saved.revision))
                else:
                    result = conn.execute("UPDATE workspaces SET payload = ?, revision = ? WHERE token = ? AND revision = ?", (saved.model_dump_json(), saved.revision, saved.id, previous))
                    if result.rowcount != 1:
                        raise ConflictError("This comparison changed in another tab. Reload before saving.")
        return saved
