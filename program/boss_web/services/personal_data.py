"""Storage for workspace-owned resumes, scores, drafts and interview questions."""

from __future__ import annotations

import json
import os
from pathlib import Path
import time

import boss_db

_LEGACY_OWNER = "legacy_personal_workspace"


def database_path(root: Path | None = None) -> Path:
    """Keep the installation's existing data with its first active workspace."""
    installation = boss_db.resolve_db_path()
    active = os.environ.get("BOSS_WORKFLOW_DIR", "").strip()
    if root is None:
        if not active:
            return installation
        root = Path(active)
    root = root.expanduser().resolve()

    record = boss_db.doc_get_raw(_LEGACY_OWNER, installation)
    if record is None:
        conn = boss_db.acquire(installation)
        payload = json.dumps({"root": str(root)}, ensure_ascii=False)
        with conn:
            conn.execute("INSERT OR IGNORE INTO doc (name, payload, updated_at) VALUES (?, ?, ?)",
                         (_LEGACY_OWNER, payload, time.time()))
        record = boss_db.doc_get_raw(_LEGACY_OWNER, installation)
    if Path(json.loads(record)["root"]) == root:
        return installation
    return root / "personal.db"


def acquire():
    """Open the personal database for the active workspace."""
    return boss_db.acquire(database_path())


def acquire_with_jobs():
    """Provide the personal connection and the job table for a read-only join."""
    path = database_path()
    conn = boss_db.acquire(path)
    installation = boss_db.resolve_db_path()
    if path == installation:
        return conn, "main.jobs"
    if not any(row["name"] == "platform" for row in conn.execute("PRAGMA database_list")):
        conn.execute("ATTACH DATABASE ? AS platform", (str(installation.resolve()),))
    return conn, "platform.jobs"
