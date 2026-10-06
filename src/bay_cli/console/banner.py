"""ASCII art banner for the Bay CLI."""

from __future__ import annotations

from typing import TYPE_CHECKING

from rich.text import Text

from bay_cli.console.output import console
from bay_cli.console.output import is_json_mode
from bay_cli.console.theme import BRAND_BOLD, BRAND_DIM

if TYPE_CHECKING:
    from bay_cli.context import Context

_LOGO = [
    ("[ B A Y ]", BRAND_BOLD),
    ("~-~-~-~-~", BRAND_DIM),
]


def banner(*, subtitle: str = "", version: str = "") -> None:
    """Print the Bay ASCII banner with optional subtitle and version."""
    if is_json_mode():
        return
    console.print()
    for text, style in _LOGO:
        console.print(Text(f"  {text}", style=style))
    meta = "  "
    if version:
        meta += version
    if subtitle:
        if version:
            meta += "  "
        meta += subtitle
    if meta.strip():
        console.print(Text(meta, style="dim"))
    console.print()


def show_banner(cx: Context | None = None, *, subtitle: str = "") -> None:
    """Print banner with the framework version read from ``cx``.

    Without a Context there is no framework to ask, so the version and the
    dev-link warning are left out.
    """
    if is_json_mode():
        return
    if cx is None:
        banner(subtitle=subtitle)
        return
    banner(subtitle=subtitle, version=_get_version(cx))
    _show_dev_link_warning(cx)


def _show_dev_link_warning(cx: Context) -> None:
    """Show a prominent warning if dev-link mode is active."""
    try:
        from bay_cli.paths import is_dev_linked

        if is_dev_linked(cx.fleet_root):
            from rich.panel import Panel

            console.print(
                Panel(
                    "[bold yellow]DEV-LINK ACTIVE[/bold yellow] — .bay/ is symlinked to local framework\n"
                    "[dim]Run 'bin/bay dev-unlink' to restore pinned version[/dim]",
                    border_style="yellow",
                    expand=False,
                )
            )
    except Exception:
        pass


def _get_version(cx: Context) -> str:
    """Read version from git tag (source of truth), falling back to version.yml."""
    try:
        import subprocess

        bay_dir = cx.framework_root

        # Prefer exact git tag
        result = subprocess.run(
            ["git", "-C", str(bay_dir), "describe", "--tags", "--exact-match"],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()

        # Not on exact tag — show describe output (e.g. v0.38.0-3-gabc1234)
        result = subprocess.run(
            ["git", "-C", str(bay_dir), "describe", "--tags"],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()

        # Last resort: version.yml
        version_yml = bay_dir / "version.yml"
        if version_yml.exists():
            for line in version_yml.read_text().splitlines():
                if line.strip().startswith("bay_version:"):
                    return "v" + line.split(":", 1)[1].strip().strip('"')
    except Exception:
        pass
    return ""
