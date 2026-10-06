"""Pre-flight guard checks."""

from pathlib import Path

from bay_cli import git


def check_git_health(bay_dir: Path) -> None:
    """Run git fsck on the framework checkout. Raises on corruption."""
    git.fsck(bay_dir)
