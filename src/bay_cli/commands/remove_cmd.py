"""``bay remove``: plan taking a project, or one environment of it, out of a fleet.

The verb only plans. ``bay approve`` and ``bay up <env> --plan-id <id>``
apply it (:mod:`bay_cli.remove`). Kept small: the logic loads on use, so the
CLI start-up cost stays flat.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from bay_cli.errors import BayError


def remove(
    ctx: typer.Context,
    project: Annotated[str, typer.Argument(help="The project to take out of the fleet.")],
    env: Annotated[
        str | None,
        typer.Option(
            "--env",
            help="Take only this environment out. bay.toml must no longer have a "
            "deploy.<env> table. Default: every environment and the project itself.",
        ),
    ] = None,
    remote: Annotated[
        bool,
        typer.Option("--remote", help="Also run the deploy in check mode on the box."),
    ] = False,
    no_remote: Annotated[
        bool, typer.Option("--no-remote", help="Do not read the box receipt.")
    ] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Print one JSON document.")] = False,
    log: Annotated[
        Path | None,
        typer.Option("--log", help="Append progress to this file instead of the terminal."),
    ] = None,
) -> None:
    """Plan the removal of a project: its containers stop, its data stays.

    Saves a plan with one destructive remove step per container, and lists
    every volume and database that stays on the box. Bay never deletes data:
    the plan prints the docker volume rm and DROP DATABASE lines for you to
    run by hand later. Exit code: 10 approve, 20 blocked.

    Then: bay approve <plan-id> --reason "<why>", and bay up <env> --plan-id
    <plan-id>. bay up deletes the lock only when the box receipt confirms
    the containers are gone.

    With --remote the plan also asks each box, in check mode, what the deploy
    would remove, as bay plan --remote does. Without it the plan reads the
    receipt only. --no-remote skips the receipt too.

    Examples:

        bay --fleet ~/fleets/prod remove shop
        bay --fleet ~/fleets/prod remove shop --env staging
    """
    from bay_cli import remove as removemod
    from bay_cli.commands.project_cmd import (
        _echo_json,
        _json_error,
        _read_identity,
        find_bay_toml,
        fleet_context,
        routed_output,
    )

    try:
        with routed_output(as_json, log):
            here = find_bay_toml(Path.cwd())
            _, fleet_name = _read_identity(here) if here else (None, None)
            cx = fleet_context(ctx, fleet_name, allow_cwd=True)
            plan = removemod.make_remove_plan(
                cx,
                project,
                env=env,
                read_running=not no_remote,
                box_check=remote,
                cwd=Path.cwd(),
            )
            from bay_cli import plan as planmod

            planmod.save(cx, plan)
    except BayError as exc:
        if not as_json:
            raise
        _json_error(exc)
    if as_json:
        _echo_json(plan)
    else:
        typer.echo(removemod.render_plan(plan))
    raise typer.Exit(int(plan["exit_code"]))
