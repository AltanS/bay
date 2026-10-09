"""The Bay agent skill: one ``SKILL.md`` that teaches a coding agent how to use Bay.

``bay skill install`` writes it into the user-level skill folder of each agent
harness on this machine, and records every file it wrote in
``~/.config/bay/skills.json``. ``bay skill update`` (and ``bay self update``)
rewrite the recorded files with the text of the installed Bay. The text is
``SKILL.md`` at the root of the framework checkout, the file ``bay --skill``
prints.

Each written file ends with a stamp line, ``<!-- bay-skill <version> <hash> -->``,
where the hash is the first 12 hex digits of the sha256 of everything above it.
The stamp tells three states apart: the current text (``ok``), an older Bay text
(``outdated``) and a file someone edited by hand (``edited``). Bay never
overwrites a file without a stamp, and overwrites an edited one only with
``--force``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from bay_cli.context import package_root
from bay_cli.errors import BayError

SKILL_NAME = "bay"
MANIFEST_VERSION = 1
_STAMP = re.compile(r"^<!-- bay-skill (\S+) ([0-9a-f]{12}) -->$", re.MULTILINE)


@dataclass(frozen=True)
class Harness:
    """One agent harness: its id, the command that marks it present, and its skill folder."""

    id: str
    label: str
    binary: str
    home_env: str | None
    default_home: str
    subdir: str = "skills"

    def home(self) -> Path:
        override = os.environ.get(self.home_env) if self.home_env else None
        if override:
            return Path(override).expanduser()
        if self.default_home.startswith("$XDG_CONFIG_HOME/"):
            base = os.environ.get("XDG_CONFIG_HOME") or "~/.config"
            return Path(base).expanduser() / self.default_home.removeprefix("$XDG_CONFIG_HOME/")
        return Path(self.default_home).expanduser()

    def skill_file(self) -> Path:
        return self.home() / self.subdir / SKILL_NAME / "SKILL.md"

    def present(self) -> bool:
        """True when the command is on PATH or its config folder exists."""
        return shutil.which(self.binary) is not None or self.home().is_dir()


HARNESSES: tuple[Harness, ...] = (
    Harness("claude", "Claude Code", "claude", "CLAUDE_CONFIG_DIR", "~/.claude"),
    Harness("codex", "Codex", "codex", "CODEX_HOME", "~/.codex"),
    Harness("opencode", "OpenCode", "opencode", "OPENCODE_CONFIG_DIR", "$XDG_CONFIG_HOME/opencode"),
    Harness("pi", "Pi", "pi", "PI_CODING_AGENT_DIR", "~/.pi/agent"),
)
HARNESS_IDS: tuple[str, ...] = tuple(h.id for h in HARNESSES)


def harness_by_id(harness_id: str) -> Harness:
    for harness in HARNESSES:
        if harness.id == harness_id:
            return harness
    raise BayError(f"unknown harness: {harness_id}", hint=f"Known: {', '.join(HARNESS_IDS)}.")


# ── the text ────────────────────────────────────────────────────────────────


def source_text() -> str:
    """The unstamped ``SKILL.md`` of the framework checkout this command runs from."""
    path = package_root() / "SKILL.md"
    if not path.is_file():
        raise BayError(f"SKILL.md not found in the framework checkout: {path}", hint="Run `bay self update`.")
    return path.read_text(encoding="utf-8")


def _hash(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:12]


def render(version: str, body: str | None = None) -> str:
    """The stamped text for ``version``. The stamp is the last line."""
    text = source_text() if body is None else body
    if not text.endswith("\n"):
        text += "\n"
    return f"{text}<!-- bay-skill {version} {_hash(text)} -->\n"


def read_stamp(text: str) -> tuple[str, str] | None:
    """``(version, hash)`` of the stamp, or ``None`` when the text has none."""
    match = _STAMP.search(text)
    return (match.group(1), match.group(2)) if match else None


def _stamp_matches_body(text: str) -> bool:
    match = _STAMP.search(text)
    return bool(match) and _hash(text[: match.start()]) == match.group(2)


def file_state(path: Path, expected: str) -> str:
    """``ok``, ``outdated``, ``edited``, ``unstamped`` or ``missing``."""
    if not path.is_file():
        return "missing"
    text = path.read_text(encoding="utf-8")
    if read_stamp(text) is None:
        return "unstamped"
    if text == expected:
        return "ok"
    return "outdated" if _stamp_matches_body(text) else "edited"


# ── the manifest ────────────────────────────────────────────────────────────


def manifest_path() -> Path:
    """``~/.config/bay/skills.json``, next to ``~/.config/bay/fleets``."""
    return Path("~/.config/bay/skills.json").expanduser()


def load_manifest() -> dict[str, dict[str, str]]:
    """Recorded installs, keyed by harness id. Empty when there is no manifest."""
    path = manifest_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BayError(f"cannot read {path}: {exc}", hint="Fix or delete the file, then run `bay skill install`.") from exc
    installs = data.get("installs") if isinstance(data, dict) else None
    if not isinstance(installs, dict):
        return {}
    return {k: v for k, v in installs.items() if isinstance(v, dict) and isinstance(v.get("path"), str)}


def save_manifest(installs: dict[str, dict[str, str]]) -> None:
    path = manifest_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": MANIFEST_VERSION, "skill": SKILL_NAME, "installs": dict(sorted(installs.items()))}
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


# ── actions ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Outcome:
    harness: str
    path: str
    result: str  # written | unchanged | refused | removed | absent | skipped
    detail: str = ""

    def to_dict(self) -> dict[str, str]:
        return {"harness": self.harness, "path": self.path, "result": self.result, "detail": self.detail}


def _write(path: Path, text: str, *, force: bool) -> tuple[str, str]:
    state = file_state(path, text)
    if state == "ok":
        return "unchanged", ""
    if state == "unstamped":
        return "refused", "the file has no bay-skill stamp; move it away first"
    if state == "edited" and not force:
        return "refused", "the file was edited by hand; pass --force to overwrite"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return "written", state


def install(harness_ids: list[str], version: str, *, force: bool = False) -> list[Outcome]:
    """Write the skill for each harness and record it in the manifest."""
    harnesses = [harness_by_id(i) for i in harness_ids]
    text = render(version)
    installs = load_manifest()
    outcomes: list[Outcome] = []
    for harness in harnesses:
        path = harness.skill_file()
        result, detail = _write(path, text, force=force)
        if result != "refused":
            installs[harness.id] = {"path": str(path), "version": version}
        outcomes.append(Outcome(harness.id, str(path), result, detail))
    save_manifest(installs)
    return outcomes


def update(version: str, *, force: bool = False) -> list[Outcome]:
    """Rewrite every recorded install with the current text.

    A missing file is written again while its harness is still on this machine.
    When the harness is gone too, the install is skipped, not recreated.
    """
    text = render(version)
    installs = load_manifest()
    outcomes: list[Outcome] = []
    for harness_id, entry in sorted(installs.items()):
        path = Path(entry["path"])
        harness = next((h for h in HARNESSES if h.id == harness_id), None)
        if not path.is_file() and (harness is None or not harness.present()):
            outcomes.append(Outcome(harness_id, str(path), "skipped", "the harness is not on this machine"))
            continue
        result, detail = _write(path, text, force=force)
        if result != "refused":
            entry["version"] = version
        outcomes.append(Outcome(harness_id, str(path), result, detail))
    if installs:
        save_manifest(installs)
    return outcomes


def uninstall(harness_ids: list[str] | None = None) -> list[Outcome]:
    """Remove recorded installs (all when ``harness_ids`` is None). Never removes an unstamped file."""
    if harness_ids is not None:
        for harness_id in harness_ids:
            harness_by_id(harness_id)
    installs = load_manifest()
    targets = sorted(installs) if harness_ids is None else harness_ids
    outcomes: list[Outcome] = []
    for harness_id in targets:
        entry = installs.get(harness_id)
        if entry is None:
            outcomes.append(Outcome(harness_id, "", "absent", "not recorded"))
            continue
        path = Path(entry["path"])
        if not path.is_file():
            installs.pop(harness_id)
            outcomes.append(Outcome(harness_id, str(path), "absent"))
            continue
        if read_stamp(path.read_text(encoding="utf-8")) is None:
            outcomes.append(Outcome(harness_id, str(path), "refused", "the file has no bay-skill stamp; left alone"))
            continue
        path.unlink()
        if path.parent.is_dir() and not any(path.parent.iterdir()):
            path.parent.rmdir()
        installs.pop(harness_id)
        outcomes.append(Outcome(harness_id, str(path), "removed"))
    save_manifest(installs)
    return outcomes


def status(version: str) -> list[dict[str, str | bool | None]]:
    """One row per harness: present on this machine, recorded, file state and stamped version."""
    text = render(version)
    installs = load_manifest()
    rows: list[dict[str, str | bool | None]] = []
    for harness in HARNESSES:
        entry = installs.get(harness.id)
        path = Path(entry["path"]) if entry else harness.skill_file()
        state = file_state(path, text)
        stamp = read_stamp(path.read_text(encoding="utf-8")) if path.is_file() else None
        rows.append(
            {
                "harness": harness.id,
                "present": harness.present(),
                "recorded": entry is not None,
                "path": str(path),
                "state": state,
                "version": stamp[0] if stamp else None,
            }
        )
    return rows
