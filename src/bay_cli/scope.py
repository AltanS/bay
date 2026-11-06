"""Which boxes a ``bay up <env> --project <p>`` runs on (2.13.0).

A run or plan with ``--project`` has a **scope**: the fleet boxes it needs.
The playbook, the box check of ``bay plan --remote``, the receipt read after
the deploy and the health check run only on the inventory hosts of those
boxes (``ansible-playbook -l <hosts>``). A box is in the scope when one of
these holds (``docs/plan.md``, "Which boxes a run touches"):

1. It is the box of the project.
2. It is the old box of a move.
3. The plan has a route step and the box is ``[tailnet] ingress_box``.
4. The build surface changed (:func:`changed_builds`): every box that renders
   something a push build depends on (:func:`receiver_boxes`).
5. The plan has a compile step for a container of another project (or of no
   project) on that box. A planned step must run where it is planned.

A box that shares an inventory host with a box of the scope is in the scope
too: Ansible deploys hosts, not boxes, so the run reaches it anyway.

No scope (no ``-l``): a run with no project, ``--all-boxes``, a scope that
holds every host of the box env, a box env with one box, a box env with no
host map, a plan with a tailnet allowlist step, and a planned step that Bay
cannot place on a box.

The scope is computed once, in the plan, and is part of the plan id.
``bay up`` deploys exactly the scope of its plan (``limits``).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bay_cli import gitrepo
from bay_cli.context import Context

PROJECT_BOX = "project box"
OLD_BOX = "old box of the move"
INGRESS_BOX = "ingress box"
BUILD_SERVER = "build server"
WEBHOOK_BOX = "webhook box"
PLANNED_STEP = "planned step"
SHARED_HOST = "shares a host"
#: When a box has two reasons, the first of this order names it.
_ORDER = (PROJECT_BOX, OLD_BOX, INGRESS_BOX, BUILD_SERVER, WEBHOOK_BOX, PLANNED_STEP, SHARED_HOST)

#: ``why`` of a scope with ``all_boxes``: the run has no ``-l``.
WHY_FLAG = "--all-boxes"
WHY_NO_PROJECT = "no project: the run covers the whole environment"
WHY_EVERY_HOST = "the scope holds every host of the box environment"
WHY_TAILNET = "the plan changes the tailnet allowlist of every box"
WHY_UNPLACED = "a planned step cannot be placed on one box"
WHY_NO_MAP = "no host map"
WHY_BUILD_SERVER = "the build server cannot be read from group_vars"

#: The Ansible extra var a limited run sets. ``deploy.yml`` reads it, so the
#: build server bootstrap play (``run_once`` + ``delegate_to: build_server``)
#: skips a build server outside the limit. ``bay deploy --region`` never sets it.
LIMITED_VAR = "bay_scope_limited"

#: ``(cx, box_env) -> ({inventory host: [fleet box]} | None, problem)``.
HostMap = Callable[[Context, str], "tuple[dict[str, list[str]] | None, str | None]"]


def limit_args(hosts: Sequence[str]) -> list[str]:
    """``ansible-playbook`` arguments for a limited run: ``-l h1,h2`` and the flag var."""
    return ["-l", ",".join(hosts), "-e", f"{LIMITED_VAR}=true"]


def whole(
    why: str, boxes: Iterable[str] = (), reasons: Mapping[str, str] | None = None
) -> dict[str, Any]:
    """A scope with no limit: every host of every box env the run deploys."""
    chosen = sorted(set(boxes))
    return {
        "all_boxes": True,
        "boxes": chosen,
        "hosts": [],
        "skipped_boxes": [],
        "reasons": {b: r for b, r in sorted((reasons or {}).items()) if b in chosen},
        "limits": {},
        "why": why,
    }


def is_limited(scope: Mapping[str, Any] | None) -> bool:
    """True when the scope has a limit: the run deploys only some hosts."""
    if not scope:
        return False
    return not scope.get("all_boxes", True) and bool(scope.get("limits"))


def limit_for(scope: Mapping[str, Any] | None, box_env: str) -> list[str] | None:
    """The hosts of ``box_env`` a run of ``scope`` deploys, or None: the whole box env."""
    if not is_limited(scope):
        return None
    assert scope is not None
    hosts = (scope.get("limits") or {}).get(box_env)
    return list(hosts) if hosts else None


def host_map(cx: Context, box_env: str) -> tuple[dict[str, list[str]] | None, str | None]:
    """``({inventory host: [fleet box]}, problem)`` for ``box_env``.

    :func:`bay_cli.receipts.host_boxes` over the fleet's ``[boxes]``, with the
    inventory read once, as the deploy reads it. The map is None, and
    ``problem`` says why, when the inventory cannot be read, when it has no
    host in the box env, or when a box of the box env maps to no host.
    """
    from bay_cli import plan as planmod
    from bay_cli import receipts

    try:
        boxes = planmod.load_fleet_doc(cx).get("boxes") or {}
        names = receipts.inventory_names(cx.fleet_root, cx.framework_root)
    except Exception as exc:  # the inventory reader raises many types
        return None, f"cannot read the inventory of {box_env}: {exc}"
    if not names.get(box_env):
        return None, f"the inventory has no host in {box_env}"
    mapping = receipts.host_boxes(cx.fleet_root, boxes, box_env, names=names)
    placed = {b for found in mapping.values() for b in found}
    lost = [b for b in env_boxes(boxes, box_env) if b not in placed]
    if lost:
        return None, f"box {', '.join(lost)} of {box_env} maps to no inventory host"
    return mapping, None


def env_boxes(boxes: Mapping[str, Any], box_env: str) -> list[str]:
    return sorted(
        n for n, b in boxes.items() if isinstance(b, Mapping) and b.get("env") == box_env
    )


# ── rule 4: the build surface ───────────────────────────────────────────────


def _entries(data: Mapping[str, Any] | None) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for section in ("accessories", "services"):
        block = (data or {}).get(section) or {}
        if isinstance(block, Mapping):
            for key, value in block.items():
                if isinstance(value, Mapping):
                    out[str(key)] = dict(value)
    return out


def changed_builds(current: Mapping[str, Any], wanted: Mapping[str, Any]) -> list[str]:
    """Containers with a ``build`` whose compiled entry differs between the two files.

    Added, removed, or any key changed: the whole entry is compared. A pin
    move with no bay.toml change compiles the same entry, so it is not here.
    """
    old, new = _entries(current), _entries(wanted)
    out = []
    for name in sorted(set(old) | set(new)):
        a, b = old.get(name), new.get(name)
        if a == b:
            continue
        if (a is not None and "build" in a) or (b is not None and "build" in b):
            out.append(name)
    return out


def _builds(*datas: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [e for d in datas for e in _entries(d).values() if "build" in e]


def _remote_builds(builds: list[dict[str, Any]], default_strategy: str) -> bool:
    """True when a build is in the global remote list (``roles/git_deploy/tasks/main.yml``)."""
    for entry in builds:
        build = entry.get("build")
        strategy = build.get("strategy") if isinstance(build, Mapping) else None
        if (strategy or default_strategy) in ("remote", "push"):
            return True
    return False


def receiver_boxes(
    boxes: Mapping[str, Any], box_env: str, builds: list[dict[str, Any]]
) -> list[str]:
    """The boxes of ``box_env`` that run a webhook receiver for these builds.

    A host renders the receiver (``webhook-config.json``, ``image-map.json``,
    ``rebuild.sh``, the trigger units and the ``bay-webhook`` container) when a
    service with ``build`` is active on it (no ``regions``, or one of its
    groups in ``regions``). ``webhook-config.json`` lists every service with a
    ``build`` of the whole services file on every such box, and the receiver's
    config hash covers it, so a build change re-renders all of them.
    """
    out = []
    for name in env_boxes(boxes, box_env):
        group = (boxes.get(name) or {}).get("group")
        for entry in builds:
            regions = entry.get("regions") or []
            if not regions or group is None or group in regions:
                out.append(name)
                break
    return out


# ── rule 5: planned steps of other projects ─────────────────────────────────


def _step_regions(
    step: Mapping[str, Any], current: Mapping[str, Any], wanted: Mapping[str, Any]
) -> list[str] | None:
    """The ``regions`` of the entry a compile step changes; [] for every box, None: unknown."""
    kind = step.get("kind")
    name = step.get("container") or step.get("resource")
    if kind == "job":
        jobs = [d.get("jobs") for d in (wanted, current)]
        job = next((j[name] for j in jobs if isinstance(j, Mapping) and name in j), None)
        name = job.get("of") if isinstance(job, Mapping) else None
    if not name:
        return None
    entry = _entries(wanted).get(str(name)) or _entries(current).get(str(name))
    if entry is None:
        return None
    regions = entry.get("regions")
    return [str(r) for r in regions] if isinstance(regions, list) else []


def _groups(boxes: Mapping[str, Any], names: Iterable[str]) -> dict[str, str | None]:
    return {n: (boxes.get(n) or {}).get("group") for n in names}


# ── the scope ───────────────────────────────────────────────────────────────


@dataclass
class Result:
    scope: dict[str, Any]
    notes: list[str] = field(default_factory=list)
    #: ``{box_env: {inventory host: [fleet box]}}`` for each env whose map was read.
    maps: dict[str, dict[str, list[str]]] = field(default_factory=dict)


def compute(
    cx: Context,
    fleet_doc: Mapping[str, Any],
    *,
    project: str | None,
    box: str | None,
    box_env: str | None,
    moves: Sequence[tuple[str, str | None]],
    steps: Sequence[Mapping[str, Any]],
    current: Mapping[str, Any],
    wanted: Mapping[str, Any] | None,
    all_boxes: bool = False,
    read_map: HostMap | None = None,
    group_var: Callable[[str, str], Any] | None = None,
) -> Result:
    """The scope of a one-project plan. See the module docstring for the rules.

    ``moves`` is ``[(old box, old box env)]``; ``wanted`` is the compiled file
    this run writes (None: the compile did not run, rule 4 is left out).
    ``group_var(box_env, key)`` reads plain group_vars (``build_server``,
    ``git_deploy_build_strategy``).
    """
    boxes: Mapping[str, Any] = fleet_doc.get("boxes") or {}
    if box_env is None:
        return Result(whole(WHY_NO_PROJECT))
    envs = [box_env] + sorted({e for _, e in moves if e and e != box_env})
    by_env = {e: env_boxes(boxes, e) for e in envs}
    everything = sorted({b for names in by_env.values() for b in names})
    reasons: dict[str, str] = {}

    def add(name: str | None, why: str) -> None:
        if not name or name not in everything:
            return
        if name not in reasons or _ORDER.index(why) < _ORDER.index(reasons[name]):
            reasons[name] = why

    add(box, PROJECT_BOX)
    for old, _ in moves:
        add(old, OLD_BOX)
    if any(s.get("kind") == "route" for s in steps):
        ingress = (fleet_doc.get("tailnet") or {}).get("ingress_box")
        add(ingress if isinstance(ingress, str) else None, INGRESS_BOX)

    if all_boxes:
        return Result(whole(WHY_FLAG, everything, reasons))
    if any(s.get("kind") == "tailnet" for s in steps):
        return Result(
            whole(WHY_TAILNET, everything, reasons),
            [f"this run deploys every box of {', '.join(envs)}: {WHY_TAILNET}"],
        )

    # Rule 5: a compile step of another project (or of none) runs where it is planned.
    for step in steps:
        if step.get("source") == "box" or step.get("kind") in ("route", "move", "tailnet"):
            continue
        if project is not None and step.get("project") == project:
            continue  # this project's own containers: its box (rule 1) or the old box (rule 2)
        regions = (
            None if step.get("kind") == "fleet" else _step_regions(step, current, wanted or {})
        )
        if not regions:
            what = step.get("container") or step.get("resource")
            why = f"{WHY_UNPLACED} (step {step.get('kind')} {what})"
            return Result(
                whole(WHY_UNPLACED, everything, reasons),
                [f"this run deploys every box of {', '.join(envs)}: {why}"],
            )
        for name, group in _groups(boxes, everything).items():
            if group in regions:
                add(name, PLANNED_STEP)

    changed = changed_builds(current, wanted) if wanted is not None else []
    hooked = wanted is not None and (current.get("webhook") or wanted.get("webhook"))
    builds = _builds(current, wanted or {}) if changed and hooked else []

    read = read_map or host_map
    notes: list[str] = []
    maps: dict[str, dict[str, list[str]]] = {}
    limits: dict[str, list[str]] = {}
    unlimited: list[str] = []
    why = WHY_EVERY_HOST
    for env in envs:
        if len(by_env[env]) <= 1:
            # One box: today's run, no inventory read and no -l.
            unlimited.append(env)
            continue
        mapping, problem = read(cx, env)
        if mapping is None:
            notes.append(
                f"per-box scope is not available for {env} ({problem}); this run deploys "
                f"every box of {env}"
            )
            unlimited.append(env)
            why = WHY_NO_MAP
            continue
        maps[env] = mapping
        if builds:
            reader = group_var or (lambda _e, _k: None)
            server = reader(env, "build_server")
            strategy = reader(env, "git_deploy_build_strategy") or "local"
            if (isinstance(server, str) and "{{" in server) or (
                isinstance(strategy, str) and "{{" in strategy
            ):
                notes.append(
                    f"this run deploys every box of {env}: the build surface changed and "
                    f"{WHY_BUILD_SERVER} (a Jinja value)"
                )
                unlimited.append(env)
                why = WHY_BUILD_SERVER
                continue
            if server is not None and _remote_builds(builds, str(strategy)):
                for found in mapping.get(str(server), []):
                    add(found, BUILD_SERVER)
            for name in receiver_boxes(boxes, env, builds):
                add(name, WEBHOOK_BOX)
        chosen = {b for b in reasons if b in by_env[env]}
        # Ansible limits hosts, not boxes: a box on a host of the scope runs too.
        while True:
            hosts = {h for h, found in mapping.items() if set(found) & chosen}
            more = {b for h in hosts for b in mapping[h]} - chosen
            if not more:
                break
            for name in more:
                add(name, SHARED_HOST)
            chosen |= more
        if hosts >= set(mapping):
            unlimited.append(env)
            continue
        limits[env] = sorted(hosts)

    if not limits:
        return Result(whole(why, everything, reasons), notes, maps)
    in_scope = {b for b in reasons} | {b for e in unlimited for b in by_env[e]}
    chosen_all = sorted(b for b in everything if b in in_scope)
    return Result(
        {
            "all_boxes": False,
            "boxes": chosen_all,
            "hosts": sorted({h for hs in limits.values() for h in hs}),
            "skipped_boxes": sorted(b for b in everything if b not in in_scope),
            "reasons": {b: reasons[b] for b in chosen_all if b in reasons},
            "limits": {e: limits[e] for e in sorted(limits)},
            "why": None,
        },
        notes,
        maps,
    )


def scope_note(scope: Mapping[str, Any], env: str) -> str | None:
    """The note of a limited scope (it replaces ``plan.UNPLANNED_NOTE``)."""
    if not is_limited(scope):
        return None
    return (
        f"this run deploys box {', '.join(scope['boxes'])} only and skips "
        f"{', '.join(scope['skipped_boxes']) or 'no box'}. Fleet-level changes (the shared "
        "proxy, the gateway, the update watcher) reach the skipped boxes only with "
        f"`bay up {env}` or --all-boxes"
    )


def groups_of(fleet_doc: Mapping[str, Any], names: Iterable[str]) -> set[str]:
    boxes = fleet_doc.get("boxes") or {}
    return {str(g) for g in _groups(boxes, names).values() if g}


# ── fleet drift on skipped boxes ────────────────────────────────────────────

#: Fleet paths a deploy does not read from, or that every bay up rewrites.
_NOT_DRIFT_PREFIXES = ("plans/",)
_NOT_DRIFT_FILES = ("group_vars/all/services.yml",)


def _is_drift_path(path: str) -> bool:
    from bay_cli.fleet import LOCK_FILE, LOCK_SUFFIX, PROJECTS_DIR

    if path in _NOT_DRIFT_FILES or path.startswith(_NOT_DRIFT_PREFIXES):
        return False
    parts = path.split("/")
    if parts[0] == PROJECTS_DIR and (
        (len(parts) == 3 and parts[2] == LOCK_FILE)
        or (len(parts) == 2 and parts[1].endswith(LOCK_SUFFIX))
    ):
        return False
    return True


def fleet_drift(fleet_root: Path, fleet_commit: str | None) -> dict[str, Any] | None:
    """The fleet files that changed between a box's ``fleet_commit`` and the fleet HEAD.

    Leaves out the lock files (the committed receipts), ``plans/`` and the
    compiled ``group_vars/all/services.yml``. ``{"fleet_commit", "known",
    "paths"}``; ``known`` is false when the local repo does not know the
    commit. None when there is no commit to compare.
    """
    if not fleet_commit:
        return None
    head = gitrepo.head(fleet_root)
    if head is not None and head == fleet_commit:
        return {"fleet_commit": fleet_commit, "known": True, "paths": []}
    if gitrepo.resolve_commit(fleet_root, f"{fleet_commit}^{{commit}}") is None:
        return {"fleet_commit": fleet_commit, "known": False, "paths": []}
    out = gitrepo._out(
        fleet_root, "-c", "core.quotepath=off", "diff", "--name-only", "--relative",
        f"{fleet_commit}..HEAD",
    )
    if out is None:
        return {"fleet_commit": fleet_commit, "known": False, "paths": []}
    paths = sorted(p for p in out.splitlines() if p and _is_drift_path(p))
    return {"fleet_commit": fleet_commit, "known": True, "paths": paths}


def box_commit(
    entries: Sequence[Mapping[str, Any]], hosts: Iterable[str] | None
) -> str | None:
    """The ``fleet_commit`` of the first receipt of ``hosts`` (None: every entry)."""
    wanted = None if hosts is None else set(hosts)
    for entry in sorted(entries, key=lambda e: str(e.get("box"))):
        if wanted is not None and str(entry.get("box")) not in wanted:
            continue
        receipt = entry.get("receipt")
        if isinstance(receipt, Mapping) and receipt.get("fleet_commit"):
            return str(receipt["fleet_commit"])
    return None


def drift_line(name: str, drift: Mapping[str, Any]) -> str | None:
    """One line about a box's fleet drift, or None when there is none."""
    commit = str(drift["fleet_commit"])[:12]
    if not drift["known"]:
        return f"box {name}: unknown fleet commit {commit} (not in the local fleet repo)"
    paths = list(drift["paths"])
    if not paths:
        return None
    shown = ", ".join(paths[:3]) + (", ..." if len(paths) > 3 else "")
    return (
        f"box {name} runs fleet commit {commit}; {len(paths)} fleet file(s) changed "
        f"since then ({shown})"
    )


def drift_notes(
    fleet_root: Path,
    scope: Mapping[str, Any],
    maps: Mapping[str, Mapping[str, list[str]]],
    entries: Sequence[Mapping[str, Any]],
    box_env: str,
    env: str,
) -> list[str]:
    """One note per skipped box of ``box_env`` whose receipt runs an older fleet."""
    mapping = maps.get(box_env)
    if not is_limited(scope) or mapping is None:
        return []
    notes = []
    for name in scope.get("skipped_boxes") or []:
        hosts = [h for h, found in mapping.items() if name in found]
        if not hosts:
            continue
        drift = fleet_drift(fleet_root, box_commit(entries, hosts))
        line = drift_line(name, drift) if drift else None
        if line:
            notes.append(f"{line}; it is skipped, run `bay up {env}` to bring it up to date")
    return notes
