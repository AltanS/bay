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
from collections.abc import Sequence
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
    """The bytes of ``rel`` at ``commit``, or None when it is not there.

    ``rel`` is relative to ``repo`` (``./`` form), so a fleet that is a
    subdirectory of a bigger repo works too.
    """
    proc = _run_bytes(repo, "show", _object(repo, commit, rel))
    if proc is None or proc.returncode != 0:
        return None
    return proc.stdout


def has_path(repo: Path, commit: str, rel: str) -> bool:
    proc = _run(repo, "cat-file", "-e", _object(repo, commit, rel))
    return proc is not None and proc.returncode == 0


def _object(repo: Path, commit: str, rel: str) -> str:
    """``<commit>:./<rel>`` in a work tree; a mirror has none, so ``<commit>:<rel>`` there."""
    if (repo / "HEAD").is_file() and (repo / "objects").is_dir() and is_bare(repo):
        return f"{commit}:{_plain(rel)}"
    return f"{commit}:./{_plain(rel)}"


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


def _specs(rel: str | Sequence[str]) -> list[str]:
    return [rel] if isinstance(rel, str) else list(rel)


def is_ancestor(repo: Path, older: str, newer: str) -> bool | None:
    """True when ``older`` is an ancestor of ``newer`` (or the same commit).

    None when git cannot tell (a commit is not in ``repo``).
    """
    proc = _run(repo, "merge-base", "--is-ancestor", older, newer)
    if proc is None or proc.returncode not in (0, 1):
        return None
    return proc.returncode == 0


def diff_name_status(repo: Path, old: str, new: str) -> list[list[str]] | None:
    """``git diff --name-status -M old..new`` as one list of fields per changed path.

    A rename is ``["R100", old_path, new_path]``, anything else
    ``[status, path]``. None when git cannot tell (an unknown commit).
    """
    out = _out(repo, "-c", "core.quotepath=off", "diff", "--name-status", "-M", f"{old}..{new}")
    if out is None:
        return None
    return [line.split("\t") for line in out.splitlines() if line]


def diff_changed_lines(repo: Path, old: str, new: str, rel: str) -> list[str] | None:
    """The added and removed lines of ``rel`` between two commits, ``+``/``-`` kept.

    No context lines and no file headers. None when git cannot tell.
    """
    out = _out(repo, "diff", "--unified=0", "--no-color", f"{old}..{new}", "--", rel)
    if out is None:
        return None
    return [
        line
        for line in out.splitlines()
        if line[:1] in "+-" and not line.startswith(("+++", "---"))
    ]


def last_change(repo: Path, rel: str | Sequence[str], ref: str = "HEAD") -> str | None:
    """The newest commit at or before ``ref`` that touched ``rel``, or None.

    ``rel`` is one path or a list of pathspecs (``:(exclude)...`` works).
    """
    return _out(repo, "log", "-1", "--format=%H", ref, "--", *_specs(rel)) or None


def last_change_matching(
    repo: Path, rel: str | Sequence[str], pattern: str, ref: str = "HEAD"
) -> str | None:
    """The newest commit at or before ``ref`` whose diff of ``rel`` adds or drops a line
    that matches the extended regular expression ``pattern`` (``git log -E -G``), or None.

    One git call, however many lines match.
    """
    return _out(
        repo, "log", "-1", "--format=%H", "-E", "-G", pattern, ref, "--", *_specs(rel)
    ) or None


def path_dirty(repo: Path, rel: str | Sequence[str]) -> bool | None:
    """True when ``rel`` differs from HEAD in the work tree (untracked files count)."""
    out = _out(repo, "status", "--porcelain", "--untracked-files=normal", "--", *_specs(rel))
    return None if out is None else bool(out)


def uncommitted_paths(repo: Path, specs: Sequence[str]) -> list[str]:
    """Untracked or changed files under ``specs``, relative to ``repo``. Empty outside git."""
    proc = _run(
        repo, "status", "--porcelain=v1", "-z", "--untracked-files=all", "--", *specs
    )
    if proc is None or proc.returncode != 0:
        return []
    out: list[str] = []
    parts = proc.stdout.split("\0")
    i = 0
    while i < len(parts):
        entry = parts[i]
        i += 1
        if len(entry) < 4:
            continue
        status, path = entry[:2], entry[3:]
        if "R" in status or "C" in status:
            i += 1  # the source path of a rename follows
        if status.strip() == "D":
            continue
        out.append(path)
    top = toplevel(repo)
    if top is not None and top.resolve() != repo.resolve():
        prefix = repo.resolve().relative_to(top.resolve()).as_posix() + "/"
        out = [p[len(prefix) :] if p.startswith(prefix) else p for p in out]
    return sorted(set(out))


def is_bare(repo: Path) -> bool:
    return _out(repo, "rev-parse", "--is-bare-repository") == "true"


def on_remote(repo: Path, commit: str) -> bool | None:
    """Is ``commit`` on a branch of the remote? None when git cannot tell.

    In a checkout that means a remote-tracking branch (``refs/remotes/``)
    contains it, as of the last fetch or push. In a mirror (the fleet's repo
    cache) every branch is the remote's, so ``refs/heads/`` counts.
    """
    refs = "refs/heads/" if is_bare(repo) else "refs/remotes/"
    out = _out(repo, "for-each-ref", "--contains", commit, "--format=%(refname)", refs)
    if out is None:
        return None
    return any(line and not line.endswith("/HEAD") for line in out.splitlines())


def clone_mirror(url: str, dest: Path) -> str | None:
    """``git clone --mirror url dest``. Returns a problem line, or None when it worked."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        proc = subprocess.run(
            ["git", "clone", "--quiet", "--mirror", url, str(dest)],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=300,
            env={**os.environ, **_ENV},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"git clone failed: {exc}"
    if proc.returncode != 0:
        return _last_line(proc.stderr) or "git clone failed"
    return None


def fetch_prune(repo: Path) -> str | None:
    """``git fetch --prune`` in a mirror. Returns a problem line, or None when it worked."""
    proc = _run(repo, "fetch", "--quiet", "--prune", "origin", timeout=120)
    if proc is None:
        return "git fetch timed out"
    if proc.returncode != 0:
        return _last_line(str(proc.stderr)) or "git fetch failed"
    return None


def is_tracked(repo: Path, rel: str) -> bool:
    proc = _run(repo, "ls-files", "--error-unmatch", "--", rel)
    return proc is not None and proc.returncode == 0


def tracked_files(repo: Path, rel: str) -> list[str]:
    """Paths git tracks under ``rel``, relative to ``repo``. Empty when git cannot tell."""
    out = _out(repo, "ls-files", "--", rel)
    return [line for line in (out or "").splitlines() if line]


def remove_and_commit(repo: Path, rels: Sequence[str], message: str) -> str:
    """``git rm`` the tracked ``rels`` and commit only that removal. Return the new HEAD."""
    remove(repo, rels)
    return commit_staged(repo, rels, message)


def remove(repo: Path, rels: Sequence[str]) -> None:
    """``git rm`` the tracked ``rels`` (work tree and index). Raises GitError."""
    if not rels:
        return
    done = _run(repo, "rm", "--quiet", "--", *rels)
    if done is None or done.returncode != 0:
        raise GitError(_last_line(str(done.stderr)) if done is not None else "git rm failed")


def move(repo: Path, old: str, new: str) -> None:
    """``git mv old new``. Raises GitError."""
    proc = _run(repo, "mv", "--", old, new)
    if proc is None or proc.returncode != 0:
        raise GitError(_last_line(str(proc.stderr)) if proc is not None else "git mv failed")


def add(repo: Path, rels: Sequence[str]) -> None:
    proc = _run(repo, "add", "--", *rels)
    if proc is None or proc.returncode != 0:
        raise GitError(_last_line(str(proc.stderr)) if proc is not None else "git add failed")


def commit_staged(repo: Path, rels: Sequence[str], message: str) -> str:
    """Commit what is staged under ``rels`` (moves included) and nothing else."""
    done = _run(repo, "commit", "--quiet", "-m", message, "--", *rels, timeout=120)
    if done is None or done.returncode != 0:
        detail = (str(done.stderr) or str(done.stdout)) if done is not None else ""
        raise GitError(_last_line(detail) or "git commit failed")
    new = head(repo)
    if new is None:
        raise GitError("git commit left no HEAD")
    return new


def dirty(repo: Path, *, exclude: tuple[str, ...] = ()) -> bool | None:
    """True when the work tree has changes (untracked files count). None outside git."""
    if head(repo) is None and not is_repo(repo):
        return None
    spec = [".", *(f":(exclude){e}" for e in exclude)]
    out = _out(repo, "status", "--porcelain", "--untracked-files=normal", "--", *spec)
    if out is None:
        return None
    return bool(out)


def toplevel(repo: Path) -> Path | None:
    """The work tree root that holds ``repo`` (``git rev-parse --show-toplevel``)."""
    out = _out(repo, "rev-parse", "--show-toplevel")
    return Path(out) if out else None


def is_toplevel(repo: Path) -> bool:
    """True when ``repo`` is the root of its own git work tree, not a subdirectory."""
    top = toplevel(repo)
    return top is not None and top.resolve() == repo.resolve()


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


def push(repo: Path) -> tuple[bool, str | None]:
    """Push HEAD to its remote branch. ``(pushed, problem)``. Never prompts.

    The upstream remote wins, else ``origin``, else the first remote. A repo
    with no remote returns ``(False, None)``: there is nothing to push to.
    """
    remotes = (_out(repo, "remote") or "").split()
    if not remotes:
        return False, None
    upstream = _out(repo, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    if upstream:
        proc = _run(repo, "push", "--quiet", timeout=120)
    else:
        remote = "origin" if "origin" in remotes else remotes[0]
        proc = _run(repo, "push", "--quiet", remote, "HEAD", timeout=120)
    if proc is None:
        return False, "git push timed out"
    if proc.returncode != 0:
        return False, _last_line(str(proc.stderr)) or "git push failed"
    return True, None


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


def _plain(rel: str) -> str:
    return rel[2:] if rel.startswith("./") else rel


def _last_line(text: str) -> str:
    lines = [ln.strip() for ln in text.strip().splitlines() if ln.strip()]
    return lines[-1] if lines else ""
