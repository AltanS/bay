"""The daily verbs: ``bay init``, ``plan``, ``approve``, ``up``, ``rollback``, ``show``, ``adopt``.

``bay adopt`` is an alias of ``bay toml move <name> --to repo`` (since 2.17.0).

Which project and which fleet:

* Inside an app repo, the ``bay.toml`` in the working directory or above it
  names the project (``name``) and the fleet (``fleet``). The fleet is
  ``~/.config/bay/fleets/<fleet>`` unless ``--fleet <path>`` (before the
  verb) or ``BAY_FLEET`` names a directory.
* ``--project <name>`` works from anywhere; the fleet then comes from
  ``--fleet``, ``BAY_FLEET`` or ``BAY_FLEET_NAME``.
* With no ``bay.toml`` here or above and no ``--project``, ``bay plan <env>``
  and ``bay up <env>`` cover the whole environment of the fleet: the one
  ``--fleet``, ``BAY_FLEET`` or ``BAY_FLEET_NAME`` names, the fleet
  directory you stand in (a ``bay.fleet.toml`` here or above), or the only
  fleet nearby (step 5b).

No verb asks a question. Secrets never travel on the command line.
Plan exit codes: 0 auto, 10 approve, 20 blocked, 30 stale. ``bay up`` also
exits 40 when the deploy failed only for a first image, 41 when the fleet is
not in step with its remote (diverged, unreachable), 42 when its claim push
was refused, 43 when another run holds the fleet, and 44 when it deployed but
could not push the receipt.
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
    Context,
    GlobalOptions,
    context_from,
    fleet_dir_above,
)
from bay_cli.errors import (
    REASON_FLEET_AMBIGUOUS,
    REASON_NO_FLEET,
    BayError,
    FleetSelectionError,
)

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
_TakeOverOpt = Annotated[
    bool,
    typer.Option(
        "--take-over",
        help="Deploy although another run claimed this box env less than 30 minutes ago "
        "(env_busy). Only for a run you know is dead.",
    ),
]
_AllowDirtyOpt = Annotated[
    bool,
    typer.Option(
        "--allow-dirty",
        help="Deploy although the fleet has uncommitted files. Ansible reads them, and no "
        "other machine can see them.",
    ),
]
_AllowOlderBayOpt = Annotated[
    bool,
    typer.Option(
        "--allow-older-bay",
        help="Deploy although the fleet was last deployed with a newer Bay (bay_older). "
        "A deliberate downgrade: bay.compat.json then records this older version.",
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
_AllBoxesOpt = Annotated[
    bool,
    typer.Option(
        "--all-boxes",
        help="With --project: run on every box of the box environment, as before 2.13. "
        "Without it, a project run deploys only the boxes it needs (docs/plan.md, "
        '"Which boxes a run touches").',
    ),
]
_ForceCodeOpt = Annotated[
    bool,
    typer.Option(
        "--force-code",
        help=(
            'track = "branch": move the code to the pin even when Bay cannot tell '
            "whether it is older than what runs (a destructive step)."
        ),
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


def fleet_context(
    ctx: typer.Context, fleet_name: str | None, *, allow_cwd: bool = False
) -> Context:
    """``--fleet <path>``, then ``BAY_FLEET``, then the fleet named in bay.toml, then
    ``BAY_FLEET_NAME``, then (with ``allow_cwd``) the fleet you stand in or the one
    fleet nearby (discovery step 5)."""
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
        nearby = Context._from_nearby_folders()
        if nearby is not None:
            return nearby
    raise FleetSelectionError(
        "no fleet found",
        reason=REASON_NO_FLEET,
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
    the fleet must be known: ``--fleet``, ``BAY_FLEET``, ``BAY_FLEET_NAME``,
    the fleet directory you stand in, or the only fleet nearby.
    """
    if project is not None:
        return None
    here = find_bay_toml(Path.cwd())
    if here is not None and _read_identity(here)[0] is not None:
        return None
    try:
        cx = fleet_context(ctx, None, allow_cwd=True)
    except FleetSelectionError as exc:
        if exc.reason == REASON_FLEET_AMBIGUOUS:
            raise  # name the fleets it found
        raise FleetSelectionError(
            "no bay.toml here or above, and no fleet",
            reason=REASON_NO_FLEET,
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
    from bay_cli import fleetsync

    handle: IO[str] = open(log, "a", encoding="utf-8") if log is not None else sys.stderr
    fds = (1, 2) if log is not None else (1,)
    saved: dict[int, int] = {}
    terminal: IO[str] | None = sys.stderr if log is not None else None
    try:
        on_fd2 = sys.stderr.fileno() == 2
    except (OSError, ValueError, AttributeError):
        on_fd2 = False
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
    owned = 2 in saved and on_fd2
    if owned:
        # sys.stderr wrote to fd 2, which is the log now: the terminal is the saved copy.
        terminal = os.fdopen(os.dup(saved[2]), "w", encoding="utf-8")
    if terminal is not None:
        fleetsync.TERMINAL.append(terminal)

    def say(message: str) -> None:
        print(message, file=handle, flush=True)

    try:
        with contextlib.ExitStack() as stack:
            stack.enter_context(contextlib.redirect_stdout(handle))
            if log is not None:
                stack.enter_context(contextlib.redirect_stderr(handle))
            yield say
    finally:
        if terminal is not None:
            fleetsync.TERMINAL.remove(terminal)
            if owned:
                terminal.close()
        handle.flush()
        for fd, copy in saved.items():
            os.dup2(copy, fd)
            os.close(copy)
        if log is not None:
            handle.close()


def _json_error(exc: BayError, extra: dict[str, Any] | None = None) -> None:
    doc: dict[str, Any] = {"error": str(exc), "hint": exc.hint, "code": exc.code.value}
    if isinstance(exc, FleetSelectionError):
        doc.update(exc.json_doc())
    if extra is not None:
        doc.update(extra)
    _echo_json(doc)
    raise typer.Exit(exc.exit_code)


def _error_outcome(ctx: typer.Context, exc: BaseException) -> dict[str, Any]:
    """``reason``, ``box_touched``, ``next`` and ``fleet`` for a JSON error of up and rollback."""
    from bay_cli import apply as applymod

    try:
        here = find_bay_toml(Path.cwd())
        _, fleet_name = _read_identity(here) if here else (None, None)
        cx: Context | None = fleet_context(ctx, fleet_name, allow_cwd=True)
    except BayError:
        cx = None
    doc = applymod.outcome(
        cx,
        reason=getattr(exc, "reason", None) or "error",
        box_touched=bool(getattr(exc, "box_touched", False)),
        next_cmd=getattr(exc, "next_cmd", None),
    )
    undo_failed = getattr(exc, "undo_failed", None)
    if undo_failed is not None:
        # A refused claim undid its commit; these paths could not be put back.
        doc["undo_failed"] = list(undo_failed)
    return doc


def _plan_pull(cx: Context) -> Any:
    """``bay plan``: fast-forward a fleet that is only behind, under the run lock.

    When another run holds the lock, skip the pull: the plan then says the
    fleet is behind, as before 2.12.
    """
    from bay_cli import fleetsync

    try:
        with fleetsync.run_lock(cx.fleet_root, "plan"):
            return fleetsync.pull_if_behind(cx.fleet_root)
    except fleetsync.SyncError as exc:
        if exc.reason != fleetsync.REASON_BUSY:
            raise
        fleetsync.notice(f"fleet: not pulled, {exc}")
        return None


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

    here = fleet_dir_above(Path.cwd())
    if here is not None:
        raise BayError(
            f"this is a fleet folder ({here}); bay init drafts a bay.toml in an app repo",
            hint="For an app that lives in the fleet, write projects/<name>/bay.toml "
            "(see docs/layout-scenarios.md).",
        )
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


#: The note ``bay adopt`` prints on stderr: the verb is an alias now.
ADOPT_NOTE = "note: bay adopt is now `bay toml move {name} --to repo`"


def adopt(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="The project that lives in the fleet.")],
    toml_path: Annotated[
        str | None,
        typer.Option(
            "--toml-path",
            help="Where bay.toml goes, relative to the repo root (a monorepo). "
            "Default: bay.toml at the repo root.",
        ),
    ] = None,
    check: Annotated[
        bool, typer.Option("--check", help="Print the files and the lock change; change nothing.")
    ] = False,
    as_json: _JsonOpt = False,
) -> None:
    """Deprecated: use `bay toml move <name> --to repo`. It works the same.

    Moves a project's bay.toml and its files from the fleet into this app
    repo. Run it inside a checkout of the app repo, with the fleet named by
    --fleet, BAY_FLEET or BAY_FLEET_NAME. Prints one note line on stderr.

    Examples:

        bay --fleet ~/fleets/prod adopt shop
        bay --fleet ~/fleets/prod toml move shop --to repo
    """
    typer.echo(ADOPT_NOTE.format(name=name), err=True)
    run_adopt(ctx, name, toml_path=toml_path, check=check, as_json=as_json)


def run_adopt(
    ctx: typer.Context, name: str, *, toml_path: str | None, check: bool, as_json: bool
) -> None:
    """``bay toml move <name> --to repo`` (and its alias ``bay adopt``): run and print."""
    from bay_cli import adopt as adoptmod

    try:
        cx = fleet_context(ctx, None)
        result = adoptmod.adopt(cx, Path.cwd(), name, toml_path=toml_path, check=check)
    except BayError as exc:
        if not as_json:
            raise
        _json_error(exc)
    if as_json:
        _echo_json(result)
        return
    verb = "would copy" if check else "copied"
    for item in result["files"]:
        console.info(f"{verb} {item['from']} -> {item['to']}")
    gone = "would remove" if check else "removed"
    for path in result["removed"]:
        console.info(f"{gone} {path} from the fleet")
    for path in result["kept"]:
        console.info(f"kept {path} in the fleet: another fleet entry reads it")
    typer.echo("\n".join(result["lock_diff"]))
    if check:
        console.info("check only: nothing changed")
        return
    console.success(
        f"app commit {result['app_commit'][:12]} in {result['repo_root']} (local: push it "
        "after bay up)"
    )
    console.success(f"fleet commit {result['fleet_commit'][:12]}: {name} adopted")
    plan_cmd, up_cmd, push = result["next"]
    console.info(
        f"Next, in this app repo: now run `{plan_cmd}` (it must show 0 steps), then "
        f"`{up_cmd}` (it takes the unpushed adopt commit and moves no code)."
    )
    console.info(
        f"Then: `{push}`. The box sees a config-only push and builds nothing. "
        "Pushed before bay up, the box would rebuild and recreate the app."
    )


def run_move_to_fleet(ctx: typer.Context, name: str, *, check: bool, as_json: bool) -> None:
    """``bay toml move <name> --to fleet``: run and print."""
    from bay_cli import fleetsync, tomlmove

    try:
        here = find_bay_toml(Path.cwd())
        _, fleet_name = _read_identity(here) if here else (None, None)
        cx = fleet_context(ctx, fleet_name, allow_cwd=True)
        result = tomlmove.move_to_fleet(cx, Path.cwd(), name, check=check)
    except BayError as exc:
        if not as_json:
            if isinstance(exc, fleetsync.SyncError):
                # The exit code of bay up for the same refusal (41, 43, 45).
                console.error(str(exc))
                if exc.hint:
                    console.info(f"Hint: {exc.hint}")
                raise typer.Exit(exc.exit_code) from None
            raise
        extra = {"reason": exc.reason} if isinstance(exc, fleetsync.SyncError) else None
        _json_error(exc, extra)
    if as_json:
        _echo_json(result)
        return
    verb = "would copy" if check else "copied"
    for item in result["files"]:
        console.info(f"{verb} {item['from']} (app repo) -> {item['to']}")
    for path in result["kept"]:
        console.info(f"kept {path} in the fleet: the compile reads it there")
    for note in result["notes"]:
        console.info(f"note: {note}")
    typer.echo("\n".join(result["lock_diff"]))
    if check:
        console.info("check only: nothing changed")
        return
    console.success(
        f"fleet commits {result['fleet_commit'][:12]} (the move) and "
        f"{result['lock_commit'][:12]} (the lock): {name} lives in the fleet (local: "
        "bay up pushes them)"
    )
    plan_cmd, up_cmd, rm_cmd, commit_cmd, push = result["next"]
    console.info(
        f"Next: run `{plan_cmd}` (it must show 0 steps), then `{up_cmd}` (it moves no code)."
    )
    console.info(
        f"Then, in the app repo: `{rm_cmd}`, `{commit_cmd}`, `{push}`. That push is a normal "
        "code push. Pushed before bay up, the box builds it and holds it."
    )


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
    force_code: _ForceCodeOpt = False,
    data: _DataOpt = None,
    log: _LogOpt = None,
    all_boxes: _AllBoxesOpt = False,
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
            removal = _saved_remove(ctx, plan_id) if plan_id else None
            if removal is not None:
                result = _plan_remove(removal)
                raise _Done
            env_cx = whole_env_fleet(ctx, project, at)
            pulled = _plan_pull(env_cx) if env_cx is not None else None
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
                result["fleet_sync"] = pulled.to_dict() if pulled else None
                planmod.save(env_cx, result)
            else:
                proj, cwd_repo = resolve(ctx, project)
                pulled = _plan_pull(proj.cx)
                if pulled is not None:
                    proj, cwd_repo = resolve(ctx, project)
                opts = planmod.PlanOptions(
                    env=env,
                    at=at,
                    read_running=not no_remote,
                    box_check=remote,
                    allow_unsupported=allow_unsupported,
                    cwd_repo=cwd_repo,
                    data=data,
                    force_code=force_code,
                    all_boxes=all_boxes,
                )
                if plan_id:
                    result = planmod.recheck(proj, planmod.load_saved(proj.cx, plan_id), opts)
                else:
                    result = planmod.make_plan(proj, opts)
                result["fleet_sync"] = pulled.to_dict() if pulled else None
                planmod.save(proj.cx, result)
    except _Done:
        pass
    except BayError as exc:
        if not as_json:
            raise
        _json_error(exc)
    if as_json:
        _echo_json(result)
    else:
        typer.echo(_render(result))
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
    force_code: bool = False,
    take_over: bool = False,
    allow_dirty: bool = False,
    all_boxes: bool = False,
    allow_older_bay: bool = False,
) -> None:
    from bay_cli import apply as applymod
    from bay_cli import plan as planmod

    removed: dict[str, Any] | None = None
    try:
        planmod.check_data_mode(data)
        with routed_output(as_json, log) as say:
            common: dict[str, Any] = {
                "force": force,
                "reason": reason,
                "echo": say,
                "push": push,
                "take_over": take_over,
                "allow_dirty": allow_dirty,
                "allow_older_bay": allow_older_bay,
            }
            removal = _saved_remove(ctx, plan_id) if which == "up" and plan_id else None
            if removal is not None:
                from bay_cli import remove as removemod

                removed = removemod.apply_remove(
                    removal[0], str(plan_id), env=env, cwd=Path.cwd(), **common
                )
                raise _Done
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
                    force_code=force_code,
                    all_boxes=all_boxes,
                )
                if which == "rollback":
                    # For a rollback, `at` is --to: the code commit to roll back to.
                    result = applymod.rollback(proj, opts, to=at, **common)
                else:
                    result = applymod.up(proj, opts, plan_id=plan_id, **common)
    except _Done:
        pass
    except BayError as exc:
        if not as_json:
            raise
        _json_error(exc, _error_outcome(ctx, exc))
    except applymod.Refused as exc:
        if as_json:
            doc = dict(exc.plan)
            doc.update(_error_outcome(ctx, BayError(str(exc))))
            doc["reason"] = exc.reason
            doc["next"] = exc.next_cmd or _refused_next(exc)
            _echo_json(doc)
        else:
            typer.echo(_render(exc.plan))
            console.error(str(exc))
        raise typer.Exit(exc.exit_code) from None
    except applymod.DeployFailed as exc:
        if as_json:
            _echo_json(exc.result)
        else:
            retry = _retry_deploy(exc.result)
            if exc.result.get("route_only"):
                console.error(
                    f"{exc}. No lock changed; "
                    + (retry or f"run bay up {exc.result['env']} again.")
                )
                raise typer.Exit(exc.exit_code) from None
            pins = (
                f"the fleet pins {exc.result['commit'][:12]}"
                if exc.result.get("commit")
                else f"the fleet pins the planned commits of {len(exc.result['projects'])} projects"
            )
            console.error(f"{exc}. {pins}; bay show says HALF until a deploy succeeds.")
            if retry:
                console.info(retry)
            if exc.exit_code == applymod.FIRST_IMAGE_EXIT:
                for note in exc.result.get("notes") or []:
                    if note.startswith("first deploy of "):
                        console.info(note)
        raise typer.Exit(exc.exit_code) from None
    except KeyboardInterrupt as exc:
        # Ctrl-C. Before the box: the unwind ran. After: the lock stays pending,
        # and the same command repairs the stopped run. With --json: one document.
        if not as_json:
            raise
        doc = _error_outcome(ctx, exc)
        doc.update({"error": "interrupted", "hint": None, "reason": "interrupted"})
        _echo_json(doc)
        raise typer.Exit(130) from None
    except Exception as exc:
        # Any other error (a lock that breaks its schema, an OSError): with --json
        # it is still one JSON document, never only a traceback.
        import click

        if not as_json or isinstance(exc, (click.exceptions.Exit, click.ClickException)):
            raise
        wrapped = BayError(f"{type(exc).__name__}: {exc}")
        doc = _error_outcome(ctx, wrapped)
        doc["box_touched"] = bool(getattr(exc, "box_touched", False))
        _json_error(wrapped, doc)
    from bay_cli import fleetsync

    unpushed = (removed or result).get("reason") == fleetsync.REASON_RECEIPT_UNPUSHED
    if removed is not None:
        _report_remove(removed, as_json)
        if unpushed:
            raise typer.Exit(fleetsync.EXIT_RECEIPT_UNPUSHED)
        return
    if as_json:
        _echo_json(result)
        if unpushed:
            raise typer.Exit(fleetsync.EXIT_RECEIPT_UNPUSHED)
        return
    previous = result["previous_commit"]
    what = (
        f"{result['project']} {result['env']}: "
        f"{(previous or 'none')[:12]} -> {result['commit'][:12]}"
        if result.get("project")
        else f"{result['env']} (routes only, no project pinned)"
        if result.get("route_only")
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
    if unpushed:
        raise typer.Exit(fleetsync.EXIT_RECEIPT_UNPUSHED)


def _refused_next(exc: Any) -> str | None:
    """The next command after a refused ``bay up``, when there is one obvious one."""
    from bay_cli import fleetsync

    plan = exc.plan
    if exc.reason == fleetsync.REASON_DIVERGED:
        return "bay fleet push"
    if exc.reason == fleetsync.REASON_DIRTY:
        return "bay fleet status"
    if exc.reason == fleetsync.REASON_ENV_BUSY:
        return "bay show"
    if exc.reason == fleetsync.REASON_BAY_OLDER:
        return f"bay self update --to v{(plan.get('bay_compat') or {}).get('deployed_with')}"
    if exc.reason == fleetsync.REASON_COMPAT_NEWER:
        return "bay self update"
    if exc.reason == "approve":
        return f'bay approve {plan["plan_id"]} --reason "<why>"'
    if exc.reason == "stale":
        return "bay plan"
    return None


def _retry_deploy(result: dict[str, Any]) -> str | None:
    """How to retry a failed up whose deploy ran extra tags, else None.

    The failed up already committed ``services.yml``, so a second ``bay up`` plans
    no step and would leave the extra tags (route, volume backup) out. The
    retry that runs them is ``bay deploy <env> --tags <the tags that up used>``.
    """
    from bay_cli.apply import UP_DEPLOY_TAGS

    tags = str(result.get("deploy_tags") or "")
    if tags in ("", UP_DEPLOY_TAGS):
        return None
    return (
        f"A second bay up plans no step, because services.yml is already committed, "
        f"and would skip the tags {tags}. Retry with: "
        f"bay deploy {result['box_env']} --tags {tags}"
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
    force_code: _ForceCodeOpt = False,
    data: _DataOpt = None,
    as_json: _JsonOpt = False,
    no_push: _NoPushOpt = False,
    log: _LogOpt = None,
    take_over: _TakeOverOpt = False,
    allow_dirty: _AllowDirtyOpt = False,
    all_boxes: _AllBoxesOpt = False,
    allow_older_bay: _AllowOlderBayOpt = False,
) -> None:
    """Pin the project's commit in the fleet and deploy it.

    Plans first and refuses a blocked (exit 20) or stale (exit 30) plan, and
    an approve plan (exit 10) without `bay approve`. Pulls the fleet first
    when it is only behind its remote. Then writes the lock, compiles,
    commits the fleet repo, pushes that commit (the claim; a refused push
    stops before the box, exit 42), deploys, records the receipt, prunes old
    records from plans/ and pushes the fleet repo again (exit 44 when that
    fails). --no-push skips both pushes. A failed deploy exits 1, or 40 when
    the only failure is a build container with no image yet (the first
    deploy of a build app: push so the webhook builds it, then bay up again).
    Exit 41: the fleet and its remote diverged, the remote is down, or the
    fleet has uncommitted files (--allow-dirty deploys them). Exit 43:
    another bay run holds this fleet checkout, or another machine claimed this
    box env less than 30 minutes ago (env_busy; --take-over deploys anyway).
    Exit 45: the fleet was last deployed with a newer Bay (bay_older; run
    `bay self update`, or --allow-older-bay for a deliberate downgrade), or
    its bay.compat.json cannot be read (compat_invalid, compat_newer).

    In a fleet directory (or with --fleet and no bay.toml here), with no
    --project, it applies a whole-environment plan: every project of the
    environment at its WANTED commit, on every box of the box environment.

    With a project, it deploys only the boxes the plan needs (the scope:
    the project's box, the old box of a move, the ingress box for a route
    change, the build server and webhook boxes when a build changes).
    --all-boxes deploys every box of the box environment.

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
        force_code=force_code,
        take_over=take_over,
        allow_dirty=allow_dirty,
        all_boxes=all_boxes,
        allow_older_bay=allow_older_bay,
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
    take_over: _TakeOverOpt = False,
    allow_dirty: _AllowDirtyOpt = False,
    all_boxes: _AllBoxesOpt = False,
    allow_older_bay: _AllowOlderBayOpt = False,
) -> None:
    """Return an environment to its previous pin and its previous code, and freeze it.

    Runs bay up with the commit the lock records as previous; the two pins
    swap, so a second rollback undoes the first. The box points the image
    back at the code the lock records for each build container before the
    last bay up (previous.containers). The environment is frozen:
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
        take_over=take_over,
        allow_dirty=allow_dirty,
        all_boxes=all_boxes,
        allow_older_bay=allow_older_bay,
    )


def show(
    ctx: typer.Context,
    name: Annotated[str | None, typer.Argument(help="Project. Default: the bay.toml here.")] = None,
    as_json: _JsonOpt = False,
    no_remote: _NoRemoteOpt = False,
    show_routes: Annotated[
        bool,
        typer.Option(
            "--routes",
            help="Show the fleet's tailnet routes instead: WANTED (bay.fleet.toml), "
            "PINNED (the compiled file) and RUNNING (the ingress box receipt).",
        ),
    ] = False,
) -> None:
    """Print WANTED, PINNED and RUNNING for a project, and a status per environment.

    Status: ok, ahead (the box runs WANTED, a push deployed it, and the pin
    is behind), behind (the project is ahead of the pin and the box does not
    run it yet), drift (the box differs from the pin), unknown (no receipt)
    or HALF (the last bay up failed). RUNNING names the code each build
    container runs. With --routes: the same for every tailnet route of the
    fleet.
    """
    from bay_cli import apply as applymod

    if show_routes:
        _show_routes(ctx, as_json, no_remote)
        return
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


def _show_routes(ctx: typer.Context, as_json: bool, no_remote: bool) -> None:
    """``bay show --routes``: the fleet's tailnet routes, WANTED against PINNED and RUNNING."""
    from bay_cli import plan as planmod
    from bay_cli import routes

    here = find_bay_toml(Path.cwd())
    _, fleet_name = _read_identity(here) if here else (None, None)
    try:
        cx = fleet_context(ctx, fleet_name)
        fleet = planmod.load_fleet_doc(cx)
        pinned, _state = planmod.current_services(cx)
        entries = None
        env = routes.ingress_env(fleet)
        if not no_remote and env is not None and routes.table(fleet):
            try:
                entries = planmod.default_receipt_reader(cx, env)
            except (BayError, OSError) as exc:
                console.warning(f"cannot read the receipt of {env}: {exc}")
        doc = routes.show_routes(fleet, pinned, entries, cx.fleet_root)
    except BayError as exc:
        if not as_json:
            raise
        _json_error(exc)
    if as_json:
        _echo_json(doc)
    else:
        typer.echo(routes.render_show(doc))


# ── bay remove plans (bay plan / bay up --plan-id) ──────────────────────────


class _Done(Exception):
    """Leaves the redirected output block once a remove plan was applied."""


def _saved_remove(ctx: typer.Context, plan_id: str) -> tuple[Context, dict[str, Any]] | None:
    """``(fleet, saved plan)`` when ``plan_id`` names a ``bay remove`` plan, else None."""
    from bay_cli import plan as planmod

    try:
        here = find_bay_toml(Path.cwd())
        _, fleet_name = _read_identity(here) if here else (None, None)
        cx = fleet_context(ctx, fleet_name, allow_cwd=True)
        saved = planmod.load_saved(cx, plan_id)
    except BayError:
        return None
    return (cx, saved) if saved.get("remove") else None


def _plan_remove(found: tuple[Context, dict[str, Any]]) -> dict[str, Any]:
    """``bay plan --plan-id`` for a remove plan: plan it again, say whether it is stale."""
    from bay_cli import plan as planmod
    from bay_cli import remove as removemod

    cx, saved = found
    result = removemod.recheck_remove(cx, saved, cwd=Path.cwd())
    planmod.save(cx, result)
    return result


def _render(plan: dict[str, Any]) -> str:
    """The human form of a plan; a remove plan adds what stays and the cleanup lines."""
    if plan.get("remove"):
        from bay_cli import remove as removemod

        return removemod.render_plan(plan)
    from bay_cli import plan as planmod

    return planmod.render(plan)


def _report_remove(result: dict[str, Any], as_json: bool) -> None:
    from bay_cli import remove as removemod

    if as_json:
        _echo_json(result)
    else:
        if result["result"] == "ok":
            gone = (
                f"projects/{result['project']}/ removed from the fleet"
                if result["lock_removed"]
                else f"{', '.join(result['confirmed'])} removed from the lock"
            )
            console.success(
                f"removed {result['project']} ({', '.join(result['envs']) or 'never deployed'}): "
                f"the receipt confirms it, {gone}, fleet commit {result['receipt_commit'][:12]}"
                + (", pushed" if result["pushed"] else "")
            )
        else:
            console.error(
                f"{result['project']} is not removed yet: the receipt does not confirm it"
            )
        for note in result["notes"]:
            console.info(f"note: {note}")
        for line in removemod.render_cleanup(result["cleanup"]):
            typer.echo(line)
        if result["push_error"]:
            console.warning(f"the fleet repo was not pushed: {result['push_error']}")
        if result.get("push_skipped"):
            console.warning(str(result["push_skipped"]))
    if result["result"] != "ok":
        raise typer.Exit(1)
