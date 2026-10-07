"""``bay up``, ``bay rollback`` and ``bay show``.

``bay up`` in order:

1. Plan again (or re-check a saved plan with ``--plan-id``). Refuse
   ``blocked`` (exit 20) and ``stale`` (exit 30). Refuse ``approve`` (exit 10)
   unless ``bay approve`` recorded an approval, or ``--force --reason`` is
   given; the reason is written to the lock's ``previous``. Refuse a repo
   project's commit that is on no branch of its remote ("push first"): the
   box can only build what the remote has.
2. Write the lock: the project pin moves to the planned commit, the env
   record gets ``result: pending`` and ``previous`` (the pin it replaces).
3. Compile the fleet into its services file (hash header).
4. Commit the fleet repo: ``bay: up <name> <env> <short sha>``.
5. Run today's deploy for the box env, limited to the ``deploy_stack`` tag.
   The deploy copies config files from the ``files/`` of the scratch fleet
   step 3 compiled (``bay_config_files_root``): the committed files, with
   each file a bay.toml mounts from beside it mapped in. That scratch fleet
   is removed when the deploy has finished.
6. Read the receipt back. The deploy covered the whole box env, so every
   project the compile read that has a ``[deploy.<env>]`` on that box env is
   pinned (:func:`_pin_deployed`): ``commit``, ``result``, ``deployed_at``,
   ``plan_id``, ``last_receipt_sha256`` and ``previous``. One commit:
   ``bay: receipt <box env> (<n> projects)``.
7. Report what the deploy did: ``applied`` lists, per box, every container
   whose receipt action is not ``noop`` (:func:`applied_from`). ``steps`` is
   still the plan. Prune ``plans/`` to the newest records plus every record
   a lock names (:func:`prune_plans`): ``bay: prune plans (<n> files)``.
8. Push the fleet repo when it has a remote (not with ``--no-push``). A
   failed push is a warning. A fleet that is a subdirectory of a larger repo
   is committed but never pushed (``push_skipped`` says why).

A plan with a box move (``moves``) writes the new box into the lock in step
2. When the old box is in another box environment, step 5 deploys that one
too, so the moved containers leave it. :func:`up_env` applies a
whole-environment plan: every project of it is pinned in step 2, and the
fleet commit is ``bay: up <env> (<n> projects)``.

A failed deploy keeps the new pin and records ``result: failed``, so
``bay show`` says HALF. ``bay rollback`` runs the same steps with the
``previous`` commit, which swaps the two pins.
"""

from __future__ import annotations

import copy
import json
import shutil
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from bay_cli import gitrepo, lockfile
from bay_cli import plan as planmod
from bay_cli.context import Context
from bay_cli.errors import BayError, ErrorCode
from bay_cli.fleet import GENERATED_SERVICES

#: ``(cx, box_env, config_files_root=<dir>[, code_targets=...]) -> None``.
#: Raises on a failed deploy. Tests swap in a fake. ``code_targets`` is passed
#: only when there are any.
Deployer = Callable[..., None]
#: ``(cx, box_env, repo) -> {box: [commit tags]}``, as receipts.list_commit_tags.
TagLister = Callable[[Context, str, str], dict[str, list[str]]]
Echo = Callable[[str], None]


class Refused(Exception):
    """``bay up`` will not apply the plan. ``exit_code`` is the verdict's code."""

    def __init__(self, plan: dict[str, Any], message: str) -> None:
        super().__init__(message)
        self.plan = plan
        self.exit_code = int(plan["exit_code"]) or 1


#: The tags ``bay up`` runs. They are the tags ``bay plan``'s box check runs, so
#: the plan and the apply do the same work. The ``git_deploy`` role tags its
#: render tasks (rebuild.sh, image-map.json) with ``deploy_stack``, so a ``bay up``
#: re-renders the webhook rebuild script without cloning, building or pulling.
UP_DEPLOY_TAGS = "deploy_stack"


def default_deploy(
    cx: Context,
    box_env: str,
    *,
    config_files_root: Path | None = None,
    code_targets: Mapping[str, Any] | None = None,
    tags: str = UP_DEPLOY_TAGS,
) -> None:
    """Today's ``bay deploy <env> --tags deploy_stack``, without the prompts and the banner.

    ``tags`` adds ``headscale,traefik`` when the plan has a route step
    (:func:`bay_cli.routes.deploy_tags`).

    ``config_files_root`` is the ``files/`` of the scratch fleet ``bay up``
    compiled; the deploy copies config files from there
    (``bay_config_files_root``), so it ships the committed files the plan read.

    ``code_targets`` (``{container: {"commit"|"source", "strict"}}``) becomes
    the ``bay_code_targets`` extra var: the box points ``:latest`` at those
    commit images before the reconciler pass (``bay_reconcile.codepin``).
    """
    from bay_cli.commands import ops
    from bay_cli.commands.validate import run_validation
    from bay_cli.healthcheck import new_report_dir, report_dir_vars
    from bay_cli.receipts import deploy_extra_vars

    result = run_validation(cx.fleet_root, box_env, bay_dir=cx.framework_root, show_banner=False)
    if result.total_issues:
        raise BayError(f"validation failed with {result.total_issues} problem(s)")
    # The per-box reconciler reports go to a temp dir outside every working
    # tree, never into the framework checkout, and are removed afterwards.
    report_dir = new_report_dir()
    try:
        extra = [
            "-e",
            "_rig_mode=true",
            "-e",
            "_rig_write=false",
            *deploy_extra_vars(cx),
            *report_dir_vars(report_dir),
            *planmod.config_files_vars(config_files_root),
        ]
        if code_targets:
            extra += ["-e", json.dumps({"bay_code_targets": dict(code_targets)})]
        ops._run_playbook(cx, "deploy", box_env, tags, extra)
        ops._invalidate_rig_cache(cx.cache_dir)
        ops._run_post_deploy_healthcheck(
            box_env, cx.fleet_root, cx.framework_root, report_dir=report_dir
        )
    finally:
        shutil.rmtree(report_dir, ignore_errors=True)


def _write_services(cx: Context, text: str) -> Path:
    target = cx.fleet_root / GENERATED_SERVICES
    _, state = planmod.current_services(cx)
    if state == "foreign":
        raise BayError(
            "the fleet's services file was not written by bay", hint="Run `bay import` first."
        )
    if state == "edited":
        raise BayError(
            "the fleet's services file was edited by hand since the last compile",
            hint="Move the change into bay.toml or bay.fleet.toml.",
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(text)
    tmp.replace(target)
    return target


def _previous_for(record: Mapping[str, Any], lock: Mapping[str, Any]) -> dict[str, Any] | None:
    commit = record.get("commit") or lock.get("commit")
    if not commit:
        return None
    previous = {
        "commit": commit,
        "deployed_at": record.get("deployed_at"),
        "receipt_sha256": record.get("last_receipt_sha256"),
    }
    if record.get("plan_id"):
        # The plan that deployed the replaced pin; plans/ prune keeps it.
        previous["plan_id"] = record["plan_id"]
    return previous


def _gate(plan: dict[str, Any], force: bool, reason: str | None, say: Echo) -> bool:
    """Refuse blocked, stale and unapproved plans. True when ``--force`` overrode approve."""
    verdict = plan["verdict"]
    if verdict == "blocked":
        raise Refused(plan, "the plan is blocked: " + "; ".join(plan["blockers"]))
    if verdict == "stale":
        raise Refused(plan, "the plan is stale: " + "; ".join(plan["stale"]))
    if verdict == "approve":
        if not force:
            raise Refused(
                plan, f"plan {plan['plan_id']} has destructive or shared steps and no approval"
            )
        say(f"forced without approval: {reason}")
        return True
    return False


def up(
    proj: planmod.ProjectRef,
    opts: planmod.PlanOptions,
    *,
    plan_id: str | None = None,
    force: bool = False,
    reason: str | None = None,
    action: str = "up",
    read_receipts: planmod.ReceiptReader | None = None,
    check_box: planmod.BoxCheck | None = None,
    deploy: Deployer | None = None,
    echo: Echo | None = None,
    push: bool = True,
    code: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Apply a plan. Returns a result document; raises :class:`Refused` or BayError.

    With ``push`` (the default) the fleet repo is pushed after the last lock
    commit when it has a remote. A failed push is a warning, never a failed
    deploy; the result says ``pushed: false`` and ``push_error``.

    ``code`` is what the box does with this project's build images
    (:func:`_code_targets`): ``None`` lets ``up`` decide; ``bay rollback``
    passes ``{"source": "prev"}`` or ``{"commit": <sha>, "strict": True}``.
    A rollback also freezes the env; a later ``up`` to a newer commit thaws
    it (:func:`_freeze`).
    """
    say = echo or (lambda _msg: None)
    cx = proj.cx
    if force and not (reason and reason.strip()):
        raise BayError("--force needs --reason", hint='Pass --reason "<why>".')

    if plan_id:
        saved = planmod.load_saved(cx, plan_id)
        plan = planmod.recheck(
            proj, saved, opts, read_receipts=read_receipts, check_box=check_box
        )
    else:
        plan = planmod.make_plan(proj, opts, read_receipts=read_receipts)
    planmod.save(cx, plan)
    forced = _gate(plan, force, reason, say)
    commit = str(plan["wanted"]["commit"])
    return _apply_plan(
        cx,
        plan,
        [(proj, commit)],
        forced=forced,
        reason=reason,
        message=f"bay: {action} {proj.name} {plan['env']} {commit[:12]}",
        action=action,
        read_receipts=read_receipts,
        deploy=deploy,
        say=say,
        push=push,
        code=code,
    )


def up_env(
    cx: Context,
    opts: planmod.PlanOptions,
    *,
    plan_id: str | None = None,
    force: bool = False,
    reason: str | None = None,
    cwd: Path | None = None,
    read_receipts: planmod.ReceiptReader | None = None,
    check_box: planmod.BoxCheck | None = None,
    deploy: Deployer | None = None,
    echo: Echo | None = None,
    push: bool = True,
) -> dict[str, Any]:
    """Apply a whole-environment plan (``bay up <env>`` in a fleet, no ``--project``).

    Every project of the plan is pinned to its WANTED commit, then the box
    environment is deployed once, as :func:`up` does for one project.
    """
    say = echo or (lambda _msg: None)
    if force and not (reason and reason.strip()):
        raise BayError("--force needs --reason", hint='Pass --reason "<why>".')
    if plan_id:
        saved = planmod.load_saved(cx, plan_id)
        plan = planmod.recheck_env(
            cx, saved, opts, cwd=cwd, read_receipts=read_receipts, check_box=check_box
        )
    else:
        plan = planmod.make_env_plan(cx, opts, cwd=cwd, read_receipts=read_receipts)
    planmod.save(cx, plan)
    if not plan["projects"]:
        raise BayError(
            f"no project has [deploy.{plan['env']}], so there is nothing to deploy",
            code=ErrorCode.NOT_FOUND,
        )
    forced = _gate(plan, force, reason, say)
    members = [
        (planmod.load_project(cx, p["name"], cwd=cwd, fetch=False), str(p["wanted"]["commit"]))
        for p in plan["projects"]
    ]
    return _apply_plan(
        cx,
        plan,
        members,
        forced=forced,
        reason=reason,
        message=f"bay: up {plan['env']} ({len(members)} projects)",
        action="up",
        read_receipts=read_receipts,
        deploy=deploy,
        say=say,
        push=push,
    )


def _apply_plan(
    cx: Context,
    plan: dict[str, Any],
    members: list[tuple[planmod.ProjectRef, str]],
    *,
    forced: bool,
    reason: str | None,
    message: str,
    action: str,
    read_receipts: planmod.ReceiptReader | None,
    deploy: Deployer | None,
    say: Echo,
    push: bool,
    code: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Steps 2 to 8 of ``bay up`` for the projects of an accepted plan.

    ``code`` goes to :func:`_code_targets` for every project (``bay rollback``).
    """
    env = str(plan["env"])
    box_env = str(plan["box_env"])
    for proj, commit in members:
        on_remote = planmod.commit_on_remote(proj, commit)
        if on_remote is not True:
            why = "is not" if on_remote is False else "cannot be checked to be"
            raise Refused(
                plan,
                f"commit {commit[:12]} of {proj.name} {why} on a branch of {proj.repo}; "
                "push first",
            )
    moves = {m["project"]: m for m in plan.get("moves") or [] if m["env"] == env}

    # 2. the locks
    locks: dict[str, dict[str, Any]] = {}
    previous_by: dict[str, dict[str, Any] | None] = {}
    for proj, commit in members:
        lock = copy.deepcopy(proj.lock)
        envs = lock.setdefault("envs", {})
        record = dict(envs.get(env, {}))
        old_pin = lockfile.env_pin(proj.lock, env)
        previous = record.get("previous")
        if old_pin and old_pin != commit:
            previous = _previous_for(record, proj.lock)
        if forced and previous is not None:
            previous = {**previous, "force_reason": str(reason).strip()}
        lock["commit"] = commit
        move = moves.get(proj.name)
        if move is not None:
            # The plan moved the project: the lock now pins the new box.
            record["box"] = move["to"]
        elif not record.get("box"):
            box = next(
                (p["box"] for p in plan.get("projects") or [] if p["name"] == proj.name),
                plan.get("box"),
            )
            if box:
                record["box"] = box
        record.update({"commit": commit, "result": "pending", "plan_id": plan["plan_id"]})
        if previous is not None:
            record["previous"] = previous
        freeze_note = _freeze(proj, record, commit, rollback=action == "rollback")
        if freeze_note:
            say(f"note: {freeze_note}")
        envs[env] = record
        lockfile.write(proj.lock_file, lock)
        locks[proj.name] = lock
        previous_by[proj.name] = previous

    # 3. compile into the fleet. The scratch fleet stays until the deploy
    # has finished: the deploy copies config files from its files/.
    failure: str | None = None
    first = members[0][0]
    with planmod.compiled_fleet(cx, cwd=first.cwd) as comp:
        if comp.result is None:
            raise BayError(
                "compile failed after the lock was written:\n  " + "\n  ".join(comp.errors)
            )
        services = _write_services(cx, comp.result.text())
        compiled_commits = dict(comp.commits)
        left_out = list(comp.unpinned)
        # Only a one-project plan carries code.keep (branch mode, newer code runs).
        keep = set((plan.get("code") or {}).get("keep") or [])
        code_targets: dict[str, dict[str, Any]] = {}
        for proj, commit in members:
            built = _build_containers(comp.result.data(), proj, locks[proj.name], env, commit)
            code_targets.update(_code_targets(proj, env, commit, built, code, keep=keep))

        # 4. commit
        paths = [p.lock_file for p, _ in members]
        paths += [services, planmod.plan_file(cx, plan["plan_id"])]
        approval = planmod.approval_file(cx, plan["plan_id"])
        if approval.is_file():
            paths.append(approval)
        try:
            fleet_commit = gitrepo.commit_paths(cx.fleet_root, paths, message)
        except gitrepo.GitError as exc:
            raise BayError(f"cannot commit the fleet repo: {exc}") from None
        say(f"fleet commit {fleet_commit}")

        # 5. deploy. A move to a box of another box environment also deploys
        # the old one, so its reconciler removes the moved containers there.
        # A route step adds the headscale and traefik tags (spec M117/07) on
        # the plan's box environment, where the ingress box is.
        from bay_cli import routes

        tags = routes.deploy_tags(plan["steps"], UP_DEPLOY_TAGS)
        if tags != UP_DEPLOY_TAGS:
            say(f"deploy tags: {tags} (the plan changes a tailnet route)")
        deploy_envs = [box_env] + sorted(
            {
                str(m["from_box_env"])
                for m in moves.values()
                if m.get("from_box_env") and m["from_box_env"] != box_env
            }
        )
        for target in deploy_envs:
            # Code targets name containers of the plan's box environment.
            extra: dict[str, Any] = (
                {"code_targets": code_targets} if code_targets and target == box_env else {}
            )
            if target == box_env and tags != UP_DEPLOY_TAGS:
                extra["tags"] = tags
            try:
                (deploy or default_deploy)(
                    cx, target, config_files_root=comp.files_root, **extra
                )
            except (BayError, OSError) as exc:
                failure = failure or str(exc) or type(exc).__name__
            except SystemExit as exc:
                failure = failure or f"deploy exited with {exc.code}"

    # 6. receipt
    reader = read_receipts or planmod.default_receipt_reader
    entries: list[dict[str, Any]] = []
    try:
        entries = reader(cx, box_env)
    except (BayError, OSError) as exc:
        say(f"cannot read the receipt: {exc}")
    receipt_failed = any(
        isinstance(e.get("receipt"), Mapping) and e["receipt"].get("result") == "failed"
        for e in entries
    )
    if failure is None and receipt_failed:
        failure = "the box receipt says the deploy failed"
    word = "failed" if failure else "ok"
    deployed_at = planmod._now()
    pinned: list[dict[str, Any]] = []
    lock_files: list[Path] = []
    for proj, commit in members:
        lock = locks[proj.name]
        names = set(
            planmod.project_containers(
                proj.name, planmod.doc_at(proj, commit) or {}, lock, proj.primary_env
            )
            .get(env, {})
            .values()
        )
        slice_ = planmod.running_slice(entries, names)
        record = dict(lock["envs"][env])
        record["result"] = word
        record["deployed_at"] = deployed_at
        record["last_receipt_sha256"] = slice_["receipt_sha256"]
        lock["envs"][env] = record
        lockfile.write(proj.lock_file, lock)
        pinned.append({"project": proj.name, "env": env, "commit": commit, "result": word})
        lock_files.append(proj.lock_file)
    more_files, more_rows = _pin_deployed(
        cx,
        skip={p.name for p, _ in members},
        box_env=box_env,
        commits=compiled_commits,
        entries=entries,
        stamp={"result": word, "deployed_at": deployed_at, "plan_id": plan["plan_id"]},
    )
    lock_files += more_files
    pinned += more_rows
    notes = [
        f"{name} has no pinned commit, so this deploy left it out and its lock is unchanged"
        for name in left_out
    ]
    notes += [
        f"{name} runs code newer than {members[0][1][:12]}; bay up kept that code and "
        "applied the config only"
        for name in sorted(keep)
        if name not in code_targets
    ]
    applied, stale_boxes = applied_from(entries, fleet_commit)
    notes += [
        f"box {b}: the receipt is not from this deploy, so `applied` leaves it out"
        for b in stale_boxes
    ]
    for move in moves.values():
        notes.extend(planmod.move_notes(move))
    for note in notes:
        say(f"note: {note}")
    try:
        receipt_commit = gitrepo.commit_paths(
            cx.fleet_root, lock_files, f"bay: receipt {box_env} ({len(lock_files)} projects)"
        )
    except gitrepo.GitError as exc:
        raise BayError(f"cannot commit the fleet repo: {exc}") from None

    # 7. plans/ prune: the newest records plus every record a lock names.
    pruned: list[str] = []
    try:
        pruned = prune_plans(cx)
    except gitrepo.GitError as exc:
        say(f"warning: plans/ was not pruned: {exc}")
    if pruned:
        say(f"pruned {len(pruned)} old plan file(s) from {planmod.PLANS_DIR}/")

    single = plan.get("project") is not None
    commit0 = members[0][1]
    result = {
        "project": plan.get("project"),
        "projects": [p.name for p, _ in members],
        "env": env,
        "box_env": box_env,
        "action": action,
        "plan_id": plan["plan_id"],
        "verdict": plan["verdict"],
        "forced": forced,
        "commit": commit0 if single else None,
        "previous_commit": (previous_by[members[0][0].name] or {}).get("commit")
        if single
        else None,
        "fleet_commit": fleet_commit,
        "receipt_commit": receipt_commit,
        "result": word,
        "error": failure,
        "pushed": False,
        "push_error": None,
        "push_skipped": None,
        "steps": plan["steps"],
        "applied": applied,
        "pinned": pinned,
        "pruned": pruned,
        "notes": notes,
        "code_targets": code_targets,
        "frozen": any(bool(locks[p.name]["envs"][env].get("frozen")) for p, _ in members),
    }
    if push:
        push_fleet(cx, result, say)
    if failure:
        raise DeployFailed(result)
    return result


def push_fleet(cx: Context, result: dict[str, Any], say: Echo) -> None:
    """Step 8: push the fleet repo; set ``pushed``, ``push_error`` and ``push_skipped``.

    A fleet that is a directory inside a bigger repo (a workspace with
    several fleets) shares that repo's remote and branch. Pushing it would
    publish everything else committed there too, so bay commits and leaves
    the push to the operator. A failed push is a warning.
    """
    if not gitrepo.is_toplevel(cx.fleet_root):
        top = gitrepo.toplevel(cx.fleet_root)
        result["push_skipped"] = (
            f"the fleet lives inside a larger repo ({top}); it was committed, not pushed"
        )
        say(f"warning: {result['push_skipped']}")
        return
    pushed, problem = gitrepo.push(cx.fleet_root)
    result["pushed"], result["push_error"] = pushed, problem
    if problem:
        say(f"warning: the fleet repo was not pushed: {problem}")
    elif pushed:
        say("pushed the fleet repo")


#: ``plans/`` keeps this many of the newest plan records, plus every record a
#: lock names.
PLANS_KEEP = 50


def _plan_time(path: Path) -> str:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return ""
    return str(data.get("created_at") or "") if isinstance(data, dict) else ""


def _lock_plan_ids(cx: Context) -> set[str]:
    """Every plan id a lock of the fleet names: ``envs.*.plan_id`` and ``previous.plan_id``."""
    out: set[str] = set()
    for name in planmod.fleet_projects(cx):
        try:
            raw = lockfile.read(lockfile.lock_path(cx.fleet_root, name))
        except ValueError:
            continue
        for record in ((raw or {}).get("envs") or {}).values():
            if not isinstance(record, Mapping):
                continue
            if record.get("plan_id"):
                out.add(str(record["plan_id"]))
            previous = record.get("previous")
            if isinstance(previous, Mapping) and previous.get("plan_id"):
                out.add(str(previous["plan_id"]))
    return out


def prune_plans(cx: Context, *, keep: int = PLANS_KEEP) -> list[str]:
    """Remove old committed plan records from ``plans/`` in one commit. Returns the paths.

    Keeps the ``keep`` newest records (by ``created_at``) and every record a
    lock still names. A record's approval (``<id>.approved``) goes with it.
    Only files git tracks are removed, with ``git rm`` and the commit
    ``bay: prune plans``; an untracked plan (a ``bay plan`` that was never
    applied) is never touched.
    """
    rel_dir = planmod.PLANS_DIR
    tracked = set(gitrepo.tracked_files(cx.fleet_root, rel_dir))
    records = sorted(
        (rel for rel in tracked if rel.endswith(".json")),
        key=lambda rel: (_plan_time(cx.fleet_root / rel), rel),
        reverse=True,
    )
    named = _lock_plan_ids(cx)
    kept = {Path(rel).stem for rel in records[:keep]} | named
    gone: list[str] = []
    for rel in tracked:
        stem = Path(rel).name.split(".", 1)[0]
        if rel.endswith((".json", planmod.APPROVED_SUFFIX)) and stem not in kept:
            gone.append(rel)
    gone.sort()
    if gone:
        gitrepo.remove_and_commit(cx.fleet_root, gone, f"bay: prune plans ({len(gone)} files)")
    return gone


def applied_from(
    entries: list[dict[str, Any]], fleet_commit: str
) -> tuple[list[dict[str, Any]], list[str]]:
    """What the deploy did, per box, read from the receipts it wrote.

    ``steps`` is the plan; this is the result. One row per container whose
    receipt ``action`` is not ``noop`` (nor null, a pass that crashed before
    it reported): ``{box, container, action, healthy}``. A receipt whose
    ``fleet_commit`` is not this deploy's is an older one (the deploy failed
    before it wrote a new one), so it is left out and its box is returned in
    the second list.
    """
    rows: list[dict[str, Any]] = []
    stale: list[str] = []
    for entry in entries:
        receipt = entry.get("receipt")
        if not isinstance(receipt, Mapping):
            continue
        box = str(entry.get("box") or receipt.get("box"))
        if receipt.get("fleet_commit") != fleet_commit:
            stale.append(box)
            continue
        for c in receipt.get("containers") or []:
            if not isinstance(c, Mapping) or c.get("action") in (None, "noop"):
                continue
            rows.append(
                {
                    "box": box,
                    "container": c.get("name"),
                    "action": c.get("action"),
                    "healthy": c.get("healthy"),
                }
            )
    rows.sort(key=lambda r: (r["box"], str(r["container"])))
    return rows, sorted(stale)


def _pin_deployed(
    cx: Context,
    *,
    skip: set[str],
    box_env: str,
    commits: Mapping[str, str],
    entries: list[dict[str, Any]],
    stamp: Mapping[str, Any],
) -> tuple[list[Path], list[dict[str, Any]]]:
    """Record the deploy in the lock of every other project it deployed.

    ``bay up`` deploys the whole box env, so every project the compile read
    (``commits``) that has a ``[deploy.<env>]`` resolving to ``box_env`` runs
    what the compile read. For each such env:

    * ``commit``: the commit the compile read. For a project in the fleet
      that is the last fleet commit that touched ``projects/<name>/``; for a
      repo project, the commit its lock already pins (it keeps it). The
      top-level ``commit`` moves with it.
    * ``previous``: the pin it replaces, when there was one and it differs.
    * ``result``, ``deployed_at``, ``plan_id`` from ``stamp``, and
      ``last_receipt_sha256`` from this project's part of the receipt.

    A repo project with no pinned commit was not compiled, so it is not in
    ``commits`` and its lock stays as it is. Returns the lock files written
    and one row per env pinned.
    """
    files: list[Path] = []
    rows: list[dict[str, Any]] = []
    for name in sorted(commits):
        if name in skip:
            continue
        try:
            other = planmod.load_project(cx, name, fetch=False)
        except BayError:
            continue
        commit = commits[name]
        doc = planmod.doc_at(other, commit)
        if not doc:
            continue
        lock = copy.deepcopy(other.lock)
        envs = lock.setdefault("envs", {})
        touched = False
        for env in sorted(doc.get("deploy") or {}):
            box, env_box_env = planmod.resolve_box(other, env, doc)
            if env_box_env != box_env:
                continue
            record = dict(envs.get(env, {}))
            old_pin = lockfile.env_pin(other.lock, env)
            if old_pin and old_pin != commit:
                previous = _previous_for(record, other.lock)
                if previous is not None:
                    record["previous"] = previous
            if not record.get("box") and box:
                record["box"] = box
            names = set(
                planmod.project_containers(name, doc, lock, other.primary_env)
                .get(env, {})
                .values()
            )
            record.update(
                {
                    "commit": commit,
                    **stamp,
                    "last_receipt_sha256": planmod.running_slice(entries, names)[
                        "receipt_sha256"
                    ],
                }
            )
            envs[env] = record
            rows.append({"project": name, "env": env, "commit": commit, "result": stamp["result"]})
            touched = True
        if touched:
            lock["commit"] = commit
            lockfile.write(other.lock_file, lock)
            files.append(other.lock_file)
    return files, rows


class DeployFailed(Exception):
    """The deploy ran and failed. The lock says ``result: failed`` (HALF)."""

    def __init__(self, result: dict[str, Any]) -> None:
        super().__init__(f"deploy failed: {result['error']}")
        self.result = result


# ── Code and config: track, freeze, code targets ──────────────────────────


def _freeze(
    proj: planmod.ProjectRef, record: dict[str, Any], commit: str, *, rollback: bool
) -> str | None:
    """Set or clear ``frozen`` on the env record. Returns a note, or None.

    ``bay rollback`` freezes the env at the commit it restores: from then on a
    push builds and tags its image but never deploys (``rebuild.sh`` reads
    ``build.frozen`` from the compiled services file). The next ``bay up`` to
    a commit newer than ``frozen_commit`` (a descendant of it, per git in the
    project's checkout) clears the freeze. An ``up`` to the same or an older
    commit keeps it.
    """
    if rollback:
        record["frozen"] = True
        record["frozen_commit"] = commit
        return (
            f"frozen at {commit[:12]}: a push builds but does not deploy until a bay up "
            "to a newer commit"
        )
    if not record.get("frozen"):
        return None
    at = str(record.get("frozen_commit") or "")
    newer = bool(at) and at != commit and gitrepo.is_ancestor(proj.checkout, at, commit) is True
    if not newer:
        return (
            f"still frozen at {at[:12] or 'unknown'}: {commit[:12]} is not newer, so a push "
            "still builds without deploying"
        )
    record.pop("frozen", None)
    record.pop("frozen_commit", None)
    return f"the freeze from bay rollback ({at[:12]}) is cleared: {commit[:12]} is newer"


def _build_containers(
    data: Mapping[str, Any],
    proj: planmod.ProjectRef,
    lock: Mapping[str, Any],
    env: str,
    commit: str,
) -> list[str]:
    """This project's containers in ``env`` that build from source, sorted."""
    doc = planmod.doc_at(proj, commit) or {}
    by_env = planmod.project_containers(proj.name, doc, lock, proj.primary_env)
    names = set(by_env.get(env, {}).values())
    entries = {**(data.get("accessories") or {}), **(data.get("services") or {})}
    return sorted(
        n for n in names if isinstance(entries.get(n), Mapping) and "build" in entries[n]
    )


def _code_targets(
    proj: planmod.ProjectRef,
    env: str,
    commit: str,
    built: list[str],
    code: Mapping[str, Any] | None,
    *,
    keep: set[str] | frozenset[str] = frozenset(),
) -> dict[str, dict[str, Any]]:
    """``{container: {"commit"|"source", "strict"}}``: where ``:latest`` must point.

    * ``code`` given (``bay rollback``): that, for every build container.
    * ``bay up`` of a project whose bay.toml lives in the app repo: the pinned
      commit, which is also the code commit. Strict in ``track = "pin"`` (the
      image must be on the box, built by a push). Not strict in ``branch``
      mode: a missing image leaves ``:latest`` where the last push put it.
      This is how ``bay up`` releases a held build.
    * ``keep`` (branch mode, from the plan's ``code.keep``): containers that
      run code newer than the pin. They get no target: code moves only forward.
    * An in-fleet project: nothing. Its pin is a fleet commit, not a code
      commit, so ``:latest`` stays where the last push put it.
    """
    from bay_cli import bay_toml

    if not built:
        return {}
    if code is not None:
        return {name: dict(code) for name in built}
    if proj.in_fleet:
        return {}
    doc = planmod.doc_at(proj, commit) or {}
    strict = bay_toml.track(doc, env) == "pin"
    return {
        name: {"commit": commit, "strict": strict}
        for name in built
        if strict or name not in keep
    }


def rollback(
    proj: planmod.ProjectRef,
    opts: planmod.PlanOptions,
    *,
    to: str | None = None,
    list_tags: TagLister | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Restore config and code: the previous pin, and the image the box ran before.

    Moves the pin back (as ``bay up --at <previous>``), asks the box to point
    ``:latest`` at the image the previous receipt names (``<env>.prev.json``),
    and freezes the env (``frozen = true`` in the lock).

    ``to``: roll the code back to that commit's image instead, which must
    already be on the box (``<image>:<commit12>``); the CLI asks the box first
    and refuses with the commit tags it has. For a project whose bay.toml lives
    in the app repo the pin moves to ``to`` as well; an in-fleet project keeps
    its pin (its pin is a fleet commit) and only the code moves.
    """
    env = opts.env or proj.primary_env
    record = (proj.lock.get("envs") or {}).get(env, {})
    if to:
        reader = kwargs.get("read_receipts")
        at, code = _rollback_to(proj, env, to, list_tags=list_tags, reader=reader)
    else:
        previous = record.get("previous")
        adopted_from = (record.get("adopted") or {}).get("from_fleet_commit")
        if (not previous or not previous.get("commit")) and adopted_from:
            from bay_cli.adopt import ROLLBACK_AFTER_ADOPT

            # bay adopt cleared previous: it was a fleet commit, and the pin
            # is now an app repo commit.
            raise BayError(
                f"{proj.name} {env}: {ROLLBACK_AFTER_ADOPT}",
                code=ErrorCode.CONFLICT,
                hint=f"bay adopt moved the bay.toml out of the fleet (fleet commit "
                f"{str(adopted_from)[:12]}). Pin an app repo commit with bay up --at, or "
                "roll the code back with bay rollback --to <commit>.",
            )
        if not previous or not previous.get("commit"):
            raise BayError(
                f"{proj.name} has no previous pin for {env}",
                code=ErrorCode.NOT_FOUND,
                hint="A rollback needs one earlier bay up for this environment.",
            )
        at, code = str(previous["commit"]), {"source": "prev", "strict": False}
    restored = planmod.PlanOptions(
        env=env,
        at=at,
        read_running=True,
        box_check=opts.box_check,
        allow_unsupported=opts.allow_unsupported,
        cwd_repo=opts.cwd_repo,
        # A rollback moves code backwards on purpose: no forward-only check.
        code_order=False,
    )
    return up(proj, restored, action="rollback", code=code, **kwargs)


def _rollback_to(
    proj: planmod.ProjectRef,
    env: str,
    to: str,
    *,
    list_tags: TagLister | None,
    reader: planmod.ReceiptReader | None,
) -> tuple[str, dict[str, Any]]:
    """``(commit to pin, code target)`` for ``bay rollback --to``.

    Asks the box which commit tags it has for every build container of the
    project, and refuses (listing them) when ``to`` is not one of them.
    """
    from bay_reconcile.images import is_commit, short, split_ref

    if proj.in_fleet:
        if not is_commit(to.lower()):
            raise BayError(f"--to {to}: give the app commit as hex (7 to 40 characters)")
        code_commit = to.lower()
        at = lockfile.env_pin(proj.lock, env)
        if not at:
            raise BayError(f"{proj.name} has no pin for {env}", code=ErrorCode.NOT_FOUND)
    else:
        full = gitrepo.resolve_commit(proj.checkout, to)
        if full is None:
            raise BayError(
                f"--to {to}: no such commit in {proj.checkout}", code=ErrorCode.NOT_FOUND
            )
        code_commit = at = full
    tag = short(code_commit)

    pin = lockfile.env_pin(proj.lock, env)
    doc = planmod.doc_at(proj, pin) or {}
    box, box_env = planmod.resolve_box(proj, env, doc)
    if box_env is None:
        raise BayError(f"box {box} is not in bay.fleet.toml [boxes]")
    current, _ = planmod.current_services(proj.cx)
    built = set(_build_containers(current, proj, proj.lock, env, pin or ""))
    entries = (reader or planmod.default_receipt_reader)(proj.cx, box_env)
    refs = {
        str(c.get("image_ref") or c.get("image") or "")
        for e in entries
        if isinstance(e.get("receipt"), Mapping)
        for c in e["receipt"].get("containers") or []
        if isinstance(c, Mapping) and c.get("name") in built
    }
    repos = sorted({split_ref(ref)[0] for ref in refs if ref})
    if not built:
        raise BayError(
            f"{proj.name} builds no image in {env}, so there is no code to roll back to {tag}",
            hint="Run bay rollback without --to to return to the previous pin.",
        )
    if not repos:
        raise BayError(
            f"cannot roll {proj.name} {env} back to {tag}: the box receipt lists no image "
            "for it",
            hint="Run `bay status` to read the box, or `bay up` once first.",
        )
    lister = list_tags or default_tag_lister
    for repo in repos:
        try:
            found = lister(proj.cx, box_env, repo)
        except OSError as exc:
            raise BayError(f"cannot check the box for {repo}:{tag}: {exc}") from None
        # Every box of the env must have it: the tags common to all of them.
        common = set.intersection(*(set(tags) for tags in found.values())) if found else set()
        have = sorted(common)
        if tag not in have:
            raise BayError(
                f"{repo}:{tag} is not on the box, so bay rollback --to {to} cannot run it. "
                "Commit tags on the box: " + (", ".join(have) or "none"),
                code=ErrorCode.NOT_FOUND,
                hint="Pick one of those commits, or push the commit so a build tags it.",
            )
    return at, {"commit": code_commit, "strict": True}


def default_tag_lister(cx: Context, box_env: str, repo: str) -> dict[str, list[str]]:
    from bay_cli.receipts import list_commit_tags

    return list_commit_tags(cx, box_env, repo)


# ── show ────────────────────────────────────────────────────────────────────

STATUS_WORDS = ("ok", "behind", "drift", "unknown", "HALF")


def env_status(
    record: Mapping[str, Any],
    lock_commit: str | None,
    wanted_commit: str | None,
    running: Mapping[str, Any] | None,
) -> tuple[str, str]:
    """``(status word, reason)`` for one env. Order: HALF, unknown, drift, behind, ok."""
    if record.get("result") in ("failed", "pending"):
        return "HALF", (
            "the last bay up failed; the pin moved but the box may not run it"
            if record.get("result") == "failed"
            else "the last bay up never reported back"
        )
    if running is None or not running.get("checked"):
        return "unknown", "the box was not read"
    if running.get("receipt_sha256") is None:
        return "unknown", "the box has no receipt"
    if not record.get("commit"):
        return "unknown", "bay up never ran for this environment, so no receipt was recorded"
    if record.get("commit") != lock_commit:
        return (
            "drift",
            f"the fleet pins {str(lock_commit)[:12]}, this environment runs "
            f"{str(record.get('commit'))[:12]}",
        )
    if record.get("last_receipt_sha256") != running.get("receipt_sha256"):
        return "drift", "the box receipt differs from the one bay up recorded"
    if wanted_commit and lock_commit and wanted_commit != lock_commit:
        return (
            "behind",
            f"the project is at {wanted_commit[:12]}, the fleet pins {lock_commit[:12]}",
        )
    return "ok", "WANTED, PINNED and RUNNING agree"


def show(
    proj: planmod.ProjectRef,
    *,
    remote: bool = True,
    read_receipts: planmod.ReceiptReader | None = None,
) -> dict[str, Any]:
    cx = proj.cx
    wanted = planmod.read_wanted(proj, None)
    lock = proj.lock
    lock_commit = lock.get("commit")
    pinned_doc = planmod.doc_at(proj, lock_commit)
    docs = [d for d in (wanted.doc, pinned_doc) if d]
    envs = sorted(set(lock.get("envs") or {}) | {e for d in docs for e in (d.get("deploy") or {})})
    reader = read_receipts or planmod.default_receipt_reader
    cache: dict[str, list[dict[str, Any]]] = {}
    rows: list[dict[str, Any]] = []
    for env in envs:
        record = (lock.get("envs") or {}).get(env, {})
        box, box_env = planmod.resolve_box(proj, env, pinned_doc or wanted.doc)
        names: set[str] = set()
        for d in docs:
            names |= set(
                planmod.project_containers(proj.name, d, lock, proj.primary_env)
                .get(env, {})
                .values()
            )
        running = None
        detail: list[dict[str, Any]] = []
        if remote and box_env is not None:
            if box_env not in cache:
                try:
                    cache[box_env] = reader(cx, box_env)
                except (BayError, OSError) as exc:
                    cache[box_env] = [
                        {"env": box_env, "box": None, "receipt": None, "error": str(exc)}
                    ]
            running = planmod.running_slice(cache[box_env], names)
            detail = planmod.running_detail(cache[box_env], names)
        status, why = env_status(record, lock_commit, wanted.commit, running)
        rows.append(
            {
                "env": env,
                "box": box,
                "box_env": box_env,
                "status": status,
                "reason": why,
                "pinned": {
                    "commit": record.get("commit"),
                    "deployed_at": record.get("deployed_at"),
                    "result": record.get("result"),
                    "previous": record.get("previous"),
                    "adopted": record.get("adopted") or {},
                },
                "running": {
                    "checked": running is not None,
                    "receipt_sha256": (running or {}).get("receipt_sha256"),
                    "boxes": detail,
                },
            }
        )
    return {
        "show_version": 1,
        "project": proj.name,
        "fleet": {
            "name": proj.fleet.get("name"),
            "root": str(cx.fleet_root),
            "commit": gitrepo.head(cx.fleet_root),
        },
        "wanted": {
            "checkout": str(proj.checkout),
            "commit": wanted.commit,
            "toml_sha256": wanted.toml_sha256,
            "dirty": wanted.dirty,
            "problems": wanted.problems,
        },
        "pinned": {
            "repo": lock.get("repo"),
            "commit": lock_commit,
            "lock_sha256": lockfile.sha256_of(proj.lock_file),
        },
        "envs": rows,
    }


def render_show(doc: Mapping[str, Any]) -> str:
    def short(sha: Any) -> str:
        return str(sha)[:12] if sha else "none"

    w, p = doc["wanted"], doc["pinned"]
    lines = [
        f"{doc['project']}  (fleet {doc['fleet']['name']})",
        f"  WANTED   {short(w['commit'])}  bay.toml {short(w['toml_sha256'])}"
        + ("  uncommitted changes" if w["dirty"] else ""),
        f"  PINNED   {short(p['commit'])}  {p['repo'] or 'no repo'}",
    ]
    for problem in w["problems"]:
        lines.append(f"  note: {problem}")
    for row in doc["envs"]:
        run = row["running"]
        if not run["checked"]:
            running = "not read"
        elif run["receipt_sha256"] is None:
            running = "no receipt"
        else:
            names = [c["name"] for b in run["boxes"] for c in b["containers"]]
            running = f"{len(names)} container(s), receipt {short(run['receipt_sha256'])}"
        lines.append(
            f"  {row['env']:<12} {row['status']:<8} box {row['box']}  pinned "
            f"{short(row['pinned']['commit'])}  RUNNING {running}"
        )
        lines.append(f"  {'':<12} {row['reason']}")
    return "\n".join(lines)
