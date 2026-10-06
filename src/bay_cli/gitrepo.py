"""Git reads and writes for the fleet repo and for project checkouts.

Every call is non-interactive: stdin is closed and ``GIT_TERMINAL_PROMPT=0``,
so a fetch that would ask for a password fails instead of waiting. Reads
return None on failure and print nothing (``--json`` output must stay one
document). Writes raise :class:`GitError`.

Nothing here pushes. ``bay up`` commits the fleet repo; publishing it is the
operator's step.
"""

from __future__ import annotations

import io
import os
import subprocess
import tarfile
from pathlib import Path

_ENV = {"GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"}


class GitError(Exception):
    """A git write failed. ``str(exc)`` is git's last line of output."""


def _run(repo: Path, *args: str, timeout: int = 30) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=timeout,
            env={**os.environ, **_ENV},
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def _run_bytes(
    repo: Path, *args: str, timeout: int = 30
) -> subprocess.CompletedProcess[bytes] | None:
    try:
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            stdin=subprocess.DEVNULL,
            timeout=timeout,
            env={**os.environ, **_ENV},
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def _out(repo: Path, *args: str, timeout: int = 30) -> str | None:
    proc = _run(repo, *args, timeout=timeout)
    if proc is None or proc.returncode != 0:
        return None
    return str(proc.stdout).strip()


def is_repo(path: Path) -> bool:
    return path.is_dir() and _out(path, "rev-parse", "--git-dir") is not None


def head(repo: Path) -> str | None:
    """Full SHA of HEAD, or None (not a repo, or no commit yet)."""
    return _out(repo, "rev-parse", "--verify", "--quiet", "HEAD") or None


def resolve_commit(repo: Path, ref: str) -> str | None:
    """Full SHA of ``ref`` when it names a commit in ``repo``, else None."""
    return _out(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}") or None


def show_file(repo: Path, commit: str, rel: str) -> bytes | None:
    """The bytes of ``rel`` at ``commit``, or None when it is not there."""
    proc = _run_bytes(repo, "show", f"{commit}:{rel}")
    if proc is None or proc.returncode != 0:
        return None
    return proc.stdout


def has_path(repo: Path, commit: str, rel: str) -> bool:
    proc = _run(repo, "cat-file", "-e", f"{commit}:{rel}")
    return proc is not None and proc.returncode == 0


def extract(repo: Path, commit: str, rels: list[str], dest: Path) -> None:
    """Write ``rels`` (files or directories) as they are at ``commit`` under ``dest``."""
    dest.mkdir(parents=True, exist_ok=True)
    if not rels:
        return
    proc = _run_bytes(repo, "archive", "--format=tar", commit, "--", *rels, timeout=60)
    if proc is None or proc.returncode != 0:
        detail = proc.stderr.decode(errors="replace") if proc is not None else ""
        raise GitError(_last_line(detail) or f"git archive failed in {repo}")
    with tarfile.open(fileobj=io.BytesIO(proc.stdout)) as tar:
        tar.extractall(dest, filter="data")


def dirty(repo: Path, *, exclude: tuple[str, ...] = ()) -> bool | None:
    """True when the work tree has changes (untracked files count). None outside git."""
    if head(repo) is None and not is_repo(repo):
        return None
    spec = [".", *(f":(exclude){e}" for e in exclude)]
    out = _out(repo, "status", "--porcelain", "--untracked-files=normal", "--", *spec)
    if out is None:
        return None
    return bool(out)


def remote_url(repo: Path, name: str = "origin") -> str | None:
    return _out(repo, "remote", "get-url", name) or None


def behind_remote(repo: Path) -> tuple[bool | None, str | None]:
    """Is the fleet behind its remote? ``(behind, problem)``.

    A repo with no remote is never behind. Otherwise this fetches (no prompt,
    60 s) and counts the upstream commits HEAD lacks. ``problem`` is set when
    the answer is unknown: the fetch failed, or the branch tracks nothing.
    """
    remotes = _out(repo, "remote")
    if remotes is None:
        return None, "cannot list the fleet remotes"
    if not remotes:
        return False, None
    proc = _run(repo, "fetch", "--quiet", "--no-tags", timeout=60)
    if proc is None or proc.returncode != 0:
        detail = _last_line(str(proc.stderr)) if proc is not None else "timed out"
        return None, f"cannot fetch the fleet remote: {detail or 'no detail'}"
    upstream = _out(repo, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    if not upstream:
        return None, "the fleet branch tracks no remote branch; set one with git branch -u"
    count = _out(repo, "rev-list", "--count", "HEAD..@{u}")
    if count is None:
        return None, f"cannot compare the fleet with {upstream}"
    return int(count) > 0, None


def commit_paths(repo: Path, paths: list[Path], message: str) -> str:
    """Stage ``paths`` and commit only them. Return the new HEAD.

    Other staged or unstaged changes in the repo are left alone. When the
    paths hold no change, nothing is committed and the current HEAD returns.
    """
    rels = [str(p.relative_to(repo)) if p.is_absolute() else str(p) for p in paths]
    add = _run(repo, "add", "--", *rels)
    if add is None or add.returncode != 0:
        raise GitError(_last_line(str(add.stderr)) if add is not None else "git add failed")
    staged = _run(repo, "diff", "--cached", "--quiet", "--", *rels)
    if staged is not None and staged.returncode == 0:
        current = head(repo)
        if current is None:
            raise GitError("nothing to commit and the repo has no commit")
        return current
    done = _run(repo, "commit", "--quiet", "-m", message, "--", *rels, timeout=120)
    if done is None or done.returncode != 0:
        detail = (str(done.stderr) or str(done.stdout)) if done is not None else ""
        raise GitError(_last_line(detail) or "git commit failed")
    new = head(repo)
    if new is None:
        raise GitError("git commit left no HEAD")
    return new


def _last_line(text: str) -> str:
    lines = [ln.strip() for ln in text.strip().splitlines() if ln.strip()]
    return lines[-1] if lines else ""
