"""Deploy receipt: what one deploy left running on one box.

``python -m bay_reconcile.receipt`` runs on the box right after the reconciler
pass (``roles/container_lifecycle/tasks/reconcile.yml``) and writes
``/var/lib/bay/receipts/<env>.json``. ``bay status --json`` reads the file back
over SSH. The format is documented in ``docs/deploy-receipt.md``; bump
:data:`RECEIPT_VERSION` for any change a reader would notice.

Inputs:

* ``--meta`` (JSON argument): env, box, framework and fleet versions and the
  reconciler's exit code. No secret is in it.
* ``--bundle``: the reconcile bundle. It holds resolved env (secrets), so only
  ``name``, ``image`` and ``config_hash`` are ever read out of it.
* stdin: the reconciler's JSON report, or nothing when it crashed.

Stdlib only, like the rest of the package: it runs on the box.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

RECEIPT_VERSION = 1
RECEIPTS_DIR = Path("/var/lib/bay/receipts")

# An env name becomes a file name. Keep it to inventory-group characters so a
# crafted name can never climb out of the receipts directory.
_ENV_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*$")

# Reconciler action kind -> receipt action. A canary swap replaces the
# container just like a recreate does; the receipt reports the outcome, not
# the technique.
_ACTIONS: dict[str, str] = {
    "NoOp": "noop",
    "Create": "create",
    "Recreate": "recreate",
    "CanarySwap": "recreate",
    "Remove": "remove",
}


def receipt_path(env: str, directory: Path = RECEIPTS_DIR) -> Path:
    """``<directory>/<env>.json``. Raises ValueError on an unsafe env name."""
    if not _ENV_RE.match(env):
        raise ValueError(f"invalid env name for a receipt: {env!r}")
    return directory / f"{env}.json"


def previous_path(path: Path) -> Path:
    """``<env>.json`` -> ``<env>.prev.json``."""
    return path.with_name(f"{path.stem}.prev.json")


def _now_rfc3339() -> str:
    # timezone.utc, not datetime.UTC: this runs on the box, which may be on 3.10.
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: UP017


def _healthy(state: Mapping[str, Any] | None) -> bool | None:
    """Health right after the pass: True, False, or None when unknown.

    A container that is not running is unhealthy. A running one with no
    healthcheck, or one still ``starting``, is unknown.
    """
    if not state:
        return None
    if state.get("status") != "running":
        return False
    health = state.get("health")
    if health == "healthy":
        return True
    if health == "unhealthy":
        return False
    return None


def build_receipt(
    *,
    meta: Mapping[str, Any],
    bundle: Mapping[str, Any],
    report: Mapping[str, Any] | None,
    deployed_at: str | None = None,
) -> dict[str, Any]:
    """Assemble a version-1 receipt. Pure: no I/O, no clock unless defaulted.

    ``result`` is ``ok`` only when the reconciler exited 0 AND its report says
    ``ok``. A crash (no report) or a failed action is ``failed``.
    """
    rc = meta.get("reconcile_rc")
    ok = rc == 0 and report is not None and report.get("ok") is True

    actions: dict[str, str] = {}
    removed: list[str] = []
    for result in (report or {}).get("results", []) or []:
        if not isinstance(result, Mapping):
            continue
        name = str(result.get("name", ""))
        kind = str(result.get("kind", ""))
        if not name or kind not in _ACTIONS:
            continue
        actions[name] = _ACTIONS[kind]
        if kind == "Remove":
            removed.append(name)

    state = (report or {}).get("state") or {}
    if not isinstance(state, Mapping):
        state = {}

    containers: list[dict[str, Any]] = []
    for entry in bundle.get("containers", []) or []:
        if not isinstance(entry, Mapping):
            continue
        name = str(entry.get("name", ""))
        containers.append(
            {
                "name": name,
                "image": entry.get("image"),
                "config_hash": entry.get("config_hash"),
                "action": actions.get(name),
                "healthy": _healthy(state.get(name)),
            }
        )
    for name in removed:
        containers.append(
            {
                "name": name,
                "image": None,
                "config_hash": None,
                "action": "remove",
                "healthy": None,
            }
        )

    fleet_dirty = meta.get("fleet_dirty")
    return {
        "receipt_version": RECEIPT_VERSION,
        "env": str(meta["env"]),
        "box": str(meta["box"]),
        "deployed_at": deployed_at or _now_rfc3339(),
        "framework_version": meta.get("framework_version"),
        "framework_commit": meta.get("framework_commit") or None,
        "fleet_commit": meta.get("fleet_commit") or None,
        "fleet_dirty": fleet_dirty if isinstance(fleet_dirty, bool) else None,
        "result": "ok" if ok else "failed",
        "containers": containers,
        "projects": {},
    }


def write_receipt(receipt: Mapping[str, Any], path: Path) -> None:
    """Write ``receipt`` to ``path`` atomically, keeping the old one as ``.prev``.

    The new file is written to a temp file in the same directory and renamed
    over the old one, so a reader sees the old receipt or the new one, never
    half of either. The previous receipt is copied to ``<env>.prev.json`` the
    same way before the rename. Mode 0644: a receipt holds no secret, and
    ``bay status`` reads it as whatever user SSH logs in as.
    """
    path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    if path.exists():
        _atomic_write(previous_path(path), path.read_bytes())
    payload = (json.dumps(receipt, indent=2, sort_keys=False) + "\n").encode("utf-8")
    _atomic_write(path, payload)


def _atomic_write(path: Path, data: bytes) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
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


def _load_report(text: str) -> Mapping[str, Any] | None:
    try:
        data = json.loads(text) if text.strip() else None
    except ValueError:
        return None
    return data if isinstance(data, Mapping) else None


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m bay_reconcile.receipt")
    parser.add_argument("--meta", required=True, help="receipt metadata as JSON")
    parser.add_argument("--bundle", required=True, help="path to the reconcile bundle")
    parser.add_argument("--dir", default=str(RECEIPTS_DIR), help="receipts directory")
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))

    meta = json.loads(args.meta)
    try:
        with open(args.bundle, encoding="utf-8") as handle:
            bundle = json.load(handle)
    except (OSError, ValueError):
        bundle = {}
    report = _load_report(sys.stdin.read())

    receipt = build_receipt(meta=meta, bundle=bundle, report=report)
    path = receipt_path(str(meta["env"]), Path(args.dir))
    write_receipt(receipt, path)
    # Echo the receipt (no secret in it) so the Ansible log shows what landed.
    print(
        json.dumps(
            {
                "path": str(path),
                "result": receipt["result"],
                "containers": len(receipt["containers"]),
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
