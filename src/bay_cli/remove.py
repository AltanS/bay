"""``bay remove``: take a project, or one environment of it, out of a fleet.

A remove is a plan like any other (``docs/plan.md``, "bay remove"):

1. ``bay remove <project> [--env <env>]`` compiles the fleet without the
   project (or without that env of it), diffs the result against the fleet's
   services file, and saves a plan with one ``remove`` step per container
   (risk ``destructive``, so the verdict is ``approve``). The plan record has
   a ``remove`` block: the containers per env, and every named volume (its
   name on the box) and database with its role that stay behind.
2. ``bay approve <plan-id> --reason "<why>"``.
3. ``bay up <env> --plan-id <plan-id>`` applies it: the lock's env records
   are set ``pending``, the fleet is compiled without the project, committed
   and deployed. The reconciler removes every container that is no longer in
   the compiled file. Then the receipt is read. Only when it confirms that
   the containers are gone is the env record deleted from the lock; when no
   env is left, ``projects/<name>/`` is removed from the fleet with
   ``git rm``. Otherwise the env record stays with ``result: failed`` and the
   command exits 1: run ``bay remove`` again.

Bay never deletes data. Volumes and databases stay. The plan and the apply
print the ``docker volume rm`` and ``DROP DATABASE`` / ``DROP ROLE`` lines
for the operator to run by hand.

Refused: a ``[resources.*]`` entry and the containers Bay runs on every box
itself (the reverse proxy, the IDS, the update watcher, the gateway, the
registry, the webhook receiver). Blocked: ``--env`` while bay.toml still has
``[deploy.<env>]`` (the next ``bay up`` would start it again), a project
another project needs, and a compile that would change anything else.
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
from bay_cli.fleet import PROJECTS_DIR

#: Containers Bay deploys on every box by its own roles; never a project.
FRAMEWORK_CONTAINERS = (
    "traefik",
    "crowdsec",
    "watchtower",
    "headscale",
    "zot",
    "bay-webhook",
    "tailnet-identity",
)

#: The words in front of every cleanup line.
BY_HAND = "run by hand when you are sure"

Echo = Callable[[str], None]


# ── Names ───────────────────────────────────────────────────────────────────


def refuse_reserved(fleet_doc: Mapping[str, Any], name: str) -> None:
    """Refuse a ``[resources.*]`` entry and a container Bay runs on every box."""
    if name in (fleet_doc.get("resources") or {}):
        raise BayError(
            f"{name} is a shared resource ([resources.{name}] in bay.fleet.toml), not a "
            "project; bay remove does not touch it",
            code=ErrorCode.CONFLICT,
            hint="Edit bay.fleet.toml to take a resource out, then run bay plan.",
        )
    if name in FRAMEWORK_CONTAINERS:
        raise BayError(
            f"{name} is a container Bay runs on every box itself, not a project; "
            "bay remove does not touch it",
            code=ErrorCode.CONFLICT,
            hint="Bay manages it with bay deploy --rig and the fleet's group_vars.",
        )


def stack_name(cx: Context) -> str:
    """``stack_name`` of the fleet (``group_vars/all/main.yml``), else ``bay``.

    The deploy prefixes every named volume with it: ``<stack>_<volume>`` is
    the name on the box.
    """
    import yaml

    try:
        data = yaml.safe_load(cx.main_vars_file.read_text()) or {}
    except (OSError, yaml.YAMLError):
        data = {}
    value = data.get("stack_name") if isinstance(data, dict) else None
    if isinstance(value, str) and value and "{{" not in value:
        return value
    return "bay"


def _needs_of(level: Mapping[str, Any]) -> set[str]:
    value = level.get("needs")
    if isinstance(value, list):
        return {str(v) for v in value}
    if isinstance(value, Mapping):
        return {str(k) for k in value}
    return set()


def _all_needs(doc: Mapping[str, Any]) -> set[str]:
    out = _needs_of(doc)
    for service in (doc.get("services") or {}).values():
        if isinstance(service, Mapping):
            out |= _needs_of(service)
    return out


def _derived_data(
    name: str,
    env: str,
    docs: list[dict[str, Any]],
    record: Mapping[str, Any],
    primary: str,
) -> tuple[list[str], tuple[str, str] | None]:
    """``(volume names, (database, role) or None)`` as the compiler names them.

    The lock's adopted names win, else ``<name>-<volume>`` (``<name>-<env>-<volume>``
    off the primary env) and ``<name>`` (``<name>_<env>``) for the database.
    """
    adopted = (record or {}).get("adopted") or {}
    vols_adopted = adopted.get("volumes") or {}
    is_primary = env == primary
    volumes: list[str] = []
    database: tuple[str, str] | None = None
    for doc in docs:
        if env not in (doc.get("deploy") or {}):
            continue
        levels = [doc, *(v for v in (doc.get("services") or {}).values() if isinstance(v, Mapping))]
        for level in levels:
            for mount in level.get("mounts") or []:
                if isinstance(mount, Mapping) and isinstance(mount.get("volume"), str):
                    vol = mount["volume"]
                    volumes.append(
                        vols_adopted.get(vol)
                        or (f"{name}-{vol}" if is_primary else f"{name}-{env}-{vol}")
                    )
        if "postgres" in _all_needs(doc) and database is None:
            base = name.replace("-", "_")
            derived = base if is_primary else f"{base}_{env}"
            database = (
                str(adopted.get("database") or derived),
                str(adopted.get("role") or derived),
            )
    return list(dict.fromkeys(volumes)), database


def _postgres_on(fleet_doc: Mapping[str, Any], box: str | None) -> str:
    default = fleet_doc.get("default_box")
    for key, res in sorted((fleet_doc.get("resources") or {}).items()):
        if not isinstance(res, Mapping) or res.get("kind") != "postgres":
            continue
        placed = res.get("box", default)
        if box in (placed if isinstance(placed, list) else [placed]):
            return str(key)
    return "postgres"


def cleanup_lines(volumes: list[dict[str, Any]], databases: list[dict[str, Any]]) -> list[str]:
    """The lines an operator runs by hand to delete the data a remove leaves."""
    lines: list[str] = []
    by_box: dict[str, list[str]] = {}
    for vol in volumes:
        by_box.setdefault(str(vol["box"]), []).append(str(vol["name"]))
    for box, names in sorted(by_box.items()):
        lines.append(f"box {box}: docker volume rm {' '.join(names)}")
    for db in databases:
        lines.append(
            f"box {db['box']}, resource {db['resource']}: "
            f"DROP DATABASE {db['name']}; DROP ROLE {db['role']};"
        )
    return lines


# ── The plan ────────────────────────────────────────────────────────────────


def _project_docs(proj: planmod.ProjectRef) -> list[dict[str, Any]]:
    """bay.toml at every commit the lock names: the pin and each env's last commit."""
    lock = proj.lock
    commits = [lock.get("commit")]
    for record in (lock.get("envs") or {}).values():
        if isinstance(record, Mapping):
            commits.append(record.get("commit"))
    docs: list[dict[str, Any]] = []
    for commit in dict.fromkeys(c for c in commits if c):
        doc = planmod.doc_at(proj, str(commit))
        if doc:
            docs.append(doc)
    return docs


def _active(entries: list[dict[str, Any]], names: set[str]) -> set[str]:
    """Names the receipts list as running (a ``remove`` row is not running)."""
    out: set[str] = set()
    for entry in entries:
        receipt = entry.get("receipt")
        if not isinstance(receipt, Mapping):
            continue
        for c in receipt.get("containers") or []:
            if isinstance(c, Mapping) and c.get("name") in names and c.get("action") != "remove":
                out.add(str(c["name"]))
    return out


def _needers(
    cx: Context, name: str, env: str | None, cwd: Path | None
) -> list[str]:
    """Other projects whose compiled bay.toml needs ``name`` (in ``env`` only, when set)."""
    out: list[str] = []
    for other in planmod.fleet_projects(cx):
        if other == name:
            continue
        try:
            oproj = planmod.load_project(cx, other, cwd=cwd, fetch=False)
        except BayError:
            continue
        commit = oproj.lock.get("commit")
        if commit:
            doc = planmod.doc_at(oproj, str(commit))
        elif oproj.in_fleet:
            doc = planmod.read_wanted(oproj, None).doc
        else:
            doc = None  # not compiled: it has no pin yet
        if not doc or name not in _all_needs(doc):
            continue
        if env is not None and env not in (doc.get("deploy") or {}):
            continue
        out.append(other)
    return out


def make_remove_plan(
    cx: Context,
    name: str,
    *,
    env: str | None = None,
    read_running: bool = True,
    cwd: Path | None = None,
    read_receipts: planmod.ReceiptReader | None = None,
) -> dict[str, Any]:
    """The plan that takes ``name`` (or its ``env``) out of the fleet. Does not save it."""
    fleet_doc = planmod.load_fleet_doc(cx)
    refuse_reserved(fleet_doc, name)
    proj = planmod.load_project(cx, name, cwd=cwd)
    primary = proj.primary_env
    state = planmod._fleet_state(cx)
    blockers: list[str] = list(state.blockers)
    notes: list[str] = list(proj.notes)
    lock = proj.lock
    lock_envs: dict[str, Any] = dict(lock.get("envs") or {})

    docs = _project_docs(proj)
    wanted = planmod.read_wanted(proj, None)
    if not docs and proj.in_fleet and wanted.doc is not None:
        # Never pinned: the compile reads the fleet's HEAD for it.
        docs = [wanted.doc]
    known = set(lock_envs) | {e for d in docs for e in (d.get("deploy") or {})}

    target: str | None = None
    if env is not None:
        if env not in known:
            raise BayError(
                f"{name} has no environment {env} (it has: {', '.join(sorted(known)) or 'none'})",
                code=ErrorCode.NOT_FOUND,
            )
        envs = [env]
        blockers.extend(wanted.problems)
        if wanted.doc is not None:
            from bay_cli import bay_toml

            blockers.extend(f"{proj.toml_path}: {v}" for v in bay_toml.validate(wanted.doc))
            if env in (wanted.doc.get("deploy") or {}):
                blockers.append(
                    f"{proj.toml_path} still has [deploy.{env}], so the next bay up would "
                    f"start it again; delete that table, commit it (and push), then run "
                    f"bay remove {name} --env {env} again"
                )
        target = wanted.commit
        if target and planmod.commit_on_remote(proj, target) is not True:
            blockers.append(
                f"commit {target[:12]} is not on a branch of {proj.repo}; push first"
            )
        if target:
            # The pin moves to the commit without [deploy.<env>].
            docs.append(wanted.doc or {})
    else:
        envs = sorted(known, key=lambda e: (e != primary, e))
        if not envs:
            notes.append(f"{name} was never deployed; bay up only deletes it from the fleet")

    for other in _needers(cx, name, env, cwd):
        blockers.append(
            f"{other} needs {name} (needs in its bay.toml); take that need out and run "
            f"bay up for {other} first"
        )

    current, services_state = planmod.current_services(cx)
    planmod._services_blockers(services_state, blockers)
    current_entries = planmod._entries(current)
    stack = stack_name(cx)

    # Per env: box, containers, data.
    names_docs = [*docs, *([wanted.doc] if wanted.doc else [])]
    env_rows: list[dict[str, Any]] = []
    for e in envs:
        doc_e = next((d for d in docs if e in (d.get("deploy") or {})), None)
        box, box_env = planmod.resolve_box(proj, e, doc_e)
        if box_env is None:
            blockers.append(f"{name} {e}: box {box} is not in bay.fleet.toml [boxes]")
        names: set[str] = set()
        for d in names_docs:
            names |= set(planmod.project_containers(name, d, lock, primary).get(e, {}).values())
        env_rows.append(
            {"env": e, "box": box, "box_env": box_env, "containers": sorted(names)}
        )
    all_names = {n for row in env_rows for n in row["containers"]}

    # RUNNING: every box env the project runs on.
    entries: list[dict[str, Any]] = []
    running: dict[str, Any] = {"checked": False, "receipt_sha256": None, "boxes": []}
    box_envs = sorted({str(r["box_env"]) for r in env_rows if r["box_env"]})
    if read_running and box_envs:
        reader = read_receipts or planmod.default_receipt_reader
        for be in box_envs:
            entries.extend(reader(cx, be))
        running = planmod.running_slice(entries, all_names)
        for b in running["boxes"]:
            if b.get("error"):
                notes.append(f"box {b.get('box')}: {b['error']}")
    elif not read_running:
        notes.append(
            "the box receipt was not read (--no-remote); the steps list what the fleet's "
            "services file holds"
        )
    active = _active(entries, all_names)

    # The data that stays.
    volumes: list[dict[str, Any]] = []
    databases: list[dict[str, Any]] = []
    for row in env_rows:
        e, box = row["env"], row["box"]
        vols, db = _derived_data(name, e, names_docs, lock_envs.get(e) or {}, primary)
        resource = None
        for n in row["containers"]:
            entry = current_entries.get(n) or {}
            vols.extend(v for v in planmod._named_volumes(entry) if v not in vols)
            found = entry.get("database")
            if isinstance(found, Mapping) and found.get("name"):
                resource = str(found.get("accessory") or "") or None
                db = db or (str(found["name"]), str(found.get("user") or found["name"]))
        for v in vols:
            volumes.append({"env": e, "box": box, "name": f"{stack}_{v}"})
        if db is not None:
            databases.append(
                {
                    "env": e,
                    "box": box,
                    "resource": resource or _postgres_on(fleet_doc, box),
                    "name": db[0],
                    "role": db[1],
                }
            )

    # Steps: one remove per container that the fleet compiled or the box runs.
    steps: list[dict[str, Any]] = []
    for row in env_rows:
        e = row["env"]
        left = [v["name"] for v in volumes if v["env"] == e]
        dbs = [d for d in databases if d["env"] == e]
        stays = []
        if left:
            stays.append(f"volume {', '.join(left)}")
        stays += [f"database {d['name']} (role {d['role']}) in {d['resource']}" for d in dbs]
        for n in row["containers"]:
            if n not in current_entries and n not in active:
                continue
            reason = f"{name} leaves {e}: the container on box {row['box']} is removed"
            reason += "; " + " and ".join(stays) + " stay" if stays else "; it has no data"
            steps.append(
                planmod._step(
                    "container", "remove", "destructive", reason, container=n, project=name
                )
            )

    # The compile without the project must change nothing else.
    pins = {name: target} if target else {}
    drop: dict[str, set[str] | None] = {name: set(envs)} if env is not None else {name: None}
    with planmod.compiled_fleet(cx, pins, cwd=cwd, drop=drop) as comp:
        blockers.extend(comp.errors)
        notes.extend(n for n in comp.notes if not n.startswith(f"{name} "))
        notes.extend(comp.uncommitted)
        if comp.result is not None:
            from bay_cli import routes

            wanted_data = comp.result.data()
            still = sorted(all_names & set(planmod._entries(wanted_data)))
            if still:
                blockers.append(
                    "the compile without the project still holds " + ", ".join(still)
                )
            diff = planmod.diff_steps(
                current,
                wanted_data,
                project=name,
                mine=all_names,
                resources=set((fleet_doc.get("resources") or {}).keys()),
                running=None,
                owners=comp.owners,
            )
            extra = [
                s
                for s in diff
                if not (s["kind"] == "container" and s["action"] == "remove"
                        and s["container"] in all_names)
            ]
            extra += routes.route_steps(current, wanted_data)
            if extra:
                what = "; ".join(
                    f"{s['kind']} {s['container'] or s['resource']} {s['action']}"
                    for s in extra
                )
                blockers.append(
                    f"the compile also changes what bay remove does not cover ({what}); "
                    "apply that with bay up first, then run bay remove again"
                )
    if state.is_git and planmod.tailnet_step(cx, fleet_doc) is not None:
        blockers.append(
            "the tailnet allowlist in bay.fleet.toml changed since the last fleet commit; "
            "apply it with bay up first"
        )

    whole = env is None
    plan_env = env or (envs[0] if envs else primary)
    first = next((r for r in env_rows if r["env"] == plan_env), None)
    rm = {
        "project": name,
        "scope": "project" if whole else "env",
        "in_fleet": proj.in_fleet,
        "commit": target,
        "envs": env_rows,
        "volumes": volumes,
        "databases": databases,
        "cleanup": cleanup_lines(volumes, databases),
    }
    if volumes or databases:
        notes.append(
            "Bay never deletes data: the volumes and the database stay; the cleanup lines "
            f"below are to be {BY_HAND}"
        )
    plan: dict[str, Any] = {
        "plan_version": planmod.PLAN_VERSION,
        "plan_id": "",
        "plan_sha256": "",
        "created_at": planmod._now(),
        "project": name,
        "env": plan_env,
        "box": first["box"] if first else None,
        "box_env": first["box_env"] if first else None,
        "fleet": {
            "name": fleet_doc.get("name"),
            "commit": state.head,
            "dirty": state.dirty,
            "behind": state.behind,
        },
        "wanted": {
            "commit": target,
            "toml_sha256": wanted.toml_sha256 if target else None,
            "dirty": None,
        },
        "pinned": {
            "commit": lockfile.env_pin(lock, plan_env),
            "lock_sha256": lockfile.sha256_of(proj.lock_file),
        },
        "running": running,
        "box_checked": False,
        "box_prediction": {"checked": False, "containers": [], "errors": []},
        "steps": steps,
        "unsupported": [],
        "missing_secrets": [],
        "blockers": blockers,
        "notes": notes,
        "verdict": "",
        "exit_code": 0,
        "approval": None,
        "stale": [],
        "remove": rm,
    }
    for i, step in enumerate(steps, start=1):
        step["id"] = f"s{i}"
    if state.dirty and steps:
        blockers.append(
            "the fleet repo has uncommitted changes and a step is destructive; "
            "commit or drop the changes first"
        )
    sha = planmod.body_sha256(plan)
    plan["plan_sha256"] = sha
    plan["plan_id"] = sha[:12]
    plan["approval"] = planmod.find_approval(cx, plan)
    planmod.decide(plan)
    return plan


def recheck_remove(
    cx: Context,
    saved: Mapping[str, Any],
    *,
    cwd: Path | None = None,
    read_receipts: planmod.ReceiptReader | None = None,
) -> dict[str, Any]:
    """Plan the saved remove again; mark it stale when it moved."""
    rm = saved.get("remove")
    if not isinstance(rm, Mapping):
        raise BayError(f"plan {saved.get('plan_id')} is not a bay remove plan")
    env = str(rm["envs"][0]["env"]) if rm.get("scope") == "env" and rm.get("envs") else None
    fresh = make_remove_plan(
        cx,
        str(saved["project"]),
        env=env,
        read_running=bool((saved.get("running") or {}).get("checked")),
        cwd=cwd,
        read_receipts=read_receipts,
    )
    fresh["stale"] = planmod.stale_reasons(saved, fresh)
    planmod.decide(fresh)
    return fresh


# ── Apply (bay up --plan-id) ────────────────────────────────────────────────


def _gone(
    entries: list[dict[str, Any]], names: set[str], fleet_commit: str
) -> tuple[bool, str]:
    """Does every receipt of this deploy say the containers ``names`` are gone?"""
    if not entries:
        return False, "no receipt was read"
    for entry in entries:
        box = entry.get("box") or entry.get("env")
        if entry.get("error"):
            return False, f"box {box}: {entry['error']}"
        receipt = entry.get("receipt")
        if not isinstance(receipt, Mapping):
            return False, f"box {box} has no receipt"
        if receipt.get("fleet_commit") != fleet_commit:
            return False, f"box {box}: the receipt is not from this deploy"
        for c in receipt.get("containers") or []:
            if isinstance(c, Mapping) and c.get("name") in names and c.get("action") != "remove":
                return False, f"box {box} still runs {c.get('name')}"
    return True, ""


def apply_remove(
    cx: Context,
    plan_id: str,
    *,
    env: str | None = None,
    force: bool = False,
    reason: str | None = None,
    cwd: Path | None = None,
    read_receipts: planmod.ReceiptReader | None = None,
    deploy: Any = None,
    echo: Echo | None = None,
    push: bool = True,
) -> dict[str, Any]:
    """``bay up --plan-id`` for a remove plan. Returns the result; ``result`` is ok or failed.

    Raises :class:`bay_cli.apply.Refused` for a blocked, stale or unapproved plan.
    """
    from bay_cli import apply as applymod

    say = echo or (lambda _msg: None)
    if force and not (reason and reason.strip()):
        raise BayError("--force needs --reason", hint='Pass --reason "<why>".')
    moved = applymod._migrate_layout(cx, say)
    saved = planmod.load_saved(cx, plan_id)
    rm0 = saved.get("remove")
    if not isinstance(rm0, Mapping):
        raise BayError(f"plan {plan_id} is not a bay remove plan")
    planned_envs = [str(r["env"]) for r in rm0.get("envs") or []]
    if env is not None and env != saved.get("env") and env not in planned_envs:
        raise BayError(
            f"plan {plan_id} removes {saved.get('project')} from "
            f"{', '.join(planned_envs) or 'the fleet'}, not from {env}",
            hint=f"Run `bay up {saved.get('env')} --plan-id {plan_id}`.",
        )
    plan = recheck_remove(cx, saved, cwd=cwd, read_receipts=read_receipts)
    planmod.save(cx, plan)
    forced = applymod._gate(plan, force, reason, say)
    rm = plan["remove"]
    name = str(plan["project"])
    proj = planmod.load_project(cx, name, cwd=cwd, fetch=False)
    lock_exists = proj.lock_file.is_file()
    rows: list[dict[str, Any]] = list(rm["envs"])
    box_envs = sorted({str(r["box_env"]) for r in rows if r["box_env"]})

    # 2. the lock: every env it removes is pending.
    lock = copy.deepcopy(proj.lock)
    if lock_exists:
        if rm.get("commit"):
            lock["commit"] = rm["commit"]
        envs = lock.setdefault("envs", {})
        for row in rows:
            record = dict(envs.get(row["env"]) or {})
            record.update({"result": "pending", "plan_id": plan["plan_id"]})
            envs[row["env"]] = record
        lockfile.write(proj.lock_file, lock)

    # 3-5. compile without it, commit, deploy every box env it ran on.
    whole = rm["scope"] == "project"
    drop: dict[str, set[str] | None] = (
        {name: None} if whole else {name: {str(r["env"]) for r in rows}}
    )
    pins = {name: str(rm["commit"])} if rm.get("commit") else {}
    failure: str | None = None
    scope_text = "" if whole else " " + ", ".join(str(r["env"]) for r in rows)
    with planmod.compiled_fleet(cx, pins, cwd=cwd, drop=drop) as comp:
        if comp.result is None:
            raise BayError(
                "compile failed after the lock was written:\n  " + "\n  ".join(comp.errors)
            )
        services = applymod._write_services(cx, comp.result.text())
        paths = [services, planmod.plan_file(cx, plan["plan_id"])]
        if lock_exists:
            paths.append(proj.lock_file)
        approval = planmod.approval_file(cx, plan["plan_id"])
        if approval.is_file():
            paths.append(approval)
        try:
            fleet_commit = gitrepo.commit_paths(
                cx.fleet_root, paths, f"bay: remove {name}{scope_text} (plan {plan['plan_id']})"
            )
        except gitrepo.GitError as exc:
            raise BayError(f"cannot commit the fleet repo: {exc}") from None
        say(f"fleet commit {fleet_commit}")
        for target in box_envs:
            try:
                (deploy or applymod.default_deploy)(
                    cx, target, config_files_root=comp.files_root
                )
            except (BayError, OSError) as exc:
                failure = str(exc) or type(exc).__name__
            except SystemExit as exc:
                failure = f"deploy exited with {exc.code}"
            if failure is not None:
                # Stop at the first failure, as bay up does.
                skipped = box_envs[box_envs.index(target) + 1 :]
                if skipped:
                    say(f"not deployed after the failure: {', '.join(skipped)}")
                break

    # 6. the receipt decides.
    reader = read_receipts or planmod.default_receipt_reader
    by_env: dict[str, list[dict[str, Any]]] = {}
    for target in box_envs:
        try:
            by_env[target] = reader(cx, target)
        except (BayError, OSError) as exc:
            say(f"cannot read the receipt of {target}: {exc}")
            by_env[target] = []
    if failure is None and any(
        isinstance(e.get("receipt"), Mapping) and e["receipt"].get("result") == "failed"
        for entries in by_env.values()
        for e in entries
    ):
        failure = "the box receipt says the deploy failed"
    confirmed: list[str] = []
    unconfirmed: dict[str, str] = {}
    for row in rows:
        if row["box_env"] is None:
            confirmed.append(str(row["env"]))
            continue
        entries = by_env.get(str(row["box_env"])) or []
        gone, why = _gone(entries, set(row["containers"]), fleet_commit)
        if failure:
            unconfirmed[str(row["env"])] = failure
        elif gone:
            confirmed.append(str(row["env"]))
        else:
            unconfirmed[str(row["env"])] = why

    removed_files: list[str] = []
    receipt_commit = fleet_commit
    folder = f"{PROJECTS_DIR}/{name}"
    remaining = sorted(set(lock.get("envs") or {}) - set(confirmed))
    try:
        if not unconfirmed and (whole or not remaining):
            removed_files = gitrepo.tracked_files(cx.fleet_root, folder)
            if removed_files:
                receipt_commit = gitrepo.remove_and_commit(
                    cx.fleet_root, removed_files, f"bay: remove {name}: the receipt confirms it"
                )
            leftover = cx.fleet_root / folder
            if leftover.is_dir() and not any(leftover.iterdir()):
                shutil.rmtree(leftover, ignore_errors=True)
        elif lock_exists:
            envs = lock.setdefault("envs", {})
            stamp = planmod._now()
            for e in confirmed:
                envs.pop(e, None)
            for e in unconfirmed:
                record = dict(envs.get(e) or {})
                record.update({"result": "failed", "deployed_at": stamp})
                envs[e] = record
            lockfile.write(proj.lock_file, lock)
            what = "receipt" if unconfirmed else "removed"
            receipt_commit = gitrepo.commit_paths(
                cx.fleet_root, [proj.lock_file], f"bay: {what} {name}{scope_text}"
            )
    except gitrepo.GitError as exc:
        raise BayError(f"cannot commit the fleet repo: {exc}") from None

    # 7. plans/ prune, 8. push.
    pruned: list[str] = []
    try:
        pruned = applymod.prune_plans(cx)
    except (gitrepo.GitError, applymod.PruneRefused) as exc:
        say(f"warning: plans/ was not pruned: {exc}")
    applied: list[dict[str, Any]] = []
    for entries in by_env.values():
        applied += applymod.applied_from(entries, fleet_commit)[0]
    notes = [f"{e}: not confirmed: {why}" for e, why in sorted(unconfirmed.items())]
    if unconfirmed:
        notes.append(
            f"the lock keeps {', '.join(sorted(unconfirmed))} with result failed; "
            f"run bay remove {name} again"
        )
    result: dict[str, Any] = {
        "project": name,
        "action": "remove",
        "scope": rm["scope"],
        "env": plan["env"],
        "envs": [str(r["env"]) for r in rows],
        "box_envs": box_envs,
        "plan_id": plan["plan_id"],
        "verdict": plan["verdict"],
        "forced": forced,
        "fleet_commit": fleet_commit,
        "receipt_commit": receipt_commit,
        "result": "failed" if unconfirmed else "ok",
        "error": failure,
        "confirmed": confirmed,
        "unconfirmed": unconfirmed,
        "lock_removed": bool(removed_files) and not unconfirmed,
        "removed_files": removed_files,
        "volumes": rm["volumes"],
        "databases": rm["databases"],
        "cleanup": rm["cleanup"],
        "steps": plan["steps"],
        "applied": applied,
        "pruned": pruned,
        "notes": notes,
        "pushed": False,
        "push_error": None,
        "push_skipped": None,
    }
    if push:
        applymod.push_fleet(cx, result, say)
    return applymod._with_layout_notes(moved, result)


# ── Human output ────────────────────────────────────────────────────────────


def _what(rm: Mapping[str, Any]) -> str:
    envs = ", ".join(str(r["env"]) for r in rm["envs"]) or "no env"
    if rm["scope"] == "env":
        return f"{rm['project']} ({envs})"
    return f"{rm['project']} ({envs}, the whole project)"


def render_cleanup(cleanup: list[str]) -> list[str]:
    if not cleanup:
        return ["no volume and no database stay behind"]
    return [f"to delete the data later, {BY_HAND}:", *(f"  {line}" for line in cleanup)]


def render_plan(plan: Mapping[str, Any]) -> str:
    rm = plan["remove"]
    lines = [planmod.render(plan), "", f"remove: {_what(rm)}"]
    lines += [f"  stays: volume {v['name']} on box {v['box']}" for v in rm["volumes"]]
    lines += [
        f"  stays: database {d['name']} and role {d['role']} in {d['resource']} on box {d['box']}"
        for d in rm["databases"]
    ]
    lines += render_cleanup(list(rm["cleanup"]))
    if plan["verdict"] in ("approve", "auto"):
        lines.append(f"then: bay up {plan['env']} --plan-id {plan['plan_id']}")
    return "\n".join(lines)
