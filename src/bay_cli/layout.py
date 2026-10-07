"""Move a fleet to format 2: one folder per project.

Format 1 kept a project's lock beside its folder, ``projects/<name>.lock``.
Format 2 keeps it inside, ``projects/<name>/bay.lock``, so a project is one
folder: its ``bay.toml``, the files that toml mounts, and its lock.

:func:`migrate` does the move once per fleet. Every command that reads the
locks calls :func:`ensure` first (``bay plan``, ``bay up``, ``bay show``,
``bay compile``, ``bay init``), so the first run of a 2.1 CLI moves them:

* each ``projects/<name>.lock`` moves to ``projects/<name>/bay.lock``
  (``git mv``, so history follows the file);
* ``format = 2`` goes into ``bay.fleet.toml``, on the line after ``name``
  (a text edit: comments and order stay);
* one fleet commit, ``bay: move locks into project folders``.

It refuses when one project has both forms, and when ``bay.fleet.toml`` has
uncommitted changes (the commit would take them along). The lock content is
not touched here: a version 1 lock is read as version 2 and stored as
version 2 on its next write.
"""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from bay_cli import gitrepo
from bay_cli.errors import BayError, ErrorCode
from bay_cli.fleet import (
    FLEET_FILE,
    FLEET_FORMAT,
    LOCK_FILE,
    LOCK_SUFFIX,
    PROJECTS_DIR,
    fleet_format,
)

COMMIT_MESSAGE = "bay: move locks into project folders"
FORMAT_MESSAGE = "bay: fleet format 2"

_NAME_LINE = re.compile(r"^name\s*=.*$", re.MULTILINE)
_FORMAT_LINE = re.compile(r"^format\s*=.*$", re.MULTILINE)


@dataclass
class Migration:
    """What :func:`migrate` did, or would do with ``dry_run``."""

    moves: list[tuple[str, str]] = field(default_factory=list)
    set_format: bool = False
    commit: str | None = None
    dry_run: bool = False

    @property
    def changed(self) -> bool:
        return bool(self.moves) or self.set_format

    def lines(self) -> list[str]:
        verb = "would move" if self.dry_run else "moved"
        out = [f"{verb} {old} to {new}" for old, new in self.moves]
        if self.set_format:
            out.append(f"{'would set' if self.dry_run else 'set'} format = 2 in {FLEET_FILE}")
        if self.commit:
            out.append(f"fleet commit {self.commit[:12]}")
        return out


def pending(fleet_root: Path) -> Migration:
    """What a migration of ``fleet_root`` would do. Raises BayError on a conflict."""
    out = Migration(dry_run=True)
    fleet_file = fleet_root / FLEET_FILE
    if not fleet_file.is_file():
        return out
    projects = fleet_root / PROJECTS_DIR
    both: list[str] = []
    for flat in sorted(projects.glob(f"*{LOCK_SUFFIX}")) if projects.is_dir() else []:
        name = flat.name[: -len(LOCK_SUFFIX)]
        new = projects / name / LOCK_FILE
        old_rel = f"{PROJECTS_DIR}/{flat.name}"
        new_rel = f"{PROJECTS_DIR}/{name}/{LOCK_FILE}"
        if new.exists():
            both.append(f"{name} ({old_rel} and {new_rel})")
            continue
        out.moves.append((old_rel, new_rel))
    if both:
        raise BayError(
            "a project has a lock in both places: " + ", ".join(both),
            code=ErrorCode.CONFLICT,
            hint=f"Keep {PROJECTS_DIR}/<name>/{LOCK_FILE}, delete the other one, and commit.",
        )
    try:
        doc = tomllib.loads(fleet_file.read_text())
    except (OSError, tomllib.TOMLDecodeError):
        doc = None  # the fleet file loader reports it
    if doc is not None and fleet_format(doc) < FLEET_FORMAT:
        out.set_format = True
    return out


def with_format(text: str) -> str:
    """``text`` of bay.fleet.toml with ``format = 2``: a minimal edit."""
    line = f"format = {FLEET_FORMAT}"
    # Only the top level, before the first [table], holds the key.
    table = re.search(r"^\s*\[", text, re.MULTILINE)
    top, rest = (text[: table.start()], text[table.start() :]) if table else (text, "")
    if _FORMAT_LINE.search(top):
        return _FORMAT_LINE.sub(line, top, count=1) + rest
    match = _NAME_LINE.search(top)
    if match is None:
        return f"{line}\n{text}"
    end = match.end()
    return f"{top[:end]}\n{line}{top[end:]}{rest}"


def migrate(fleet_root: Path, *, dry_run: bool = False) -> Migration:
    """Move the fleet's flat locks into project folders and set ``format = 2``.

    With ``dry_run`` nothing is written; the result says what would change.
    A fleet that is not a git repo is changed on disk and not committed.
    """
    plan = pending(fleet_root)
    if dry_run or not plan.changed:
        return plan
    in_git = gitrepo.is_repo(fleet_root) and gitrepo.head(fleet_root) is not None
    if in_git and plan.set_format and gitrepo.path_dirty(fleet_root, FLEET_FILE):
        raise BayError(
            f"{FLEET_FILE} has uncommitted changes, and Bay must commit format = 2 into it",
            code=ErrorCode.CONFLICT,
            hint=f"Commit or drop the change to {FLEET_FILE}, then run the command again.",
        )
    done = Migration(moves=list(plan.moves), set_format=plan.set_format)
    staged: list[str] = []
    try:
        for old, new in plan.moves:
            (fleet_root / new).parent.mkdir(parents=True, exist_ok=True)
            if in_git and gitrepo.is_tracked(fleet_root, old):
                gitrepo.move(fleet_root, old, new)
                staged += [old, new]
            else:
                os.replace(fleet_root / old, fleet_root / new)
                if in_git:
                    gitrepo.add(fleet_root, [new])
                    staged.append(new)
        if plan.set_format:
            path = fleet_root / FLEET_FILE
            path.write_text(with_format(path.read_text()))
            if in_git:
                gitrepo.add(fleet_root, [FLEET_FILE])
                staged.append(FLEET_FILE)
        if in_git and staged:
            message = COMMIT_MESSAGE if plan.moves else FORMAT_MESSAGE
            done.commit = gitrepo.commit_staged(fleet_root, staged, message)
    except gitrepo.GitError as exc:
        raise BayError(
            f"cannot move the locks into project folders: {exc}",
            hint="Check `git status` in the fleet; the move may be half done.",
        ) from None
    return done


def ensure(fleet_root: Path) -> list[str]:
    """Migrate when needed. Returns one line per change, empty when there was none."""
    done = migrate(fleet_root)
    return done.lines() if done.changed else []
