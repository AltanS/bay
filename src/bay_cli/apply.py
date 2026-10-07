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
   still the plan.
8. Push the fleet repo when it has a remote (not with ``--no-push``). A
   failed push is a warning. A fleet that is a subdirectory of a larger repo
   is committed but never pushed (``push_skipped`` says why).

A failed deploy keeps the new pin and records ``result: failed``, so
``bay show`` says HALF. ``bay rollback`` runs the same steps with the
``previous`` commit, which swaps the two pins.
"""

from __future__ import annotations

import copy
import shutil
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from bay_cli import gitrepo, lockfile
from bay_cli import plan as planmod
from bay_cli.context import Context
from bay_cli.errors import BayError, ErrorCode
from bay_cli.fleet import GENERATED_SERVICES

#: ``(cx, box_env, config_files_root=<dir>) -> None``. Raises on a failed
#: deploy. Tests swap in a fake.
Deployer = Callable[..., None]
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


def default_deploy(cx: Context, box_env: str, *, config_files_root: Path | None = None) -> None:
    """Today's ``bay deploy <env> --tags deploy_stack``, without the prompts and the banner.

    ``config_files_root`` is the ``files/`` of the scratch fleet ``bay up``
    compiled; the deploy copies config files from there
    (``bay_config_files_root``), so it ships the committed files the plan read.
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
        ops._run_playbook(cx, "deploy", box_env, UP_DEPLOY_TAGS, extra)
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
    return {
        "commit": commit,
        "deployed_at": record.get("deployed_at"),
        "receipt_sha256": record.get("last_receipt_sha256"),
    }


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
) -> dict[str, Any]:
    """Apply a plan. Returns a result document; raises :class:`Refused` or BayError.

    With ``push`` (the default) the fleet repo is pushed after the last lock
    commit when it has a remote. A failed push is a warning, never a failed
    deploy; the result says ``pushed: false`` and ``push_error``.
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

    verdict = plan["verdict"]
    forced = False
    if verdict == "blocked":
        raise Refused(plan, "the plan is blocked: " + "; ".join(plan["blockers"]))
    if verdict == "stale":
        raise Refused(plan, "the plan is stale: " + "; ".join(plan["stale"]))
    if verdict == "approve":
        if not force:
            raise Refused(
                plan, f"plan {plan['plan_id']} has destructive or shared steps and no approval"
            )
        forced = True
        say(f"forced without approval: {reason}")

    env = str(plan["env"])
    box_env = str(plan["box_env"])
    commit = str(plan["wanted"]["commit"])
    short = commit[:12]
    on_remote = planmod.commit_on_remote(proj, commit)
    if on_remote is not True:
        why = "is not" if on_remote is False else "cannot be checked to be"
        raise Refused(
            plan,
            f"commit {short} of {proj.name} {why} on a branch of {proj.repo}; push first",
        )

    # 2. the lock
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
    if not record.get("box") and plan.get("box"):
        record["box"] = plan["box"]
    record.update({"commit": commit, "result": "pending", "plan_id": plan["plan_id"]})
    if previous is not None:
        record["previous"] = previous
    envs[env] = record
    lockfile.write(proj.lock_file, lock)

    # 3. compile into the fleet. The scratch fleet stays until the deploy
    # has finished: the deploy copies config files from its files/.
    failure: str | None = None
    with planmod.compiled_fleet(cx, cwd=proj.cwd) as comp:
        if comp.result is None:
            raise BayError(
                "compile failed after the lock was written:\n  " + "\n  ".join(comp.errors)
            )
        services = _write_services(cx, comp.result.text())
        compiled_commits = dict(comp.commits)
        left_out = list(comp.unpinned)

        # 4. commit
        paths = [proj.lock_file, services, planmod.plan_file(cx, plan["plan_id"])]
        approval = planmod.approval_file(cx, plan["plan_id"])
        if approval.is_file():
            paths.append(approval)
        try:
            fleet_commit = gitrepo.commit_paths(
                cx.fleet_root, paths, f"bay: {action} {proj.name} {env} {short}"
            )
        except gitrepo.GitError as exc:
            raise BayError(f"cannot commit the fleet repo: {exc}") from None
        say(f"fleet commit {fleet_commit}")

        # 5. deploy
        try:
            (deploy or default_deploy)(cx, box_env, config_files_root=comp.files_root)
        except (BayError, OSError) as exc:
            failure = str(exc) or type(exc).__name__
        except SystemExit as exc:
            failure = f"deploy exited with {exc.code}"

    # 6. receipt
    reader = read_receipts or planmod.default_receipt_reader
    names = set(
        planmod.project_containers(
            proj.name, planmod.doc_at(proj, commit) or {}, lock, proj.primary_env
        )
        .get(env, {})
        .values()
    )
    entries: list[dict[str, Any]] = []
    try:
        entries = reader(cx, box_env)
    except (BayError, OSError) as exc:
        say(f"cannot read the receipt: {exc}")
    slice_ = planmod.running_slice(entries, names)
    receipt_failed = any(
        isinstance(e.get("receipt"), Mapping) and e["receipt"].get("result") == "failed"
        for e in entries
    )
    if failure is None and receipt_failed:
        failure = "the box receipt says the deploy failed"
    word = "failed" if failure else "ok"
    deployed_at = planmod._now()
    record = dict(lock["envs"][env])
    record["result"] = word
    record["deployed_at"] = deployed_at
    record["last_receipt_sha256"] = slice_["receipt_sha256"]
    lock["envs"][env] = record
    lockfile.write(proj.lock_file, lock)
    pinned = [{"project": proj.name, "env": env, "commit": commit, "result": word}]
    lock_files = [proj.lock_file]
    more_files, more_rows = _pin_deployed(
        cx,
        skip=proj.name,
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
    applied, stale_boxes = applied_from(entries, fleet_commit)
    notes += [
        f"box {b}: the receipt is not from this deploy, so `applied` leaves it out"
        for b in stale_boxes
    ]
    for note in notes:
        say(f"note: {note}")
    try:
        receipt_commit = gitrepo.commit_paths(
            cx.fleet_root, lock_files, f"bay: receipt {box_env} ({len(lock_files)} projects)"
        )
    except gitrepo.GitError as exc:
        raise BayError(f"cannot commit the fleet repo: {exc}") from None

    result = {
        "project": proj.name,
        "env": env,
        "box_env": box_env,
        "action": action,
        "plan_id": plan["plan_id"],
        "verdict": verdict,
        "forced": forced,
        "commit": commit,
        "previous_commit": (previous or {}).get("commit"),
        "fleet_commit": fleet_commit,
        "receipt_commit": receipt_commit,
        "result": record["result"],
        "error": failure,
        "pushed": False,
        "push_error": None,
        "push_skipped": None,
        "steps": plan["steps"],
        "applied": applied,
        "pinned": pinned,
        "notes": notes,
    }
    if push and not gitrepo.is_toplevel(cx.fleet_root):
        # A fleet that is a directory inside a bigger repo (a workspace with
        # several fleets) shares that repo's remote and branch. Pushing it
        # would publish everything else committed there too, so bay commits
        # and leaves the push to the operator.
        top = gitrepo.toplevel(cx.fleet_root)
        result["push_skipped"] = (
            f"the fleet lives inside a larger repo ({top}); it was committed, not pushed"
        )
        say(f"warning: {result['push_skipped']}")
    elif push:
        pushed, problem = gitrepo.push(cx.fleet_root)
        result["pushed"], result["push_error"] = pushed, problem
        if problem:
            say(f"warning: the fleet repo was not pushed: {problem}")
        elif pushed:
            say("pushed the fleet repo")
    if failure:
        raise DeployFailed(result)
    return result


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
    skip: str,
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
        if name == skip:
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


def rollback(
    proj: planmod.ProjectRef,
    opts: planmod.PlanOptions,
    **kwargs: Any,
) -> dict[str, Any]:
    env = opts.env or proj.primary_env
    record = (proj.lock.get("envs") or {}).get(env, {})
    previous = record.get("previous")
    if not previous or not previous.get("commit"):
        raise BayError(
            f"{proj.name} has no previous pin for {env}",
            code=ErrorCode.NOT_FOUND,
            hint="A rollback needs one earlier bay up for this environment.",
        )
    restored = planmod.PlanOptions(
        env=env,
        at=str(previous["commit"]),
        read_running=True,
        box_check=opts.box_check,
        allow_unsupported=opts.allow_unsupported,
        cwd_repo=opts.cwd_repo,
    )
    return up(proj, restored, action="rollback", **kwargs)


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
