"""`bay import`: turn a fleet in today's YAML layout into bay.fleet.toml and bay.toml files.

Read-only against ``--fleet``. Writes only into ``--out``, which must be empty
or absent. ``--check`` writes nothing it keeps: it imports into a scratch
directory, compiles, renders today's file and the compiled one through the
deploy code and prints every container that differs.

Exit codes: 0 written (or ``--check`` found no difference), 1 for a
``--check`` difference or a fleet that cannot be read.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from bay_cli import console
from bay_cli.errors import BayError


def import_fleet(
    fleet: Annotated[
        Path,
        typer.Option("--fleet", help="The fleet in today's YAML layout. Never written."),
    ],
    out: Annotated[
        Path | None,
        typer.Option("--out", help="An empty or new directory for the new fleet files."),
    ] = None,
    name: Annotated[
        str | None,
        typer.Option("--name", help="The fleet name. Default: the name of the --fleet directory."),
    ] = None,
    check: Annotated[
        bool,
        typer.Option(
            "--check",
            help="Write nothing. Import into a scratch directory, compile, and print every "
            "container whose deploy settings would change. Exit 1 on a difference.",
        ),
    ] = False,
    show_diff: Annotated[
        bool,
        typer.Option("--diff", help="With --check: print the full diff of each container."),
    ] = False,
) -> None:
    """Import a fleet from today's YAML files into bay.fleet.toml, bay.toml files and lockfiles."""
    from bay_cli import importer

    if not fleet.is_dir():
        raise BayError(f"{fleet} is not a directory")

    if check:
        _check(fleet, name, show_diff)
        return

    if out is None:
        raise BayError(
            "--out is required", hint="Pass an empty directory, or use --check to write nothing."
        )
    if out.resolve() == fleet.resolve() or fleet.resolve() in out.resolve().parents:
        raise BayError(
            f"{out} is inside the fleet it reads", hint="Write the new fleet to another directory."
        )
    if out.exists() and any(out.iterdir()):
        raise BayError(f"{out} is not empty", hint="Pass an empty or new directory.")
    try:
        result = importer.import_fleet(fleet, name)
    except importer.ImportError_ as exc:
        for line in exc.lines:
            typer.echo(line, err=True)
        raise BayError("import failed") from None
    result.write(out)
    for line in result.report_lines():
        typer.echo(line)
    console.success(
        f"wrote {out}: {len(result.projects)} project(s), {len(result.resources)} resource(s), "
        f"{len(result.copies)} config file(s)"
    )


def _check(fleet: Path, name: str | None, show_diff: bool) -> None:
    from bay_cli import compiler, importer, roundtrip
    from bay_cli.fleet import FleetError

    try:
        gate = roundtrip.run_gate(fleet, name=name)
    except importer.ImportError_ as exc:
        for line in exc.lines:
            typer.echo(line, err=True)
        raise BayError("import failed") from None
    except (FleetError, compiler.CompileError) as exc:
        for line in exc.lines:
            typer.echo(line, err=True)
        raise BayError("the imported fleet does not compile") from None
    for line in gate.imported.report_lines():
        typer.echo(line)
    for line in gate.summary_lines():
        typer.echo(line)
    if show_diff:
        for d in gate.diffs:
            typer.echo(d.diff, nl=False)
    if not gate.ok:
        raise typer.Exit(1)
    console.success("every container keeps its deploy settings")
