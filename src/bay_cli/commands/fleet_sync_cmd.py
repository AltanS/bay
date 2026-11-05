"""`bay fleet status`, `bay fleet pull`, `bay fleet push`: the fleet checkout and its remote.

They work from any folder with the normal fleet selection (``--fleet``,
``BAY_FLEET``, the bay.toml here, ``BAY_FLEET_NAME``, the fleet you stand in).
None of them reads a box.
"""

from __future__ import annotations

import json
from typing import Annotated, Any

import typer

from bay_cli import console, fleetsync, gitrepo, lockfile
from bay_cli.context import Context
from bay_cli.errors import BayError

_JsonOpt = Annotated[bool, typer.Option("--json", help="Print one JSON document.")]


def _context(ctx: typer.Context) -> Context:
    from pathlib import Path

    from bay_cli.commands.project_cmd import _read_identity, find_bay_toml, fleet_context

    here = find_bay_toml(Path.cwd())
    _, fleet_name = _read_identity(here) if here else (None, None)
    return fleet_context(ctx, fleet_name, allow_cwd=True)


def _need_git(cx: Context) -> None:
    if not gitrepo.is_repo(cx.fleet_root) or gitrepo.head(cx.fleet_root) is None:
        raise BayError(f"the fleet {cx.fleet_root} is not a git repo with a commit")


def _open_records(cx: Context) -> list[dict[str, Any]]:
    """Every env record of every lock that is ``pending`` or ``failed``."""
    from bay_cli import plan as planmod

    rows: list[dict[str, Any]] = []
    for name in planmod.fleet_projects(cx):
        raw = lockfile.read(lockfile.lock_path(cx.fleet_root, name))
        if not raw:
            continue
        for env, record in sorted((raw.get("envs") or {}).items()):
            if isinstance(record, dict) and record.get("result") in ("pending", "failed"):
                rows.append(
                    {
                        "project": raw.get("name") or name,
                        "env": env,
                        "result": record.get("result"),
                        "claimed_by": record.get("claimed_by"),
                        "claimed_at": record.get("claimed_at"),
                    }
                )
    return rows


def _untracked_plans(cx: Context) -> int:
    from bay_cli.plan import PLANS_DIR

    out = gitrepo._out(
        cx.fleet_root, "ls-files", "--others", "--exclude-standard", "--", PLANS_DIR
    )
    return sum(1 for line in (out or "").splitlines() if line.endswith(".json"))


def status(ctx: typer.Context, as_json: _JsonOpt = False) -> None:
    """Show the fleet checkout against its remote. Exit 0 when in sync and clean, else 41.

    Fleet name and path, branch, upstream, ahead, behind, uncommitted paths,
    untracked plan records, the run that holds the fleet (if one does), and
    every lock record that is pending or failed. One fetch, no box access.
    """
    from bay_cli import apply as applymod

    cx = _context(ctx)
    _need_git(cx)
    st = fleetsync.state(cx.fleet_root)
    doc: dict[str, Any] = {
        "fleet": applymod.fleet_info(cx),
        **st.to_dict(),
        "untracked_plans": _untracked_plans(cx),
        "lock_holder": fleetsync.lock_holder(cx.fleet_root),
        "open_records": _open_records(cx),
    }
    # ``clean`` means no uncommitted file. Behind or ahead is ``in_sync``, not ``clean``.
    clean = not st.dirty_paths
    ok = st.in_sync and clean
    if st.no_remote or st.not_toplevel:
        # Nothing to be in step with (or bay never pushes it): clean is enough.
        ok = clean and not st.unreachable
    doc["clean"] = clean
    if as_json:
        typer.echo(json.dumps(doc, indent=2))
    else:
        typer.echo(_render_status(doc, st, ok))
    raise typer.Exit(0 if ok else fleetsync.EXIT_NOT_IN_SYNC)


def _render_status(doc: dict[str, Any], st: fleetsync.State, ok: bool) -> str:
    fleet = doc["fleet"]
    lines = [f"fleet {fleet['name']}  {fleet['path']}  (selected by {fleet['selected_by']})"]
    if st.no_remote:
        remote = "no remote"
    elif st.unreachable:
        remote = f"remote unreachable: {st.unreachable}"
    elif st.upstream is None:
        remote = "the branch tracks no remote branch"
    else:
        remote = f"upstream {st.upstream}: ahead {st.ahead}, behind {st.behind}"
        if st.diverged:
            remote += " (diverged)"
    lines.append(f"  branch {st.branch or '(detached)'}  {remote}")
    if st.not_toplevel:
        lines.append("  the fleet is a folder of a bigger repo; bay commits it and never pushes it")
    if st.dirty_paths:
        lines.append(f"  uncommitted: {fleetsync._path_list(st.dirty_paths)}")
    if doc["untracked_plans"]:
        lines.append(f"  untracked plan records: {doc['untracked_plans']}")
    holder = doc["lock_holder"]
    if holder is not None:
        lines.append(f"  {fleetsync.busy_message(holder)}")
    for row in doc["open_records"]:
        who = (
            f"  claimed by {row['claimed_by']} at {row['claimed_at']}"
            if row.get("claimed_by")
            else ""
        )
        lines.append(f"  {row['project']} {row['env']}: {row['result']}{who}")
    if st.diverged:
        lines.append(f"  {fleetsync.diverged_message(st)}")
    lines.append("  in sync and clean" if ok else "  not in sync or not clean")
    return "\n".join(lines)


def _remote_fleet(cx: Context, verb: str) -> fleetsync.State:
    _need_git(cx)
    if not gitrepo.is_toplevel(cx.fleet_root):
        raise BayError(
            f"the fleet is a folder of a bigger repo ({gitrepo.toplevel(cx.fleet_root)}); "
            f"bay does not {verb} it",
            hint=f"Run git {verb} in that repo.",
        )
    st = fleetsync.state(cx.fleet_root)
    if st.no_remote:
        raise BayError(
            "the fleet repo has no remote", hint="Add one with: git remote add origin <url>"
        )
    if st.unreachable:
        raise fleetsync.SyncError(
            f"cannot fetch the fleet remote: {st.unreachable}",
            reason=fleetsync.REASON_UNREACHABLE,
        )
    if st.upstream is None:
        raise fleetsync.SyncError(
            "the fleet branch tracks no remote branch",
            reason=fleetsync.REASON_NO_UPSTREAM,
            hint="Set one with git branch -u origin/<branch>.",
        )
    return st


def pull(ctx: typer.Context) -> None:
    """Fetch and fast-forward the fleet. Never merges: a diverged fleet refuses."""
    cx = _context(ctx)
    with fleetsync.run_lock(cx.fleet_root, "fleet pull"):
        st = _remote_fleet(cx, "pull")
        if st.diverged:
            raise fleetsync.SyncError(
                fleetsync.diverged_message(st),
                reason=fleetsync.REASON_DIVERGED,
                next_cmd="bay fleet push",
            )
        if not st.behind:
            console.success(f"the fleet is up to date with {st.upstream}")
            return
        pulled = fleetsync.fast_forward(cx.fleet_root)
        console.success(pulled.line())


def push(ctx: typer.Context) -> None:
    """Push the fleet. When the remote moved: git pull --rebase once, then push again.

    A rebase that git refuses (local edits) or that conflicts is aborted; the
    commits stay local and the conflicting files are listed.
    """
    cx = _context(ctx)
    with fleetsync.run_lock(cx.fleet_root, "fleet push"):
        st = _remote_fleet(cx, "push")
        if not st.ahead:
            console.success(f"nothing to push: the fleet has no commit that {st.upstream} lacks")
            return
        done = fleetsync.push_with_rebase(cx.fleet_root, attempts=1)
        if done.pushed:
            how = " after a rebase onto the remote" if done.rebased else ""
            console.success(f"pushed {st.ahead} commit(s) to {st.upstream}{how}")
            return
        files = f" Conflicting files: {', '.join(done.conflicts)}." if done.conflicts else ""
        raise fleetsync.SyncError(
            f"the fleet was not pushed: {done.problem}.{files} The rebase was undone; "
            "the commits stay local",
            reason=fleetsync.REASON_DIVERGED if done.conflicts else "push_refused",
            hint="Resolve it by hand in the fleet: commit or move your local edits, then run "
            f"bay fleet push again. Re-run the bay command that wrote a generated file, and "
            f"{fleetsync.NEVER_MERGE}.",
            paths=done.conflicts,
        )


def register(app: typer.Typer) -> None:
    """Add ``status``, ``pull`` and ``push`` to the ``bay fleet`` group."""
    app.command("status")(status)
    app.command("pull")(pull)
    app.command("push")(push)
