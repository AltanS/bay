"""`bay skill`: the agent skill that teaches a coding agent how to use Bay.

    bay skill show                 print the SKILL.md of this Bay
    bay skill install [-H <id>]    write it for each agent harness on this machine
    bay skill status               one line per harness
    bay skill update               rewrite every recorded install (bay self update does this)
    bay skill uninstall [-H <id>]  remove recorded installs

The installs are recorded in ``~/.config/bay/skills.json``. See ``bay_cli.agent_skill``.
"""

from __future__ import annotations

from typing import Annotated

import typer

from bay_cli import agent_skill, console
from bay_cli.context import package_root
from bay_cli.errors import BayError

app = typer.Typer(help="Install and update the Bay skill for coding agents (Claude Code, Codex, OpenCode, Pi).", no_args_is_help=True)

_HarnessOpt = Annotated[
    list[str] | None,
    typer.Option(
        "--harness",
        "-H",
        help=f"Harness id, repeatable: {', '.join(agent_skill.HARNESS_IDS)}. Default: every harness found on this machine.",
    ),
]
_ForceOpt = Annotated[bool, typer.Option("--force", help="Overwrite a skill file that was edited by hand.")]


def installed_version() -> str:
    """The ``bay_version`` of the framework checkout this command runs from."""
    from bay_cli import paths

    return paths.peek_installed_version(package_root()) or "unknown"


def _report(outcomes: list[agent_skill.Outcome], command: str) -> None:
    refused = [o for o in outcomes if o.result == "refused"]
    if console.is_json_mode():
        console.emit_result({"ok": not refused, "files": [o.to_dict() for o in outcomes]}, command=command)
    else:
        for o in outcomes:
            detail = f" ({o.detail})" if o.detail and o.result != "written" else ""
            typer.echo(f"{o.result:<9} {o.harness:<9} {o.path}{detail}")
    if refused:
        raise typer.Exit(1)


@app.command("show")
def show() -> None:
    """Print the SKILL.md text of this Bay, with its stamp.

    Examples:

        bay skill show
        bay skill show | less
    """
    typer.echo(agent_skill.render(installed_version()), nl=False)


@app.command("install")
def install(harness: _HarnessOpt = None, force: _ForceOpt = False) -> None:
    """Write the skill into the user-level skill folder of each harness.

    With no --harness, Bay installs for every harness it finds: the command is on
    PATH, or its config folder exists. Each written path is recorded in
    ~/.config/bay/skills.json, so `bay skill update` and `bay self update` keep it
    current. A file without a bay-skill stamp is never overwritten.

    Examples:

        bay skill install
        bay skill install -H claude -H codex
    """
    ids = harness or [h.id for h in agent_skill.HARNESSES if h.present()]
    if not ids:
        raise BayError(
            "no agent harness found on this machine",
            hint=f"Name one with --harness ({', '.join(agent_skill.HARNESS_IDS)}).",
        )
    _report(agent_skill.install(ids, installed_version(), force=force), "skill install")


@app.command("update")
def update(force: _ForceOpt = False) -> None:
    """Rewrite every recorded install with the skill of this Bay.

    Examples:

        bay skill update
    """
    outcomes = agent_skill.update(installed_version(), force=force)
    if not outcomes and not console.is_json_mode():
        typer.echo("no skill installs recorded. Run `bay skill install`.")
        return
    _report(outcomes, "skill update")


@app.command("uninstall")
def uninstall(harness: _HarnessOpt = None) -> None:
    """Remove the recorded skill files (all, or the --harness ones).

    Examples:

        bay skill uninstall
        bay skill uninstall -H pi
    """
    _report(agent_skill.uninstall(harness), "skill uninstall")


@app.command("status")
def status() -> None:
    """One line per harness: found here, recorded, and the state of the file.

    States: ok (current), outdated (an older Bay wrote it; run `bay skill update`),
    edited (changed by hand), unstamped (not written by Bay), missing.
    Exit 1 when a recorded install is not ok.

    Examples:

        bay skill status
        bay --json skill status
    """
    rows = agent_skill.status(installed_version())
    bad = [r for r in rows if r["recorded"] and r["state"] != "ok"]
    if console.is_json_mode():
        console.emit_result({"ok": not bad, "version": installed_version(), "harnesses": rows}, command="skill status")
    else:
        for r in rows:
            found = "found" if r["present"] else "absent"
            recorded = "recorded" if r["recorded"] else "-"
            typer.echo(f"{r['state']:<9} {r['harness']:<9} {found:<6} {recorded:<8} {r['path']}")
    if bad:
        raise typer.Exit(1)
