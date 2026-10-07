"""The daily verbs: ``bay init``, ``plan``, ``approve``, ``up``, ``rollback`` and ``show``.

Which project and which fleet:

* Inside an app repo, the ``bay.toml`` in the working directory or above it
  names the project (``name``) and the fleet (``fleet``). The fleet is
  ``~/.config/bay/fleets/<fleet>`` unless ``--fleet <path>`` (before the
  verb) or ``BAY_FLEET`` names a directory.
* ``--project <name>`` works from anywhere; the fleet then comes from
  ``--fleet``, ``BAY_FLEET`` or ``BAY_FLEET_NAME``.
* With no ``bay.toml`` here or above and no ``--project``, ``bay plan <env>``
  and ``bay up <env>`` cover the whole environment of the fleet: the one
  ``--fleet``, ``BAY_FLEET`` or ``BAY_FLEET_NAME`` names, or the fleet
  directory you stand in (a ``bay.fleet.toml`` here or above).

No verb asks a question. Secrets never travel on the command line.
Plan exit codes: 0 auto, 10 approve, 20 blocked, 30 stale.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import tomllib
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import IO, Annotated, Any

import typer

from bay_cli import console
from bay_cli.context import (
    FLEET_ENV,
    FLEET_NAME_ENV,
    SOURCE_CWD,
    Context,
    GlobalOptions,
    context_from,
)
from bay_cli.errors import BayError

_ProjectOpt = Annotated[
    str | None,
    typer.Option("--project", "-p", help="Project name. Default: the bay.toml here or above."),
]
_JsonOpt = Annotated[bool, typer.Option("--json", help="Print one JSON document.")]
_NoRemoteOpt = Annotated[bool, typer.Option("--no-remote", help="Do not read the box receipt.")]
_LogOpt = Annotated[
    Path | None,
    typer.Option(
        "--log",
        help="Append progress and deploy output to this file instead of the terminal.",
    ),
]
_NoPushOpt = Annotated[
    bool, typer.Option("--no-push", help="Do not push the fleet repo after the last commit.")
]
_DataOpt = Annotated[
    str | None,
    typer.Option(
        "--data",
        help="For a box move of a project with volumes or a database. keep: the new box "
        "starts empty and the data stays untouched on the old box. move is deferred.",
    ),
]
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


def fleet_dir_above(start: Path) -> Path | None:
    """The nearest directory at or above ``start`` that holds a ``bay.fleet.toml``."""
    from bay_cli.fleet import FLEET_FILE

    for directory in [start, *start.parents]:
        if (directory / FLEET_FILE).is_file():
            return directory
    return None


def fleet_context(
    ctx: typer.Context, fleet_name: str | None, *, allow_cwd: bool = False
) -> Context:
    """``--fleet <path>``, then ``BAY_FLEET``, then the fleet named in bay.toml, then
    ``BAY_FLEET_NAME``, then (with ``allow_cwd``) the fleet directory you stand in."""
    obj = ctx.find_root().obj
    if isinstance(obj, Context):
        return obj
    if (isinstance(obj, GlobalOptions) and obj.fleet is not None) or os.environ.get(FLEET_ENV):
        return context_from(ctx)
    if fleet_name:
        return Context.for_fleet_name(fleet_name)
    if os.environ.get(FLEET_NAME_ENV):
        return context_from(ctx)
    if allow_cwd:
        found = fleet_dir_above(Path.cwd())
        if found is not None:
            return Context._from_fleet_path(found, SOURCE_CWD)
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
    cx = fleet_context(ctx, fleet_name, allow_cwd=True)
    proj = planmod.load_project(
        cx, project, expect_fleet=fleet_name if name == project else None, cwd=Path.cwd()
    )
    return proj, (here.parent if here and name == project else None)


def whole_env_fleet(
    ctx: typer.Context, project: str | None, at: str | None = None
) -> Context | None:
    """The fleet for a whole-environment ``plan`` or ``up``, or None for one project.

    One project when ``--project`` is given or a bay.toml with a ``name`` is
    here or above (an app repo, or a project folder of a fleet). Otherwise
    the fleet must be known: ``--fleet``, ``BAY_FLEET``, ``BAY_FLEET_NAME`` or
    the fleet directory you stand in.
    """
    if project is not None:
        return None
    here = find_bay_toml(Path.cwd())
    if here is not None and _read_identity(here)[0] is not None:
        return None
    try:
        cx = fleet_context(ctx, None, allow_cwd=True)
    except BayError:
        raise BayError(
            "no bay.toml here or above, and no fleet",
            hint="Run inside the app repo, pass --project <name>, or pass --fleet <path> "
            "(or stand in the fleet directory) to plan the whole environment.",
        ) from None
    if at is not None:
        raise BayError(
            "--at names one project's commit; a whole-environment plan reads each "
            "project at its own WANTED commit",
            hint="Pass --project <name> with --at.",
        )
    return cx


def _echo_json(doc: Any) -> None:
    typer.echo(json.dumps(doc, indent=2))


@contextlib.contextmanager
def routed_output(as_json: bool, log: Path | None) -> Iterator[Callable[[str], None]]:
    """Keep stdout for the one result document.

    With ``--log``, progress lines and everything the deploy prints (Ansible
    included, stdout AND stderr) are appended to that file. Otherwise, with
    ``--json``, they go to stderr. Otherwise nothing is redirected.

    A child process gets ``sys.stdout`` and ``sys.stderr`` as its handles
    (``runner.run`` passes them), so both Python streams point at the target.
    Descriptors 1 (and 2 with ``--log``) are redirected too when the target
    is a real file, so a child that only inherits them follows as well.

    Yields the function that prints one progress line.
    """
    if log is None and not as_json:
        yield lambda m: console.info(m)
        return
    handle: IO[str] = open(log, "a", encoding="utf-8") if log is not None else sys.stderr
    fds = (1, 2) if log is not None else (1,)
    saved: dict[int, int] = {}
    try:
        target_fd = handle.fileno()
        sys.stdout.flush()
        sys.stderr.flush()
        for fd in fds:
            saved[fd] = os.dup(fd)
            os.dup2(target_fd, fd)
    except (OSError, ValueError, AttributeError):  # io.UnsupportedOperation is both
        for fd, copy in saved.items():
            os.dup2(copy, fd)
            os.close(copy)
        saved = {}

    def say(message: str) -> None:
        print(message, file=handle, flush=True)

    try:
        with contextlib.ExitStack() as stack:
            stack.enter_context(contextlib.redirect_stdout(handle))
            if log is not None:
                stack.enter_context(contextlib.redirect_stderr(handle))
            yield say
    finally:
        handle.flush()
        for fd, copy in saved.items():
            os.dup2(copy, fd)
            os.close(copy)
        if log is not None:
            handle.close()


def _json_error(exc: BayError) -> None:
    _echo_json({"error": str(exc), "hint": exc.hint, "code": exc.code.value})
    raise typer.Exit(exc.exit_code)


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
    toml_path: Annotated[
        str | None,
        typer.Option(
            "--toml-path",
            help="Where bay.toml goes, relative to the repo root (a monorepo). "
            "Default: bay.toml at the repo root.",
        ),
    ] = None,
    as_json: _JsonOpt = False,
) -> None:
    """Draft a bay.toml in this app repo and register the app in the fleet.

    Writes bay.toml here and projects/<name>/bay.lock in the fleet, then
    commits the fleet repo ("bay: init <name>"). Refuses when the name exists.
    The lock records the repo's origin URL and the toml path, never a path on
    this machine.

    Examples:

        bay init --fleet myfleet
        bay init --name shop --box eu-1 --domain shop.example.com
        bay init --name api --toml-path services/api/bay.toml
    """
    from bay_cli.project_init import init_project

    cx = fleet_context(ctx, fleet)
    result = init_project(
        cx, Path.cwd(), name=name, box=box, domain=domain, toml_path=toml_path
    )
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
    data: _DataOpt = None,
    log: _LogOpt = None,
) -> None:
    """Compare WANTED (bay.toml at HEAD), PINNED (the lock) and RUNNING (the box).

    Prints the steps, the risk of each and a verdict, and saves the plan to
    <fleet>/plans/<plan-id>.json. Exit code: 0 auto, 10 approve, 20 blocked,
    30 stale.

    In a fleet directory (or with --fleet and no bay.toml here), with no
    --project, the plan covers every project of the environment.

    Examples:

        bay plan
        bay plan staging --json
        bay plan --plan-id 3f2a9c0d1e2b
        bay --fleet ~/fleets/prod plan production
    """
    from bay_cli import plan as planmod

    try:
        planmod.check_data_mode(data)
        with routed_output(as_json, log):
            env_cx = whole_env_fleet(ctx, project, at)
            saved = planmod.load_saved(env_cx, plan_id) if env_cx and plan_id else None
            if env_cx is not None and saved is not None and saved.get("project"):
                project = str(saved["project"])  # a one-project plan, checked from the fleet
                env_cx = None
            if env_cx is not None:
                opts = planmod.PlanOptions(
                    env=env,
                    read_running=not no_remote,
                    box_check=remote,
                    allow_unsupported=allow_unsupported,
                    data=data,
                )
                if saved is not None:
                    result = planmod.recheck_env(env_cx, saved, opts, cwd=Path.cwd())
                else:
                    result = planmod.make_env_plan(env_cx, opts, cwd=Path.cwd())
                planmod.save(env_cx, result)
            else:
                proj, cwd_repo = resolve(ctx, project)
                opts = planmod.PlanOptions(
                    env=env,
                    at=at,
                    read_running=not no_remote,
                    box_check=remote,
                    allow_unsupported=allow_unsupported,
                    cwd_repo=cwd_repo,
                    data=data,
                )
                if plan_id:
                    result = planmod.recheck(proj, planmod.load_saved(proj.cx, plan_id), opts)
                else:
                    result = planmod.make_plan(proj, opts)
                planmod.save(proj.cx, result)
    except BayError as exc:
        if not as_json:
            raise
        _json_error(exc)
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
    cx = fleet_context(ctx, fleet_name, allow_cwd=True)
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
    push: bool,
    log: Path | None,
    data: str | None = None,
) -> None:
    from bay_cli import apply as applymod
    from bay_cli import plan as planmod

    try:
        planmod.check_data_mode(data)
        with routed_output(as_json, log) as say:
            common: dict[str, Any] = {"force": force, "reason": reason, "echo": say, "push": push}
            env_cx = whole_env_fleet(ctx, project, at) if which == "up" else None
            if env_cx is not None and plan_id:
                saved = planmod.load_saved(env_cx, plan_id)
                if saved.get("project"):
                    project, env_cx = str(saved["project"]), None
            if env_cx is not None:
                opts = planmod.PlanOptions(
                    env=env, allow_unsupported=allow_unsupported, data=data
                )
                result = applymod.up_env(
                    env_cx, opts, plan_id=plan_id, cwd=Path.cwd(), **common
                )
            else:
                proj, cwd_repo = resolve(ctx, project)
                opts = planmod.PlanOptions(
                    env=env,
                    at=at,
                    allow_unsupported=allow_unsupported,
                    cwd_repo=cwd_repo,
                    data=data,
                )
                if which == "rollback":
                    # For a rollback, `at` is --to: the code commit to roll back to.
                    result = applymod.rollback(proj, opts, to=at, **common)
                else:
                    result = applymod.up(proj, opts, plan_id=plan_id, **common)
    except BayError as exc:
        if not as_json:
            raise
        _json_error(exc)
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
            pins = (
                f"the fleet pins {exc.result['commit'][:12]}"
                if exc.result.get("commit")
                else f"the fleet pins the planned commits of {len(exc.result['projects'])} projects"
            )
            console.error(f"{exc}. {pins}; bay show says HALF until a deploy succeeds.")
        raise typer.Exit(1) from None
    if as_json:
        _echo_json(result)
        return
    previous = result["previous_commit"]
    what = (
        f"{result['project']} {result['env']}: "
        f"{(previous or 'none')[:12]} -> {result['commit'][:12]}"
        if result.get("project")
        else f"{result['env']} (whole environment, {len(result['projects'])} projects)"
    )
    console.success(
        f"{result['action']} {what}, "
        f"{len(result['steps'])} step(s) planned, {len(result.get('applied') or [])} "
        f"container change(s) applied, fleet commit {result['receipt_commit'][:12]}"
        + (", pushed" if result["pushed"] else "")
    )
    planned = set(result.get("projects") or [result["project"]])
    others = [r for r in result.get("pinned") or [] if r["project"] not in planned]
    if others:
        console.info(
            f"also pinned {len(others)} other project env(s) on {result['box_env']}: "
            + ", ".join(f"{r['project']} {r['env']}" for r in others)
        )
    for note in result.get("notes") or []:
        console.info(f"note: {note}")
    if result["push_error"]:
        console.warning(f"the fleet repo was not pushed: {result['push_error']}")
    if result.get("push_skipped"):
        console.warning(str(result["push_skipped"]))


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
    data: _DataOpt = None,
    as_json: _JsonOpt = False,
    no_push: _NoPushOpt = False,
    log: _LogOpt = None,
) -> None:
    """Pin the project's commit in the fleet and deploy it.

    Plans first and refuses a blocked (exit 20) or stale (exit 30) plan, and
    an approve plan (exit 10) without `bay approve`. Then writes the lock,
    compiles, commits the fleet repo, deploys, records the receipt, prunes
    old records from plans/ and pushes the fleet repo (not with --no-push; a
    failed push is only a warning).

    In a fleet directory (or with --fleet and no bay.toml here), with no
    --project, it applies a whole-environment plan: every project of the
    environment at its WANTED commit.

    Examples:

        bay up
        bay up staging --at 1a2b3c4
        bay up --plan-id 3f2a9c0d1e2b
        bay up production --data keep
    """
    _apply(
        ctx,
        "up",
        env,
        project,
        at,
        plan_id,
        force,
        reason,
        allow_unsupported,
        as_json,
        not no_push,
        log,
        data,
    )


def rollback(
    ctx: typer.Context,
    env: Annotated[
        str | None, typer.Argument(help="Environment. Default: the primary one.")
    ] = None,
    project: _ProjectOpt = None,
    to: Annotated[
        str | None,
        typer.Option(
            "--to", help="Roll the code back to this commit; its image must be on the box."
        ),
    ] = None,
    force: Annotated[
        bool, typer.Option("--force", help="Roll back an approve plan without approval.")
    ] = False,
    reason: Annotated[str | None, typer.Option("--reason", help="Why --force.")] = None,
    allow_unsupported: _AllowUnsupportedOpt = False,
    data: _DataOpt = None,
    as_json: _JsonOpt = False,
    no_push: _NoPushOpt = False,
    log: _LogOpt = None,
) -> None:
    """Return an environment to its previous pin and its previous code, and freeze it.

    Runs bay up with the commit the lock records as previous; the two pins
    swap, so a second rollback undoes the first. The box points the image
    back at what it ran before the last bay up. The environment is frozen:
    a push builds and tags its image but does not deploy, until a bay up to
    a newer commit. Refuses when there is no previous pin.

    With --to <commit>, the code goes back to that commit's image, which must
    be on the box (the refusal lists the commit tags it has).

    Examples:

        bay rollback
        bay rollback --to 1a2b3c4d5e6f
    """
    _apply(
        ctx,
        "rollback",
        env,
        project,
        to,
        None,
        force,
        reason,
        allow_unsupported,
        as_json,
        not no_push,
        log,
        data,
    )


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

    try:
        proj, _ = resolve(ctx, name)
        doc = applymod.show(proj, remote=not no_remote)
    except BayError as exc:
        if not as_json:
            raise
        _json_error(exc)
    if as_json:
        _echo_json(doc)
    else:
        typer.echo(applymod.render_show(doc))
