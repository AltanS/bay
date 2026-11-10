"""`bay toml`: check a bay.toml file, and move it between the fleet and the app repo.

``bay toml validate`` reads one file and nothing else: no fleet, no framework
checkout, no network. Exit 0 and no output when the file is valid; otherwise
one line per violation and exit 1.

``bay toml move <name> --to repo|fleet`` moves a project's bay.toml (and the
files its mounts read) out of the fleet into the app repo, or back. ``--to
repo`` is ``bay adopt`` (:mod:`bay_cli.adopt`), ``--to fleet`` its mirror
(:mod:`bay_cli.tomlmove`).
"""

from __future__ import annotations

import json
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer

from bay_cli import console

app = typer.Typer(help="Work with bay.toml files.", no_args_is_help=True)


@app.command("validate")
def validate(
    path: Annotated[Path, typer.Argument(help="The bay.toml file to check.")] = Path("bay.toml"),
    json_output: Annotated[
        bool,
        typer.Option(
            "--json",
            help='Print {"ok": bool, "violations": [{"path", "message"}]}.',
        ),
    ] = False,
) -> None:
    """Check a bay.toml file against schema v3; print one line per violation."""
    # Imported here: jsonschema is not needed by every `bay` invocation.
    from bay_cli import bay_toml

    violations = bay_toml.validate_file(path)
    if json_output or console.is_json_mode():
        typer.echo(
            json.dumps(
                {"ok": not violations, "violations": [v.to_dict() for v in violations]},
                indent=2,
            )
        )
    else:
        for v in violations:
            typer.echo(str(v))
    if violations:
        raise typer.Exit(1)


class Where(StrEnum):
    fleet = "fleet"
    repo = "repo"


@app.command("move")
def move(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="The project to move.")],
    to: Annotated[
        Where,
        typer.Option(
            "--to",
            help="repo: from the fleet into the app repo you stand in. "
            "fleet: from the app repo into the fleet.",
            case_sensitive=False,
        ),
    ],
    toml_path: Annotated[
        str | None,
        typer.Option(
            "--toml-path",
            help="With --to repo: where bay.toml goes, relative to the repo root (a "
            "monorepo). Default: bay.toml at the repo root.",
        ),
    ] = None,
    check: Annotated[
        bool, typer.Option("--check", help="Print the files and the lock change; change nothing.")
    ] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Print one JSON document.")] = False,
) -> None:
    """Move a project's bay.toml and the files its mounts read between the fleet and its app repo.

    --to repo: run it inside a checkout of the app repo, with the fleet named
    by --fleet, BAY_FLEET or BAY_FLEET_NAME. It copies projects/<name>/ into
    the repo in one local commit and rewrites the lock to repo form. Then run
    bay up, then git push.

    --to fleet: run it from anywhere with a fleet selected. It copies the
    bay.toml as PINNED (the bytes at the pinned app commit) and the files its
    mounts read into projects/<name>/, and rewrites the lock to fleet form, in
    two local fleet commits. It refuses when the app repo holds a config change
    that bay up has not deployed. Then run bay up, then delete the files from
    the app repo and push.

    Either way container, volume, database and config names stay, so the next
    bay plan shows 0 steps.

    Examples:

        bay --fleet ~/fleets/prod toml move shop --to repo
        bay --fleet ~/fleets/prod toml move api --to repo --toml-path services/api/bay.toml
        bay --fleet ~/fleets/prod toml move shop --to fleet --check
        bay --fleet ~/fleets/prod toml move shop --to fleet
    """
    from bay_cli.commands import project_cmd

    if to is Where.repo:
        project_cmd.run_adopt(ctx, name, toml_path=toml_path, check=check, as_json=as_json)
        return
    if toml_path is not None:
        from bay_cli.errors import BayError

        raise BayError(
            "--toml-path is for --to repo",
            hint="A bay.toml in the fleet is always projects/<name>/bay.toml.",
        )
    project_cmd.run_move_to_fleet(ctx, name, check=check, as_json=as_json)
