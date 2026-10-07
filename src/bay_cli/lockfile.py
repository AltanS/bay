"""Read and write ``projects/<name>/bay.lock`` as raw JSON.

:mod:`bay_cli.fleet` reads a lock for the compiler. This module is the only
writer: ``bay init`` creates a lock, ``bay up`` and ``bay rollback`` pin a
commit and record the deploy. Every write is checked against
``schemas/bay_lock.schema.json`` first and lands atomically (temp file plus
rename), so a reader never sees half a lock.

Version 2 (2.1.0) has ``repo``, ``toml_path``, ``commit`` and ``envs``, and
no path on this machine. A version 1 lock is read as version 2 (its
``local_path`` is dropped); the next write stores version 2.

Deploy record of one environment (all optional, see ``docs/plan.md``)::

    "envs": {"production": {
        "box": "eu-1",
        "commit": "<sha>",               # what bay up applied here
        "deployed_at": "2026-10-06T12:00:00Z",
        "result": "ok",                  # pending | ok | failed (failed = HALF)
        "plan_id": "<12 hex>",
        "last_receipt_sha256": "<64 hex>",
        "previous": {"commit": "<sha>", "deployed_at": ..., "receipt_sha256": ...}
    }}
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from bay_cli import bay_toml
from bay_cli.fleet import LOCK_SCHEMA_PATH, LOCK_VERSION, upgrade_lock
from bay_cli.fleet import lock_file as _lock_file


class LockWriteError(Exception):
    """A lock the CLI was about to write breaks the schema. Nothing was written."""


def lock_path(fleet_root: Path, name: str) -> Path:
    """``<fleet>/projects/<name>/bay.lock``."""
    return _lock_file(fleet_root, name)


def read(path: Path) -> dict[str, Any] | None:
    """The parsed lock as version 2, or None when there is no file."""
    if not path.is_file():
        return None
    data = upgrade_lock(json.loads(path.read_text()))
    if not isinstance(data, dict):
        raise ValueError(f"{path}: not a JSON object")
    return data


def sha256_of(path: Path) -> str | None:
    """Hash of the lock file bytes, or None when there is no file."""
    if not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def problems(raw: dict[str, Any]) -> list[str]:
    schema = json.loads(LOCK_SCHEMA_PATH.read_text())
    return sorted({str(v) for v in bay_toml.schema_violations(raw, schema)})


def new_lock(name: str, *, repo: str | None, toml_path: str = "bay.toml") -> dict[str, Any]:
    return {
        "lock_version": LOCK_VERSION,
        "name": name,
        "repo": repo,
        "commit": None,
        "toml_path": toml_path,
        "envs": {},
    }


def write(path: Path, raw: dict[str, Any]) -> None:
    """Check ``raw`` against the schema, then write it atomically."""
    found = problems(raw)
    if found:
        raise LockWriteError("; ".join(found))
    text = json.dumps(raw, indent=2) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def env_pin(raw: dict[str, Any], env: str) -> str | None:
    """The commit that ``env`` runs per the lock: its own record, else the project pin."""
    record = raw.get("envs", {}).get(env, {})
    commit = record.get("commit")
    return str(commit) if commit else (str(raw["commit"]) if raw.get("commit") else None)
