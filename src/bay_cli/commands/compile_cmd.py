"""`bay compile`: build services.yml from bay.fleet.toml and the pinned bay.toml files.

Each project is read at the commit its lock pins, through the same temp
copy of the fleet that ``bay plan`` and ``bay up`` compile
(:func:`bay_cli.plan.compiled_fleet`). A project with no pinned commit is
left out, with a note. ``--working-tree`` reads the local checkouts as they
are instead (dev use).

Exit codes: 0 written or already up to date (or ``--check`` found no
difference), 1 for a ``--check`` difference, invalid input, a hand-edited
target or a feature the deploy code cannot express yet.
"""

from __future__ import annotations

import difflib
from pathlib import Path
from typing import Annotated

import typer

from bay_cli import console
from bay_cli.context import Context, context_from
from bay_cli.errors import BayError


def compile_fleet(
    ctx: typer.Context,
    fleet: Annotated[
        Path | None,
        typer.Option("--fleet", help="The fleet directory. Default: the fleet Bay finds."),
    ] = None,
    out: Annotated[
        Path | None,
        typer.Option(
            "--out",
            help="Write under this directory instead of the fleet, at the same "
            "relative path as in the fleet.",
        ),
    ] = None,
    check: Annotated[
        bool,
        typer.Option("--check", help="Write nothing. Exit 1 and print a diff if the file differs."),
    ] = False,
    force: Annotated[
        bool,
        typer.Option("--force", help="Overwrite a file that was edited by hand."),
    ] = False,
    adopt: Annotated[
        bool,
        typer.Option(
            "--adopt",
            help="First compile: replace a services.yml that bay compile did not write.",
        ),
    ] = False,
    working_tree: Annotated[
        bool,
        typer.Option(
            "--working-tree",
            help="Read each project's local checkout as it is, not its pinned commit (dev use).",
        ),
    ] = False,
    allow_unsupported: Annotated[
        bool,
        typer.Option(
            "--allow-unsupported",
            help="Write the file even when a bay.toml uses a feature Bay cannot deploy yet.",
        ),
    ] = False,
) -> None:
    """Compile bay.fleet.toml and every pinned bay.toml into services.yml."""
    from bay_cli import compiler
    from bay_cli.fleet import GENERATED_SERVICES, FleetError, load_inputs

    cx = Context.resolve(fleet) if fleet is not None else context_from(ctx)
    target = (out / GENERATED_SERVICES) if out is not None else (cx.fleet_root / GENERATED_SERVICES)

    if working_tree:
        try:
            inputs = load_inputs(cx.fleet_root, output=target)
            result = compiler.compile_fleet(inputs)
        except (FleetError, compiler.CompileError) as exc:
            for line in exc.lines:
                typer.echo(line, err=True)
            raise BayError(f"compile failed with {len(exc.lines)} problem(s)") from None
    else:
        from bay_cli.plan import compiled_fleet

        with compiled_fleet(cx) as comp:
            for note in comp.notes:
                typer.echo(f"note: {note}", err=True)
            if comp.result is None:
                for line in comp.errors:
                    typer.echo(line, err=True)
                raise BayError(
                    f"compile failed with {len(comp.errors)} problem(s)",
                    hint="Pass --working-tree to read the local checkouts as they are.",
                )
            result = comp.result

    if result.unsupported:
        for item in result.unsupported:
            typer.echo(str(item), err=True)
        if not allow_unsupported:
            raise BayError(
                f"{len(result.unsupported)} feature(s) above cannot be deployed yet",
                hint="Remove them from bay.toml, or pass --allow-unsupported to write the "
                "closest form Bay can deploy today.",
            )

    text = result.text()
    current = target.read_text() if target.is_file() else None

    if check:
        if current == text:
            console.success(f"{target} is up to date")
            return
        diff = difflib.unified_diff(
            (current or "").splitlines(keepends=True),
            text.splitlines(keepends=True),
            fromfile=f"{target} (on disk)",
            tofile=f"{target} (compiled)",
        )
        typer.echo("".join(diff), nl=False)
        raise typer.Exit(1)

    if current == text:
        console.success(f"{target} is up to date")
        return
    if current is not None and not force:
        _refuse_hand_edits(target, current, adopt)

    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(text)
    tmp.replace(target)
    console.success(
        f"wrote {target} ({len(result.services)} service(s), "
        f"{len(result.accessories)} other container(s))"
    )


def _refuse_hand_edits(target: Path, current: str, adopt: bool) -> None:
    from bay_cli import compiler

    recorded, body = compiler.split_header(current)
    if recorded is None:
        if adopt:
            return
        raise BayError(
            f"{target} was not written by bay compile",
            hint="Run `bay compile --adopt` once to replace it. Git keeps the old file.",
        )
    if recorded != compiler.body_digest(body):
        raise BayError(
            f"{target} was edited by hand after the last compile; its hash no longer matches",
            hint="Move the change into bay.toml or bay.fleet.toml, then run "
            "`bay compile --force` to overwrite the file.",
        )
