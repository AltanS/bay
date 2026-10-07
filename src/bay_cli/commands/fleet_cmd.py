"""`bay fleet`: create, clone and list the fleets on this machine.

A fleet lives at ``~/.config/bay/fleets/<name>``. Every other command finds it
with ``--fleet <path>``, ``BAY_FLEET=<path>`` or ``BAY_FLEET_NAME=<name>``.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Annotated

import typer

from bay_cli import console, runner
from bay_cli.context import FLEET_ENV, FLEET_NAME_ENV, fleets_root
from bay_cli.errors import BayError, ErrorCode
from bay_cli.fleet import FLEET_FILE

app = typer.Typer(help="Create, clone and list the fleets on this machine.", no_args_is_help=True)

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")

_FLEET_TEMPLATE = """\
# {file}: the fleet file. You edit it by hand; `bay compile` reads it.
# Change the values below to match your boxes, then run `bay compile`.

name = "{name}"
default_box = "main"
default_domain = "example.com"

[boxes.main]
env = "production"
"""


_FIRST_COMMIT = "bay: fleet init"

_GITIGNORE = """\
# Caches of fleet state that Bay keeps per fleet. Never commit them.
.bay-cache/
# The ansible-vault password. Never commit it.
.vault_pass
"""


def _check_name(name: str) -> None:
    if not _NAME_RE.match(name):
        raise BayError(
            f"'{name}' is not a fleet name",
            hint="Use lowercase letters, digits and hyphens, and start with a letter or digit.",
            code=ErrorCode.VALIDATION_ERROR,
        )


def _first_commit(dest: Path) -> None:
    """``git init``, then commit the two files. A failure leaves the fleet and warns."""
    try:
        runner.run(["git", "init", "--quiet", str(dest)])
    except (BayError, OSError, subprocess.SubprocessError):
        console.warning("git init failed. The fleet is created, but it is not a git repo yet.")
        return
    try:
        runner.run(["git", "-C", str(dest), "add", FLEET_FILE, ".gitignore"])
        runner.run(["git", "-C", str(dest), "commit", "--quiet", "-m", _FIRST_COMMIT])
    except (BayError, OSError, subprocess.SubprocessError):
        console.warning(
            "The first commit failed (for example, git has no user name or email). "
            "The fleet is created. Run: "
            f'git -C {dest} add {FLEET_FILE} .gitignore && git -C {dest} commit -m "{_FIRST_COMMIT}"'
        )


def _warn_vault_pass_not_ignored(dest: Path) -> None:
    """A cloned fleet may not ignore ``.vault_pass``. Say so, so the password is never committed."""
    try:
        done = subprocess.run(
            ["git", "-C", str(dest), "check-ignore", "-q", ".vault_pass"],
            capture_output=True,
            check=False,
        )
    except OSError:
        return
    if done.returncode == 1:
        console.warning(
            f".vault_pass is not in the .gitignore of {dest}. "
            "Add the line `.vault_pass` so the vault password is never committed."
        )


def _how_to_pick(name: str, path: Path) -> list[str]:
    return [
        "Pick this fleet in one of three ways:",
        f"  1. export {FLEET_NAME_ENV}={name}",
        f"  2. export {FLEET_ENV}={path}",
        f"  3. bay --fleet {path} <command>",
    ]


@app.command("init")
def init(
    name: Annotated[str, typer.Argument(help="Fleet name. Becomes the directory name.")],
    from_url: Annotated[
        str | None,
        typer.Option("--from", help="Clone the fleet from this git URL instead of creating it."),
    ] = None,
) -> None:
    """Create a fleet at ~/.config/bay/fleets/<name>, or clone one with --from.

    A new fleet gets a minimal bay.fleet.toml, a .gitignore that holds
    .vault_pass, and a git repo with the first commit ("bay: fleet init").

    Examples:

        bay fleet init prod
        bay fleet init prod --from git@example.com:team/fleet.git
    """
    _check_name(name)
    dest = fleets_root() / name
    if dest.exists():
        raise BayError(
            f"fleet '{name}' already exists at {dest}",
            hint="Pick another name, or remove the directory first.",
            code=ErrorCode.CONFLICT,
        )
    dest.parent.mkdir(parents=True, exist_ok=True)

    if from_url is not None:
        runner.run(["git", "clone", from_url, str(dest)], message=f"Cloning {from_url}...")
        if not (dest / FLEET_FILE).is_file():
            console.warning(f"{FLEET_FILE} is not in the clone. Check that this is a fleet repo.")
        _warn_vault_pass_not_ignored(dest)
    else:
        dest.mkdir()
        (dest / FLEET_FILE).write_text(_FLEET_TEMPLATE.format(file=FLEET_FILE, name=name))
        (dest / ".gitignore").write_text(_GITIGNORE)
        _first_commit(dest)

    if console.is_json_mode():
        console.emit_result({"name": name, "path": str(dest), "cloned": from_url is not None}, command="fleet init")
        return
    console.success(f"Fleet '{name}' is ready: {dest}")
    for line in _how_to_pick(name, dest):
        console.info(line)


@app.command("ls")
def ls() -> None:
    """List the fleets in ~/.config/bay/fleets.

    Examples:

        bay fleet ls
    """
    root = fleets_root()
    found = sorted(p for p in root.iterdir() if p.is_dir()) if root.is_dir() else []
    rows = [
        {
            "name": p.name,
            "path": str(p),
            "has_fleet_file": (p / FLEET_FILE).is_file(),
        }
        for p in found
    ]
    if console.is_json_mode():
        console.emit_result({"root": str(root), "fleets": rows}, command="fleet ls")
        return
    if not rows:
        console.info(f"No fleets in {root}.")
        console.info("Create one with: bay fleet init <name>")
        return
    for row in rows:
        mark = "" if row["has_fleet_file"] else f"  (no {FLEET_FILE})"
        console.console.print(f"  {row['name']}  {row['path']}{mark}")

