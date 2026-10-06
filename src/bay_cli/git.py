"""Git operations for bay framework management."""

from pathlib import Path

from bay_cli import runner
from bay_cli.errors import BayError


def fetch_tags(bay_dir: Path) -> None:
    runner.run(
        ["git", "-C", str(bay_dir), "fetch", "--tags", "--prune"],
        message="Fetching bay framework...",
    )


def checkout(bay_dir: Path, ref: str) -> None:
    # Hard-reset index + working tree to discard any local modifications
    # (egg-info rebuilt by uv, __pycache__, etc.)
    runner.run(
        ["git", "-C", str(bay_dir), "reset", "--hard", "HEAD"],
        check=False,
    )
    runner.run(
        ["git", "-C", str(bay_dir), "clean", "-fd"],
        check=False,
    )
    runner.run(
        ["git", "-C", str(bay_dir), "-c", "advice.detachedHead=false", "checkout", ref],
        message=f"Checking out {ref}...",
    )


def describe_tags(bay_dir: Path) -> str | None:
    """Return the current exact tag, or None if not on a tag."""
    result = runner.run(
        ["git", "-C", str(bay_dir), "describe", "--tags", "--exact-match"],
        check=False,
    )
    if result.returncode == 0:
        return result.stdout.strip()
    return None


def latest_tag(bay_dir: Path) -> str | None:
    """Return the latest tag by version sort, or None if no tags."""
    result = runner.run(
        ["git", "-C", str(bay_dir), "tag", "--sort=-v:refname"],
    )
    tags = result.stdout.strip().splitlines()
    return tags[0] if tags else None


def list_tags(bay_dir: Path) -> list[str]:
    """Every tag in the checkout, newest first (version sort)."""
    result = runner.run(["git", "-C", str(bay_dir), "tag", "--sort=-v:refname"])
    return result.stdout.strip().splitlines()


def current_ref(bay_dir: Path) -> str:
    """Return the current tag if on one, otherwise short SHA."""
    tag = describe_tags(bay_dir)
    if tag:
        return tag
    result = runner.run(
        ["git", "-C", str(bay_dir), "rev-parse", "--short", "HEAD"],
    )
    return result.stdout.strip()


def fsck(bay_dir: Path) -> None:
    """Run git fsck. Raises BayError on corruption."""
    result = runner.run(
        ["git", "-C", str(bay_dir), "fsck", "--no-dangling"],
        message="Checking repository integrity...",
        check=False,
    )
    if result.returncode != 0:
        raise BayError(
            f"Git repository is corrupted:\n{result.stderr or result.stdout}"
        )


def clone(url: str, dest: Path) -> None:
    runner.run(
        ["git", "clone", url, str(dest)],
        message="Cloning bay framework...",
    )


# ── Quiet reads ──────────────────────────────────────────────────────────
# `runner.run` prints a failed command's output to the console, which is
# right for an operator watching a human command and wrong for `--json`,
# where stdout must hold one JSON document. These reads print nothing and
# return None when `path` is not a git checkout or git is missing.


def _quiet(path: Path, *args: str) -> str | None:
    import subprocess

    try:
        proc = subprocess.run(
            ["git", "-C", str(path), *args],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def head_commit(path: Path) -> str | None:
    """Full SHA of HEAD, or None when ``path`` is not in a git checkout."""
    return _quiet(path, "rev-parse", "HEAD") or None


def is_dirty(path: Path) -> bool | None:
    """True when ``path`` (and below) has uncommitted changes, None when not git.

    Scoped to ``path`` with ``-- .``: a fleet that lives in a subdirectory of
    a bigger repo is dirty only when its own files are.
    """
    if head_commit(path) is None:
        return None
    out = _quiet(path, "status", "--porcelain", "--", ".")
    if out is None:
        return None
    return bool(out)


def ref_name(path: Path) -> str | None:
    """Exact tag at HEAD, else the short SHA, else None. Never prints."""
    return _quiet(path, "describe", "--tags", "--exact-match") or (
        _quiet(path, "rev-parse", "--short", "HEAD") or None
    )


def newest_tag(path: Path) -> str | None:
    """Highest tag by version sort, or None. Never prints."""
    out = _quiet(path, "tag", "--sort=-v:refname")
    if not out:
        return None
    return out.splitlines()[0].strip() or None
