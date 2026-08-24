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
