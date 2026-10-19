"""Test-process safety bootstrap for the durable task database."""

from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

_temporary_root = Path(os.environ.get("TEMP") or os.environ.get("TMP", ""))
if not _temporary_root.is_absolute() or not _temporary_root.is_dir():
    raise RuntimeError("Pytest requires an existing absolute temporary directory.")

# Force a new path for each pytest process before test modules can import the app.
os.environ["JARVIS_TASK_DATABASE_PATH"] = str(
    _temporary_root / f"jarvis-pytest-tasks-{uuid4().hex}.sqlite3"
)
