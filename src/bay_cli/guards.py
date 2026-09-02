"""Pre-flight and post-flight guard checks."""

from pathlib import Path

from bay_cli import console, git, paths
from bay_cli.errors import BayError


def check_bay_version(bay_dir: Path, root: Path | None = None) -> None:
    """Fail if .bay-version doesn't match the installed framework version.

    This is a hard guard — it raises :class:`BayError` on mismatch.
    In dev-link mode the check is silently skipped (version pinning is
    intentionally bypassed).
    """
    if paths.is_dev_linked(root):
        return

    pinned = paths.read_pinned_version(root)
    if pinned is None:
        return

    installed = paths.read_installed_version(bay_dir)
    if installed is None:
        raise BayError(
            "Framework version unknown\n"
            "  .bay/version.yml is missing or unreadable.\n\n"
            "Run 'bin/bay install' to sync."
        )

    if installed.lstrip("v") != pinned.lstrip("v"):
        raise BayError(
            f"Framework version mismatch\n"
            f"  .bay-version: {pinned}\n"
            f"  installed:     {installed}\n\n"
            f"Run 'bin/bay install' to sync."
        )


def check_git_health(bay_dir: Path) -> None:
    """Run git fsck. Raises on corruption."""
    git.fsck(bay_dir)


def show_update_notice(bay_dir: Path, root: Path | None = None) -> None:
    """Print a notice if a newer framework version is available locally."""
    pinned = paths.read_pinned_version(root)
    if pinned is None:
        return
    latest = git.latest_tag(bay_dir)
    if latest and pinned != latest:
        console.console.print()
        console.warning(f"Update available: {pinned} -> {latest} — run 'bin/bay update'")
