"""Deploy receipt: what one deploy left running on one box.

``python -m bay_reconcile.receipt`` runs on the box right after the reconciler
pass (``roles/container_lifecycle/tasks/reconcile.yml``) and writes
``/var/lib/bay/receipts/<env>.json``. ``bay status --json`` reads the file back
over SSH. The format is documented in ``docs/deploy-receipt.md``; bump
:data:`RECEIPT_VERSION` for any change a reader would notice.

Inputs:

* ``--meta`` (JSON argument): env, box, framework and fleet versions and the
  reconciler's exit code. No secret is in it. ``code_moves`` (optional): the
  moves ``bay_reconcile.codepin`` reported before the pass; each becomes a row
  of the receipt's ``code_moves`` (``name``, ``status``, ``detail``), so the
  CLI can say which code move was skipped and why. ``stack_dir`` (optional):
  the stack directory of the box. When set, the receipt's ``routes`` lists
  the tailnet routes the box serves, read from the route file the traefik
  role rendered there (:func:`bay_reconcile.routes.rendered_routes`).
* ``--bundle``: the reconcile bundle. It holds resolved env (secrets), so only
  ``name``, ``image`` and ``config_hash`` are ever read out of it.
* stdin: the reconciler's JSON report, or nothing when it crashed. Its
  ``state`` gives each container's health, ``commit`` and resolved ``image``;
  a result with ``status: failed`` sets the container's ``failed``.

``python -m bay_reconcile.receipt stamp ...`` (:func:`stamp_main`) updates one
container's ``commit`` and ``image`` after a webhook build recreated it.

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

from .images import is_commit, is_commit_tag, short
from .routes import rendered_routes

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
    """Assemble a version-1 receipt. No I/O but one read, no clock unless defaulted.

    The one read: with ``stack_dir`` in ``meta``, ``routes`` lists the routes
    in ``<stack_dir>/dynamic/tailnet-proxies.yml`` (``[]`` when the box has no
    route file). Without ``stack_dir`` the receipt has no ``routes`` key.

    ``result`` is ``ok`` only when the reconciler exited 0 AND its report says
    ``ok``. A crash (no report) or a failed action is ``failed``.
    """
    rc = meta.get("reconcile_rc")
    ok = rc == 0 and report is not None and report.get("ok") is True

    actions: dict[str, str] = {}
    failed: set[str] = set()
    removed: list[str] = []
    for result in (report or {}).get("results", []) or []:
        if not isinstance(result, Mapping):
            continue
        name = str(result.get("name", ""))
        kind = str(result.get("kind", ""))
        if not name or kind not in _ACTIONS:
            continue
        actions[name] = _ACTIONS[kind]
        if result.get("status") == "failed":
            failed.add(name)
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
        seen = state.get(name)
        seen = seen if isinstance(seen, Mapping) else {}
        containers.append(
            {
                "name": name,
                # The image the container runs: <repo>:<commit12> when that tag
                # is provably it (see __main__._state_after), else the ref.
                "image": seen.get("image") or entry.get("image"),
                # The reference the deploy asked for (the spec's image). The
                # plan's receipt hash reads this, so a webhook build that moves
                # `image` and `commit` (stamp_receipt) is not drift.
                "image_ref": entry.get("image"),
                "commit": _commit_or_none(seen.get("commit")),
                # Where the commit came from: the image's commit label, or its
                # single commit tag. Null when there is no commit.
                "commit_source": _source_or_none(seen.get("commit"), seen.get("commit_source")),
                # The own-repo commit tags of the running image, for a
                # container with no commit label (null otherwise). With
                # several, `commit` is null, and the CLI looks for the pin here.
                "commit_tags": _tags_or_none(seen.get("commit_tags")),
                "config_hash": entry.get("config_hash"),
                "action": actions.get(name),
                # The reconciler reported this container's action as failed.
                "failed": name in failed,
                "healthy": _healthy(state.get(name)),
            }
        )
    for name in removed:
        containers.append(
            {
                "name": name,
                "image": None,
                "image_ref": None,
                "commit": None,
                "commit_source": None,
                "commit_tags": None,
                "config_hash": None,
                "action": "remove",
                "failed": name in failed,
                "healthy": None,
            }
        )

    fleet_dirty = meta.get("fleet_dirty")
    out: dict[str, Any] = {
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
    moves = _code_moves(meta.get("code_moves"))
    if moves is not None:
        out["code_moves"] = moves
    stack_dir = meta.get("stack_dir")
    if isinstance(stack_dir, str) and stack_dir:
        out["routes"] = rendered_routes(Path(stack_dir))
    return out


def _code_moves(value: object) -> list[dict[str, Any]] | None:
    """The codepin moves of this deploy, reduced to ``name``, ``status`` and ``detail``."""
    if not isinstance(value, list):
        return None
    out: list[dict[str, Any]] = []
    for move in value:
        if isinstance(move, Mapping) and move.get("name"):
            out.append(
                {
                    "name": str(move["name"]),
                    "status": str(move.get("status") or ""),
                    "detail": str(move.get("detail") or ""),
                }
            )
    return out


def _commit_or_none(value: object) -> str | None:
    return short(str(value)) if is_commit(value) else None


#: Values of a container's ``commit_source``.
COMMIT_SOURCES = ("label", "tag")


def _tags_or_none(value: object) -> list[str] | None:
    """The sorted commit tags in ``value``, or None when it is not a list."""
    if not isinstance(value, list):
        return None
    return sorted({t for t in value if is_commit_tag(t)})


def _source_or_none(commit: object, source: object) -> str | None:
    """``label`` or ``tag`` for a container with a commit; None otherwise."""
    if not is_commit(commit):
        return None
    return str(source) if source in COMMIT_SOURCES else "label"


def stamp_receipt(
    receipt: Mapping[str, Any], *, name: str, commit: str | None, image: str
) -> dict[str, Any] | None:
    """A copy of ``receipt`` with one container's ``commit`` and ``image`` replaced.

    ``rebuild.sh`` calls this after a webhook build recreated the container,
    so ``bay status`` and ``bay plan`` see the code the box runs now. Only
    those two fields move, and ``commit_source`` with them (``label``: every
    build labels its image with its commit): ``image_ref``, ``config_hash``, ``action`` and the
    deploy fields stay what the last deploy wrote. None when the receipt does
    not list the container (nothing to stamp).
    """
    out = dict(receipt)
    rows = [dict(c) if isinstance(c, Mapping) else c for c in receipt.get("containers") or []]
    hit = False
    for row in rows:
        if isinstance(row, dict) and row.get("name") == name:
            row.setdefault("image_ref", row.get("image"))
            row["image"] = image
            row["commit"] = _commit_or_none(commit)
            row["commit_source"] = _source_or_none(commit, "label")
            # A webhook build labels its image, so no tag list is needed.
            row["commit_tags"] = None
            hit = True
    if not hit:
        return None
    out["containers"] = rows
    return out


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


def stamp_main(argv: Sequence[str]) -> int:
    """``python -m bay_reconcile.receipt stamp --env E --name N --commit C --image I``.

    Rewrites ``<env>.json`` in place (atomically). Never touches
    ``<env>.prev.json``: a webhook build is not a deploy. Exit 0 when stamped,
    1 when there is no receipt or it does not list the container.
    """
    parser = argparse.ArgumentParser(prog="python -m bay_reconcile.receipt stamp")
    parser.add_argument("--env", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--commit", default="")
    parser.add_argument("--image", required=True)
    parser.add_argument("--dir", default=str(RECEIPTS_DIR), help="receipts directory")
    args = parser.parse_args(list(argv))
    path = receipt_path(args.env, Path(args.dir))
    try:
        current = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 1
    if not isinstance(current, Mapping):
        return 1
    stamped = stamp_receipt(current, name=args.name, commit=args.commit or None, image=args.image)
    if stamped is None:
        return 1
    _atomic_write(path, (json.dumps(stamped, indent=2) + "\n").encode("utf-8"))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    if raw[:1] == ["stamp"]:
        return stamp_main(raw[1:])
    parser = argparse.ArgumentParser(prog="python -m bay_reconcile.receipt")
    parser.add_argument("--meta", required=True, help="receipt metadata as JSON")
    parser.add_argument("--bundle", required=True, help="path to the reconcile bundle")
    parser.add_argument("--dir", default=str(RECEIPTS_DIR), help="receipts directory")
    args = parser.parse_args(raw)

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
