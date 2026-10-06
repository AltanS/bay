"""The daily verbs: ``bay init``, ``plan``, ``approve``, ``up``, ``rollback`` and ``show``.

Which project and which fleet:

* Inside an app repo, the ``bay.toml`` in the working directory or above it
  names the project (``name``) and the fleet (``fleet``). The fleet is
  ``~/.config/bay/fleets/<fleet>`` unless ``--fleet <path>`` (before the
  verb) or ``BAY_FLEET`` names a directory.
* ``--project <name>`` works from anywhere; the fleet then comes from
  ``--fleet``, ``BAY_FLEET`` or ``BAY_FLEET_NAME``.

No verb asks a question. Secrets never travel on the command line.
Plan exit codes: 0 auto, 10 approve, 20 blocked, 30 stale.
"""

from __future__ import annotations

import json
import os
import tomllib
from pathlib import Path
from typing import Annotated, Any

import typer

from bay_cli import console
from bay_cli.context import FLEET_ENV, FLEET_NAME_ENV, Context, GlobalOptions, context_from
from bay_cli.errors import BayError

_ProjectOpt = Annotated[
    str | None,
    typer.Option("--project", "-p", help="Project name. Default: the bay.toml here or above."),
]
_JsonOpt = Annotated[bool, typer.Option("--json", help="Print one JSON document.")]
_NoRemoteOpt = Annotated[bool, typer.Option("--no-remote", help="Do not read the box receipt.")]
_AllowUnsupportedOpt = Annotated[
    bool,
    typer.Option(
        "--allow-unsupported",
        help="Plan and apply even when bay.toml uses a feature Bay cannot deploy yet.",
    ),
]


# ── resolution ──────────────────────────────────────────────────────────────


def find_bay_toml(start: Path) -> Path | None:
    for directory in [start, *start.parents]:
        candidate = directory / "bay.toml"
        if candidate.is_file():
            return candidate
    return None


def _read_identity(path: Path) -> tuple[str | None, str | None]:
    try:
        doc = tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise BayError(f"{path}: {exc}") from None
    name, fleet = doc.get("name"), doc.get("fleet")
    return (name if isinstance(name, str) else None, fleet if isinstance(fleet, str) else None)


def fleet_context(ctx: typer.Context, fleet_name: str | None) -> Context:
    """``--fleet <path>``, then ``BAY_FLEET``, then the fleet named in bay.toml, then
    ``BAY_FLEET_NAME``."""
    obj = ctx.find_root().obj
    if isinstance(obj, Context):
        return obj
    if (isinstance(obj, GlobalOptions) and obj.fleet is not None) or os.environ.get(FLEET_ENV):
        return context_from(ctx)
    if fleet_name:
        return Context.for_fleet_name(fleet_name)
    if os.environ.get(FLEET_NAME_ENV):
        return context_from(ctx)
    raise BayError(
        "no fleet found",
        hint="Run inside an app repo whose bay.toml names its fleet, pass --fleet <path> "
        "before the verb, or set BAY_FLEET_NAME.",
    )


def resolve(ctx: typer.Context, project: str | None) -> tuple[Any, Path | None]:
    """The project (``plan.ProjectRef``) and the repo the command ran in, if any."""
    from bay_cli import plan as planmod

    here = find_bay_toml(Path.cwd())
    name, fleet_name = _read_identity(here) if here else (None, None)
    if project is None:
        if here is None or name is None:
            raise BayError(
                "no bay.toml here or above",
                hint="Run inside the app repo, or pass --project <name>.",
            )
        project = name
    cx = fleet_context(ctx, fleet_name)
    proj = planmod.load_project(cx, project, expect_fleet=fleet_name if name == project else None)
    return proj, (here.parent if here and name == project else None)


def _echo_json(doc: Any) -> None:
    typer.echo(json.dumps(doc, indent=2))


def _say(as_json: bool) -> Any:
    return (lambda _m: None) if as_json else (lambda m: console.info(m))


# ── verbs ───────────────────────────────────────────────────────────────────


def init(
    ctx: typer.Context,
    name: Annotated[
        str | None, typer.Option("--name", help="Project name. Default: the repo dir.")
    ] = None,
    fleet: Annotated[
        str | None,
        typer.Option("--fleet", help="Fleet name (~/.config/bay/fleets/<name>)."),
    ] = None,
    box: Annotated[
        str | None, typer.Option("--box", help="Box. Default: the fleet's default box.")
    ] = None,
    domain: Annotated[
        str | None, typer.Option("--domain", help="Domain. Default: <name>.<fleet domain>.")
    ] = None,
    as_json: _JsonOpt = False,
) -> None:
    """Draft a bay.toml in this app repo and register the app in the fleet.

    Writes bay.toml here and projects/<name>.lock in the fleet, then commits
    the fleet repo ("bay: init <name>"). Refuses when the name exists.

    Examples:

        bay init --fleet myfleet
        bay init --name shop --box eu-1 --domain shop.example.com
    """
    from bay_cli.project_init import init_project

    cx = fleet_context(ctx, fleet)
    result = init_project(cx, Path.cwd(), name=name, box=box, domain=domain)
    if as_json:
        _echo_json(result)
        return
    console.success(f"wrote {result['bay_toml']}")
    console.success(
        f"registered {result['project']} in fleet {result['fleet']} "
        f"(commit {result['fleet_commit'][:12]})"
    )
    for warning in result["warnings"]:
        console.warning(warning)
    console.info("Next: check bay.toml, commit it, then run `bay plan`.")


def plan(
    ctx: typer.Context,
    env: Annotated[
        str | None, typer.Argument(help="Environment. Default: the primary one.")
    ] = None,
    project: _ProjectOpt = None,
    as_json: _JsonOpt = False,
    at: Annotated[
        str | None, typer.Option("--at", help="Plan this commit instead of HEAD.")
    ] = None,
    plan_id: Annotated[
        str | None, typer.Option("--plan-id", help="Check a saved plan: is it stale?")
    ] = None,
    remote: Annotated[
        bool,
        typer.Option("--remote", help="Also run the deploy in check mode on the box."),
    ] = False,
    no_remote: _NoRemoteOpt = False,
    allow_unsupported: _AllowUnsupportedOpt = False,
) -> None:
    """Compare WANTED (bay.toml at HEAD), PINNED (the lock) and RUNNING (the box).

    Prints the steps, the risk of each and a verdict, and saves the plan to
    <fleet>/plans/<plan-id>.json. Exit code: 0 auto, 10 approve, 20 blocked,
    30 stale.

    Examples:

        bay plan
        bay plan staging --json
        bay plan --plan-id 3f2a9c0d1e2b
    """
    from bay_cli import plan as planmod

    proj, cwd_repo = resolve(ctx, project)
    opts = planmod.PlanOptions(
        env=env,
        at=at,
        read_running=not no_remote,
        box_check=remote,
        allow_unsupported=allow_unsupported,
        cwd_repo=cwd_repo,
    )
    if plan_id:
        result = planmod.recheck(proj, planmod.load_saved(proj.cx, plan_id), opts)
    else:
        result = planmod.make_plan(proj, opts)
    planmod.save(proj.cx, result)
    if as_json:
        _echo_json(result)
    else:
        typer.echo(planmod.render(result))
    raise typer.Exit(int(result["exit_code"]))


def approve(
    ctx: typer.Context,
    plan_id: Annotated[str, typer.Argument(help="The plan id that bay plan printed.")],
    reason: Annotated[str, typer.Option("--reason", help="Why the risky steps are fine.")] = "",
) -> None:
    """Approve a saved plan with destructive or shared steps.

    Writes <fleet>/plans/<plan-id>.approved with the plan hash and the
    reason. bay up then applies that exact plan. No prompt.

    Example:

        bay approve 3f2a9c0d1e2b --reason "the old volume is backed up"
    """
    from bay_cli import plan as planmod

    here = find_bay_toml(Path.cwd())
    _, fleet_name = _read_identity(here) if here else (None, None)
    cx = fleet_context(ctx, fleet_name)
    path = planmod.approve(cx, plan_id, reason)
    console.success(f"approved plan {plan_id} ({path.name})")


def _apply(
    ctx: typer.Context,
    which: str,
    env: str | None,
    project: str | None,
    at: str | None,
    plan_id: str | None,
    force: bool,
    reason: str | None,
    allow_unsupported: bool,
    as_json: bool,
) -> None:
    from bay_cli import apply as applymod
    from bay_cli import plan as planmod

    proj, cwd_repo = resolve(ctx, project)
    opts = planmod.PlanOptions(
        env=env, at=at, allow_unsupported=allow_unsupported, cwd_repo=cwd_repo
    )
    say = _say(as_json)
    try:
        if which == "rollback":
            result = applymod.rollback(proj, opts, force=force, reason=reason, echo=say)
        else:
            result = applymod.up(proj, opts, plan_id=plan_id, force=force, reason=reason, echo=say)
    except applymod.Refused as exc:
        if as_json:
            _echo_json(exc.plan)
        else:
            typer.echo(planmod.render(exc.plan))
            console.error(str(exc))
        raise typer.Exit(exc.exit_code) from None
    except applymod.DeployFailed as exc:
        if as_json:
            _echo_json(exc.result)
        else:
            console.error(
                f"{exc}. The fleet pins {exc.result['commit'][:12]}; bay show says "
                "HALF until a deploy succeeds."
            )
        raise typer.Exit(1) from None
    if as_json:
        _echo_json(result)
        return
    previous = result["previous_commit"]
    console.success(
        f"{result['action']} {result['project']} {result['env']}: "
        f"{(previous or 'none')[:12]} -> {result['commit'][:12]}, "
        f"{len(result['steps'])} step(s), fleet commit {result['receipt_commit'][:12]}"
    )


def up(
    ctx: typer.Context,
    env: Annotated[
        str | None, typer.Argument(help="Environment. Default: the primary one.")
    ] = None,
    project: _ProjectOpt = None,
    at: Annotated[str | None, typer.Option("--at", help="Pin this commit instead of HEAD.")] = None,
    plan_id: Annotated[
        str | None, typer.Option("--plan-id", help="Apply this saved plan, if it is not stale.")
    ] = None,
    force: Annotated[
        bool,
        typer.Option("--force", help="Apply an approve plan without approval (needs --reason)."),
    ] = False,
    reason: Annotated[str | None, typer.Option("--reason", help="Why --force.")] = None,
    allow_unsupported: _AllowUnsupportedOpt = False,
    as_json: _JsonOpt = False,
) -> None:
    """Pin the project's commit in the fleet and deploy it.

    Plans first and refuses a blocked (exit 20) or stale (exit 30) plan, and
    an approve plan (exit 10) without `bay approve`. Then writes the lock,
    compiles, commits the fleet repo, deploys and records the receipt.

    Examples:

        bay up
        bay up staging --at 1a2b3c4
        bay up --plan-id 3f2a9c0d1e2b
    """
    _apply(ctx, "up", env, project, at, plan_id, force, reason, allow_unsupported, as_json)


def rollback(
    ctx: typer.Context,
    env: Annotated[
        str | None, typer.Argument(help="Environment. Default: the primary one.")
    ] = None,
    project: _ProjectOpt = None,
    force: Annotated[
        bool, typer.Option("--force", help="Roll back an approve plan without approval.")
    ] = False,
    reason: Annotated[str | None, typer.Option("--reason", help="Why --force.")] = None,
    allow_unsupported: _AllowUnsupportedOpt = False,
    as_json: _JsonOpt = False,
) -> None:
    """Return an environment to its previous pin and deploy it.

    Runs bay up with the commit the lock records as previous; the two pins
    swap, so a second rollback undoes the first. Refuses when there is no
    previous pin.
    """
    _apply(ctx, "rollback", env, project, None, None, force, reason, allow_unsupported, as_json)


def show(
    ctx: typer.Context,
    name: Annotated[str | None, typer.Argument(help="Project. Default: the bay.toml here.")] = None,
    as_json: _JsonOpt = False,
    no_remote: _NoRemoteOpt = False,
) -> None:
    """Print WANTED, PINNED and RUNNING for a project, and a status per environment.

    Status: ok, behind (the project is ahead of the pin), drift (the box
    differs from the pin), unknown (no receipt) or HALF (the last bay up
    failed).
    """
    from bay_cli import apply as applymod

    proj, _ = resolve(ctx, name)
    doc = applymod.show(proj, remote=not no_remote)
    if as_json:
        _echo_json(doc)
    else:
        typer.echo(applymod.render_show(doc))
