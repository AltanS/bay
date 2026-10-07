"""`bay self`: show and change the Bay version installed on this machine.

Bay is installed once per machine from a framework checkout:

    git clone https://github.com/AltanS/bay ~/.local/share/bay/framework
    uv tool install --editable ~/.local/share/bay/framework

The checkout is the framework root (see ``context.package_root``). So an
update is: fetch, check out a tag, install again. Nothing here touches a fleet.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Annotated

import typer

from bay_cli import ansible, console, git, runner
from bay_cli.context import package_root
from bay_cli.errors import BayError

app = typer.Typer(help="Show or change the Bay version installed on this machine.", no_args_is_help=True)


def _update_help() -> str:
    """The help of `bay self update`. Its example names the version in version.yml.

    It reads the file without a YAML parser: this runs at import (see ``paths.peek_installed_version``).
    """
    from bay_cli import paths

    declared = paths.peek_installed_version(package_root())
    tag = f"v{declared}" if declared else "<tag>"
    return (
        "Move this machine to the newest Bay release, or to the tag given by --to.\n\n"
        "Fetches the tags, checks out the target tag, syncs the Python and Ansible\n"
        "dependencies, installs the CLI again (`uv tool install --editable`), and\n"
        "prints the old and the new version. Refuses when the checkout has edits.\n\n"
        "Examples:\n\n"
        "    bay self update\n\n"
        f"    bay self update --to {tag}"
    )


def framework_version(root: Path) -> str:
    """The tag (or ``tag-N-gSHA``) the checkout is on; ``unknown`` outside git."""
    described = git._quiet(root, "describe", "--tags")
    if described:
        return described
    from bay_cli import paths

    declared = paths.read_installed_version(root)
    return f"v{declared}" if declared else "unknown"


def _require_checkout(root: Path) -> None:
    if not (root / ".git").exists():
        raise BayError(
            f"this copy of Bay is not a git checkout: {root}",
            hint="Install it again from a checkout. See docs/install.md.",
        )


@app.command("version")
def version() -> None:
    """Print the installed Bay version and where the checkout lives.

    Examples:

        bay self version
    """
    root = package_root()
    current = framework_version(root)
    if console.is_json_mode():
        console.emit_result({"version": current, "path": str(root)}, command="self version")
        return
    typer.echo(f"bay {current}")
    typer.echo(f"checkout: {root}")


@app.command("update", help=_update_help())
def update(
    to: Annotated[
        str | None,
        typer.Option("--to", help="Move to this tag. Default: the newest tag."),
    ] = None,
) -> None:
    """Move to the newest tag, or to --to. The help text comes from ``_update_help``."""
    root = package_root()
    _require_checkout(root)

    if git.is_dirty(root):
        raise BayError(
            f"the Bay checkout has uncommitted changes: {root}",
            hint="Commit or discard them first. `bay self update` only moves between tags.",
        )

    old = framework_version(root)
    git.fetch_tags(root)

    tags = set(git.list_tags(root))
    if to is not None:
        if to not in tags:
            raise BayError(
                f"there is no tag {to} in {root}",
                hint="Run `git -C <checkout> tag --list` to see the tags.",
            )
        target = to
    else:
        latest = git.latest_tag(root)
        if not latest:
            raise BayError("the Bay checkout has no tags")
        target = latest

    if old == target:
        console.success(f"Already at {target}")
        return

    runner.run(
        ["git", "-C", str(root), "-c", "advice.detachedHead=false", "checkout", "--quiet", target],
        message=f"Checking out {target}...",
    )
    ansible.sync_deps(root)
    _install_tool(root)

    new = framework_version(root)
    if console.is_json_mode():
        console.emit_result({"old": old, "new": new}, command="self update")
        return
    console.success(f"Updated Bay: {old} -> {new}")


def _install_tool(root: Path) -> None:
    """Run `uv tool install --editable <checkout>` again, so the entry point is fresh."""
    try:
        runner.run(
            ["uv", "tool", "install", "--editable", "--force", str(root)],
            message="Installing the bay command...",
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise BayError(f"could not run uv: {exc}", hint="Install uv first. See docs/install.md.") from exc
