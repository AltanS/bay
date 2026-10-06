"""`bay toml`: check a bay.toml file against schema v3.

The check reads one file and nothing else: no fleet, no framework checkout,
no network. Exit 0 and no output when the file is valid; otherwise one line
per violation and exit 1.
"""

from __future__ import annotations

import json
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
