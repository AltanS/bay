"""``bay.compat.json``: the Bay version floor of a fleet.

Several machines deploy one fleet. A machine with an older Bay can compile
``services.yml`` the old way and deploy it, which undoes what a newer Bay
wrote. So the fleet records the Bay version of the last run that claimed a
deploy from it::

    {"compat_version": 1, "deployed_with": "2.16.0"}

``bay up`` (also ``bay rollback`` and the apply of a remove) writes the file in
its claim commit when ``deployed_with`` is not the version this machine runs.
A steady fleet gets no diff. ``bay up``, ``bay rollback``, a remove,
``bay deploy`` and ``bay provision`` check it after the fleet fetch and before
they write anything or touch a box (:func:`check`):

* this machine runs an older Bay: refuse (``bay_older``, exit 45), unless
  ``--allow-older-bay``. Then the run goes on, and ``bay up`` records the older
  version, so the floor moves down (a deliberate downgrade).
* no file, or the same or a newer Bay: go on.
* a file Bay cannot read, or a ``compat_version`` this Bay does not know:
  refuse (``compat_invalid``, ``compat_newer``, exit 45). No flag skips them.
* this machine does not know its own version: go on with one warning line,
  and write nothing.

``bay plan`` shows the same check as a blocker, like ``env_busy``.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bay_cli.errors import BayError

COMPAT_FILE = "bay.compat.json"
#: The newest file format this Bay reads and writes.
COMPAT_VERSION = 1
SCHEMA_PATH = Path(__file__).parent / "schemas" / "bay_compat.schema.json"

#: :attr:`Check.state` values.
MISSING, SAME, NEWER, OLDER, INVALID, NEWER_FORMAT, UNKNOWN = (
    "missing",
    "same",
    "newer",
    "older",
    "invalid",
    "newer_format",
    "unknown",
)

_VERSION = re.compile(r"^([0-9]+)\.([0-9]+)\.([0-9]+)$")


def parse_version(text: Any) -> tuple[int, int, int] | None:
    """``"2.16.0"`` as ``(2, 16, 0)``. None for anything that is not ``N.N.N``."""
    if not isinstance(text, str):
        return None
    match = _VERSION.match(text.strip())
    if match is None:
        return None
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def installed_version(framework_root: Path) -> str | None:
    """The ``bay_version`` of this machine's framework checkout, or None when unknown."""
    from bay_cli import paths

    try:
        return paths.read_installed_version(framework_root)
    except BayError:
        return None


@dataclass(frozen=True)
class Check:
    """The fleet's ``bay.compat.json`` against the Bay this machine runs."""

    state: str
    installed: str | None
    deployed_with: str | None = None
    message: str | None = None

    @property
    def reason(self) -> str | None:
        """The refusal reason (:mod:`bay_cli.fleetsync`), or None when this does not block."""
        from bay_cli import fleetsync

        return {
            OLDER: fleetsync.REASON_BAY_OLDER,
            INVALID: fleetsync.REASON_COMPAT_INVALID,
            NEWER_FORMAT: fleetsync.REASON_COMPAT_NEWER,
        }.get(self.state)

    @property
    def blocks(self) -> bool:
        return self.reason is not None

    @property
    def next_cmd(self) -> str | None:
        if self.state == OLDER:
            return f"bay self update --to v{self.deployed_with}"
        if self.state == NEWER_FORMAT:
            return "bay self update"
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "deployed_with": self.deployed_with,
            "installed": self.installed,
            "message": self.message,
        }


def older_message(deployed_with: str, installed: str) -> str:
    return (
        f"This fleet was last deployed with Bay v{deployed_with}. This machine runs "
        f"v{installed}. An older Bay can undo what the newer one wrote. Run "
        f"`bay self update --to v{deployed_with}`. To deploy with the older version on "
        "purpose (a downgrade), pass --allow-older-bay."
    )


def _invalid(installed: str | None, problem: str) -> Check:
    return Check(
        INVALID,
        installed,
        message=(
            f"{COMPAT_FILE}: {problem}. Bay cannot tell which Bay last deployed this fleet, "
            f"so it stops. Restore the file from git (git checkout HEAD -- {COMPAT_FILE} in "
            "the fleet, or take it from an older commit), then run the command again."
        ),
    )


def check(fleet_root: Path, installed: str | None) -> Check:
    """Read ``<fleet>/bay.compat.json`` and compare it with ``installed``. Never raises."""
    path = fleet_root / COMPAT_FILE
    if not path.exists() and not path.is_symlink():
        if parse_version(installed) is None:
            return _unknown(installed, None)
        return Check(MISSING, installed)
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        return _invalid(installed, f"cannot read the file ({exc})")
    except ValueError as exc:
        return _invalid(installed, f"not valid JSON ({exc})")
    if not isinstance(doc, dict):
        return _invalid(installed, "not a JSON object")
    found = doc.get("compat_version")
    if isinstance(found, int) and not isinstance(found, bool) and found > COMPAT_VERSION:
        return Check(
            NEWER_FORMAT,
            installed,
            message=(
                f"{COMPAT_FILE}: compat_version: is {found}, but this Bay knows up to "
                f"{COMPAT_VERSION}. A newer Bay wrote it. Run `bay self update`."
            ),
        )
    from bay_cli import bay_toml

    schema = json.loads(SCHEMA_PATH.read_text())
    problems = sorted({str(v) for v in bay_toml.schema_violations(doc, schema)})
    if problems:
        return _invalid(installed, "; ".join(problems))
    deployed_with = str(doc["deployed_with"])
    floor = parse_version(deployed_with)
    assert floor is not None  # the schema pattern
    mine = parse_version(installed)
    if mine is None:
        return _unknown(installed, deployed_with)
    if mine < floor:
        return Check(OLDER, installed, deployed_with, older_message(deployed_with, str(installed)))
    return Check(SAME if mine == floor else NEWER, installed, deployed_with)


def _unknown(installed: str | None, deployed_with: str | None) -> Check:
    what = f"its own version ({installed!r} in version.yml)" if installed else "its own version"
    return Check(
        UNKNOWN,
        installed,
        deployed_with,
        message=(
            f"warning: this Bay does not know {what}, so it does not check {COMPAT_FILE} "
            "and does not write it"
        ),
    )


def for_cx(cx: Any) -> Check:
    """:func:`check` for a :class:`bay_cli.context.Context`."""
    return check(cx.fleet_root, installed_version(cx.framework_root))


def needs_write(result: Check) -> bool:
    """True when a run that claims a deploy must record this machine's version.

    No file, or a file that names another version (a newer Bay here, or an
    older one with ``--allow-older-bay``). Never for an unknown version or a
    file Bay cannot read.
    """
    if result.state == MISSING:
        return True
    return result.state in (NEWER, OLDER) and result.deployed_with != result.installed


def write(fleet_root: Path, version: str) -> Path:
    """Write ``bay.compat.json`` with ``deployed_with: version``, atomically."""
    if parse_version(version) is None:
        raise BayError(f"cannot write {COMPAT_FILE}: {version!r} is not a version like 2.16.0")
    path = fleet_root / COMPAT_FILE
    text = json.dumps({"compat_version": COMPAT_VERSION, "deployed_with": version}, indent=2)
    fd, tmp = tempfile.mkstemp(dir=fleet_root, prefix=f".{COMPAT_FILE}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return path


def allowed_line(result: Check) -> str:
    """The loud line of a run that goes on with ``--allow-older-bay``."""
    return (
        f"WARNING: --allow-older-bay: this fleet was last deployed with Bay "
        f"v{result.deployed_with}, and this machine runs v{result.installed}. This run "
        "deploys with the older Bay."
    )


def require(cx: Any, *, allow_older: bool, say: Any = None) -> Check:
    """The check of ``bay deploy`` and ``bay provision``. They write nothing.

    Raises :class:`bay_cli.fleetsync.SyncError` (exit 45) for a refusal.
    ``allow_older`` lets an older Bay go on with one loud line.
    """
    from bay_cli import fleetsync

    echo = say or fleetsync.notice
    result = for_cx(cx)
    if result.state == UNKNOWN:
        echo(str(result.message))
    elif result.state == OLDER and allow_older:
        echo(allowed_line(result))
    elif result.blocks:
        raise fleetsync.SyncError(
            str(result.message),
            reason=str(result.reason),
            exit_code=fleetsync.EXIT_BAY_OLDER,
            next_cmd=result.next_cmd,
        )
    return result
