"""Test-process safety bootstrap for the durable task database."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from uuid import uuid4

_repository_root = Path(__file__).resolve().parent.parent


def _existing_absolute_directory(value: str | None) -> Path | None:
    if not value:
        return None
    candidate = Path(value)
    if not candidate.is_absolute():
        return None
    try:
        if not candidate.is_dir():
            return None
        resolved = candidate.resolve()
    except OSError:
        return None
    try:
        resolved.relative_to(_repository_root)
    except ValueError:
        return candidate
    return None


def _select_temporary_root() -> Path:
    for variable in ("TEMP", "TMP"):
        root = _existing_absolute_directory(os.environ.get(variable))
        if root is not None:
            return root

    try:
        fallback = tempfile.gettempdir()
    except OSError as exc:
        raise RuntimeError("Pytest could not establish a safe temporary directory.") from exc
    root = _existing_absolute_directory(fallback)
    if root is None:
        raise RuntimeError("Pytest could not establish a safe temporary directory.")
    return root


_temporary_root = _select_temporary_root()
os.environ["TEMP"] = str(_temporary_root)
os.environ["TMP"] = str(_temporary_root)

# Force a new path for each pytest process before test modules can import the app.
os.environ["JARVIS_TASK_DATABASE_PATH"] = str(
    _temporary_root / f"jarvis-pytest-tasks-{uuid4().hex}.sqlite3"
)
