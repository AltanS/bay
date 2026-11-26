"""Do two commits of one project hold the same settings?

``bay show`` and ``bay plan`` judge a pin that is older than WANTED by the
settings, not by the commit id (since 2.27.0). A push that changes code only
leaves the pin where it is; the box deploys it by itself. Nothing waits for
``bay up`` then, so the status must not ask for one.

"Same settings" is the definition of the hold guard (``docs/build-pipeline.md``,
"Hold guard"), read from git:

* the ``bay.toml`` at both commits has the same canonical hash
  (:func:`bay_reconcile.tomlhash.canonical_hash`; a comment, a blank line or a
  key order does not count), and
* every file that the mounts of either ``bay.toml`` read (``from`` mounts, no
  ``fleet:`` ones, a directory counts for what is under it) is identical at both
  commits. The compiler lists them as ``build.bay_toml_files``.

It works for a project in an app repo and for one in the fleet. A commit or a
file that cannot be read gives ``None``: the caller then judges by the commit
id, as it did before 2.27.0.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import TYPE_CHECKING

from bay_cli import gitrepo, reposource
from bay_cli.compiler import _repo_mount_paths
from bay_reconcile.tomlhash import NoTomlParser, canonical_hash

if TYPE_CHECKING:
    from bay_cli.plan import ProjectRef


def _locate(proj: ProjectRef, commit: str) -> tuple[Path, str] | None:
    """The repo that holds ``commit`` and its full id: the checkout, else the repo cache."""
    repo, full = proj.checkout, gitrepo.resolve_commit(proj.checkout, commit)
    if full is None and not proj.in_fleet and proj.repo:
        found = reposource.find_commit(proj.cx.fleet_root, proj.repo, commit, cwd=proj.cwd)
        repo, full = found.path or repo, found.commit
    return (repo, full) if full else None


def _settings_at(proj: ProjectRef, commit: str) -> tuple[Path, str, str, list[str]] | None:
    """``(repo, commit, canonical hash, mount paths)`` of the bay.toml at ``commit``."""
    located = _locate(proj, commit)
    if located is None:
        return None
    repo, full = located
    data = gitrepo.show_file(repo, full, proj.toml_path)
    if data is None:
        return None
    try:
        doc = tomllib.loads(data.decode("utf-8"))
        digest = canonical_hash(data)
    except (UnicodeDecodeError, ValueError, NoTomlParser):
        return None
    return repo, full, digest, _repo_mount_paths(doc, proj.toml_path)


def same_settings(
    proj: ProjectRef, wanted_commit: str | None, pin_commit: str | None
) -> bool | None:
    """True when WANTED and the pin hold the same settings, False when they differ.

    None when either commit or a file cannot be read, and when there is
    nothing to compare (no commit, or the two are one commit).
    """
    if not wanted_commit or not pin_commit or wanted_commit == pin_commit:
        return None
    wanted = _settings_at(proj, wanted_commit)
    pinned = _settings_at(proj, pin_commit)
    if wanted is None or pinned is None:
        return None
    w_repo, w_full, w_hash, w_files = wanted
    p_repo, p_full, p_hash, p_files = pinned
    if w_hash != p_hash:
        return False
    for rel in sorted(set(w_files) | set(p_files)):
        w_id = gitrepo.object_id(w_repo, w_full, rel)
        p_id = gitrepo.object_id(p_repo, p_full, rel)
        if w_id != p_id:
            return False
    return True


#: The plan note for a pin that is older than WANTED with no settings change.
NO_CHANGE_NOTE = "no settings change: bay up is not needed (it would only move the pin)"


def no_change_note(
    proj: ProjectRef, wanted_commit: str | None, pin_commit: str | None
) -> str | None:
    """The note of a plan that has no step, when WANTED differs from the pin only in code.

    None when the settings differ, or when Bay cannot tell. The caller checks
    that the plan has no step and no blocker.
    """
    return NO_CHANGE_NOTE if same_settings(proj, wanted_commit, pin_commit) is True else None
