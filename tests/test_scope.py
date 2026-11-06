"""``bay up <env> --project <p>`` runs only on the boxes it needs (2.13.0, spec 2026-11-06).

Unit tests call :func:`bay_cli.scope.compute` with a fake host map. The
integration tests use the fake box and fleet of test_plan_up (two boxes in one
box env: ``box-1`` group ``one`` on 192.0.2.11, ``box-2`` group ``two`` on
192.0.2.12). No network, no SSH. The real ``ansible-playbook`` runs are in
test_scope_ansible.py.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import jsonschema
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_plan_up import (  # noqa: E402,F401  (fixtures are used by name)
    FAKE_HOSTS,
    PLAN_SCHEMA,
    FakeBox,
    _git_identity,
    _two_boxes_status_on_one,
    _up_status,
    box,
    cli,
    commit_all,
    cx_of,
    do_up,
    edit_app,
    git,
    lock_of,
    make,
    project,
    world,
)

from bay_cli import apply as applymod  # noqa: E402
from bay_cli import lockfile, receipts  # noqa: E402
from bay_cli import plan as planmod  # noqa: E402
from bay_cli import scope as scopemod  # noqa: E402
from bay_cli.context import Context  # noqa: E402
from bay_cli.errors import BayError  # noqa: E402

# ── unit: scope.compute ─────────────────────────────────────────────────────

FOUR = {
    "boxes": {
        "eu": {"env": "production", "group": "eu"},
        "na": {"env": "production", "group": "na"},
        "eu2": {"env": "production", "group": "eu2"},
        "infra": {"env": "production", "group": "infra"},
        "lab": {"env": "lab"},
        "edge-a": {"env": "edge", "group": "edge-a"},
        "edge-b": {"env": "edge", "group": "edge-b"},
    },
    "tailnet": {"ingress_box": "infra"},
}
HOST = {"eu": "192.0.2.21", "na": "192.0.2.22", "eu2": "192.0.2.23", "infra": "192.0.2.24"}
EDGE = {"edge-a": "192.0.2.31", "edge-b": "192.0.2.32"}


def _maps(cx: Context, box_env: str) -> tuple[dict[str, list[str]] | None, str | None]:
    hosts = HOST if box_env == "production" else EDGE if box_env == "edge" else None
    if hosts is None:
        raise AssertionError(f"no map read expected for {box_env}")
    return {h: [b] for b, h in hosts.items()}, None


def _svc(group: str, *, build: dict[str, Any] | None = None, **extra: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {"image": f"registry.example.com/{group}:1", "regions": [group]}
    if build is not None:
        entry["build"] = build
    entry.update(extra)
    return entry


BASE = {
    "webhook": {"domain": "deploy.example.com", "secret": "x"},
    "services": {
        "app-eu2": _svc("eu2", build={"repo": "git@example.com:a/app.git", "branch": "main"}),
        "shop-eu": _svc("eu", build={"repo": "git@example.com:a/shop.git", "branch": "main"}),
        "news-na": _svc("na", build={"repo": "git@example.com:a/news.git", "branch": "main"}),
        "static-eu": _svc("eu"),
    },
}


def _compute(
    *,
    box: str = "eu2",
    box_env: str = "production",
    moves: list[tuple[str, str | None]] | None = None,
    steps: list[dict[str, Any]] | None = None,
    current: dict[str, Any] | None = None,
    wanted: dict[str, Any] | None = None,
    all_boxes: bool = False,
    read_map: Any = _maps,
    group_vars: dict[str, Any] | None = None,
) -> scopemod.Result:
    cx = Context.for_fleet_root(Path("/nonexistent-fleet"))
    gv = group_vars or {}
    return scopemod.compute(
        cx,
        FOUR,
        project="app",
        box=box,
        box_env=box_env,
        moves=moves or [],
        steps=steps or [],
        current=current if current is not None else BASE,
        wanted=wanted if wanted is not None else json.loads(json.dumps(BASE)),
        all_boxes=all_boxes,
        read_map=read_map,
        group_var=lambda _env, key: gv.get(key),
    )


def _changed(service: str, **change: Any) -> dict[str, Any]:
    wanted = json.loads(json.dumps(BASE))
    wanted["services"][service]["build"].update(change)
    return wanted


def test_scope_is_the_project_box() -> None:
    res = _compute()
    assert res.scope == {
        "all_boxes": False,
        "boxes": ["eu2"],
        "hosts": [HOST["eu2"]],
        "skipped_boxes": ["eu", "infra", "na"],
        "reasons": {"eu2": "project box"},
        "limits": {"production": [HOST["eu2"]]},
        "why": None,
    }
    jsonschema.validate(res.scope, PLAN_SCHEMA["properties"]["scope"])


def test_scope_takes_the_old_box_of_a_move() -> None:
    res = _compute(moves=[("na", "production")])
    assert res.scope["boxes"] == ["eu2", "na"]
    assert res.scope["reasons"] == {"eu2": "project box", "na": "old box of the move"}
    assert res.scope["limits"] == {"production": sorted([HOST["eu2"], HOST["na"]])}


def test_a_move_across_box_envs_limits_the_old_env_to_the_old_box() -> None:
    res = _compute(moves=[("edge-a", "edge")])
    assert res.scope["limits"] == {"edge": [EDGE["edge-a"]], "production": [HOST["eu2"]]}
    assert res.scope["skipped_boxes"] == ["edge-b", "eu", "infra", "na"]
    assert scopemod.limit_for(res.scope, "edge") == [EDGE["edge-a"]]


def test_a_move_from_a_one_box_env_leaves_that_env_unlimited() -> None:
    res = _compute(moves=[("lab", "lab")])
    assert res.scope["limits"] == {"production": [HOST["eu2"]]}
    assert scopemod.limit_for(res.scope, "lab") is None
    assert "lab" in res.scope["boxes"]


def test_a_route_step_takes_the_ingress_box() -> None:
    res = _compute(steps=[{"kind": "route", "action": "create", "resource": "notes"}])
    assert res.scope["reasons"] == {"eu2": "project box", "infra": "ingress box"}


def test_a_changed_build_takes_every_receiver_box_and_the_build_server() -> None:
    res = _compute(
        wanted=_changed("app-eu2", strategy="remote"),
        group_vars={"build_server": HOST["infra"]},
    )
    # webhook-config.json lists every build on every box with a receiver (eu, na,
    # eu2 have a build), and the build server renders it for the remote builds.
    assert res.scope["reasons"] == {
        "eu": "webhook box",
        "eu2": "project box",
        "infra": "build server",
        "na": "webhook box",
    }
    assert res.scope["all_boxes"] is True
    assert res.scope["why"] == scopemod.WHY_EVERY_HOST
    assert scopemod.limit_for(res.scope, "production") is None


def test_a_changed_local_build_takes_the_receiver_boxes_only() -> None:
    res = _compute(wanted=_changed("app-eu2", paths={"include": ["src/"]}),
                   group_vars={"build_server": HOST["infra"]})
    assert res.scope["boxes"] == ["eu", "eu2", "na"]
    assert res.scope["skipped_boxes"] == ["infra"]


def test_an_unchanged_build_surface_adds_no_box() -> None:
    # A plain pin move compiles the same entries: same scope as the project box alone.
    res = _compute(group_vars={"build_server": HOST["infra"]})
    assert res.scope["boxes"] == ["eu2"]


def test_a_changed_build_without_a_webhook_adds_no_box() -> None:
    current = {k: v for k, v in BASE.items() if k != "webhook"}
    wanted = _changed("app-eu2", strategy="remote")
    del wanted["webhook"]
    res = _compute(current=current, wanted=wanted, group_vars={"build_server": HOST["infra"]})
    assert res.scope["boxes"] == ["eu2"]


def test_a_jinja_build_server_takes_the_whole_env() -> None:
    res = _compute(
        wanted=_changed("app-eu2", strategy="remote"),
        group_vars={"build_server": "{{ groups['infra'][0] }}"},
    )
    assert res.scope["all_boxes"] is True and res.scope["why"] == scopemod.WHY_BUILD_SERVER
    assert any("build server" in n for n in res.notes)


def test_changed_builds_compares_the_whole_entry() -> None:
    wanted = json.loads(json.dumps(BASE))
    wanted["services"]["static-eu"]["image"] = "registry.example.com/eu:2"  # no build
    assert scopemod.changed_builds(BASE, wanted) == []
    wanted["services"]["app-eu2"]["regions"] = ["eu2", "na"]
    assert scopemod.changed_builds(BASE, wanted) == ["app-eu2"]
    del wanted["services"]["shop-eu"]
    assert scopemod.changed_builds(BASE, wanted) == ["app-eu2", "shop-eu"]


def test_all_boxes_flag_gives_no_scope() -> None:
    res = _compute(all_boxes=True, read_map=None)
    assert res.scope["all_boxes"] is True and res.scope["why"] == scopemod.WHY_FLAG
    assert res.scope["hosts"] == [] and res.scope["limits"] == {}
    assert res.scope["boxes"] == ["eu", "eu2", "infra", "na"]


def test_a_one_box_env_reads_no_map_and_has_no_limit() -> None:
    def never(cx: Context, box_env: str) -> Any:
        raise AssertionError("a one-box env reads no inventory")

    res = _compute(box="lab", box_env="lab", read_map=never)
    assert res.scope["all_boxes"] is True and res.scope["why"] == scopemod.WHY_EVERY_HOST
    assert res.scope["reasons"] == {"lab": "project box"}
    assert scopemod.limit_for(res.scope, "lab") is None


def test_no_host_map_falls_back_to_the_whole_env_with_a_note() -> None:
    res = _compute(read_map=lambda cx, env: (None, "the inventory has no host in production"))
    assert res.scope["all_boxes"] is True and res.scope["why"] == scopemod.WHY_NO_MAP
    assert res.notes == [
        "per-box scope is not available for production (the inventory has no host in "
        "production); this run deploys every box of production"
    ]


def test_a_tailnet_allowlist_step_gives_no_scope() -> None:
    res = _compute(steps=[{"kind": "tailnet", "action": "update", "resource": "allowlist"}])
    assert res.scope["all_boxes"] is True and res.scope["why"] == scopemod.WHY_TAILNET


def test_a_step_of_another_project_takes_its_box() -> None:
    step = {"kind": "container", "container": "static-eu", "project": "other", "source": "compile"}
    res = _compute(steps=[step])
    assert res.scope["reasons"] == {"eu": "planned step", "eu2": "project box"}
    # A step of the project itself is on its own box: nothing more.
    own = {"kind": "container", "container": "app-eu2", "project": "app", "source": "compile"}
    assert _compute(steps=[own]).scope["boxes"] == ["eu2"]
    # A step the box check predicted ran on the scope already.
    box_step = {**step, "source": "box"}
    assert _compute(steps=[box_step]).scope["boxes"] == ["eu2"]


def test_a_step_bay_cannot_place_gives_no_scope() -> None:
    step = {"kind": "container", "container": "gone", "project": None, "source": "compile"}
    res = _compute(steps=[step])
    assert res.scope["all_boxes"] is True and res.scope["why"] == scopemod.WHY_UNPLACED


def test_a_box_on_a_shared_host_joins_the_scope() -> None:
    def shared(cx: Context, box_env: str) -> Any:
        return {
            HOST["eu2"]: ["eu2", "na"],
            HOST["eu"]: ["eu"],
            HOST["infra"]: ["infra"],
        }, None

    res = _compute(read_map=shared)
    assert res.scope["reasons"] == {"eu2": "project box", "na": "shares a host"}
    assert res.scope["hosts"] == [HOST["eu2"]]


def test_limit_args() -> None:
    assert scopemod.limit_args(["192.0.2.1", "192.0.2.2"]) == [
        "-l", "192.0.2.1,192.0.2.2", "-e", "bay_scope_limited=true",
    ]


# ── unit: the argv ──────────────────────────────────────────────────────────


def _capture_deploy(
    monkeypatch: pytest.MonkeyPatch, world: dict[str, Path], limit: dict[str, Any] | None
) -> tuple[list[str], dict[str, Any]]:
    from bay_cli.commands import ops, validate

    seen: dict[str, Any] = {}

    class Ok:
        total_issues = 0

    monkeypatch.setattr(validate, "run_validation", lambda *a, **k: Ok())
    monkeypatch.setattr(
        ops, "_run_playbook", lambda cx, pb, env, tags, extra: seen.update(argv=list(extra))
    )
    monkeypatch.setattr(ops, "_invalidate_rig_cache", lambda *a: None)
    monkeypatch.setattr(
        ops, "_run_post_deploy_healthcheck", lambda *a, **k: seen.update(health=k)
    )
    kw = {"limit": limit} if limit is not None else {}
    applymod._REAL_DEFAULT_DEPLOY(cx_of(world), "production", **kw)
    return seen["argv"], seen["health"]


def _no_report_dir(argv: list[str]) -> list[str]:
    """The argv without the values of the temp report dirs (they differ per run)."""
    return [a for a in argv if "report_dir" not in a]


def test_the_playbook_argv_has_a_limit_exactly_when_scoped(
    world: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    whole, health = _capture_deploy(monkeypatch, world, None)
    assert "-l" not in whole and not any("bay_scope_limited" in a for a in whole)
    assert "groups" not in health
    # The argv of a run with no scope is the argv of 2.12: the same list as before.
    from bay_cli.receipts import deploy_extra_vars

    expected = ["-e", "_rig_mode=true", "-e", "_rig_write=false", *deploy_extra_vars(cx_of(world))]
    assert _no_report_dir(whole)[: len(expected)] == expected
    assert len(_no_report_dir(whole)) == len(expected) + 1  # the -e before the report dir

    limited, health = _capture_deploy(
        monkeypatch, world, {"hosts": ["192.0.2.12"], "boxes": ["box-1"]}
    )
    assert _no_report_dir(limited) == _no_report_dir(whole) + scopemod.limit_args(["192.0.2.12"])
    assert health["groups"] == set()  # box-1 has no group in the one-box fleet


def test_the_box_check_and_the_receipt_read_take_the_hosts(
    world: dict[str, Path], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from bay_cli.commands import ops

    calls: list[list[str]] = []
    monkeypatch.setattr(ops, "_run_playbook", lambda cx, pb, env, tags, extra: calls.append(extra))
    services = tmp_path / "services.yml"
    services.write_text("---\n")
    planmod.default_box_check(cx_of(world), "production", services)
    planmod.default_box_check(cx_of(world), "production", services, hosts=["192.0.2.12"])
    plain, limited = (_no_report_dir(c) for c in calls)
    assert "-l" not in plain and plain[-1] == "--check"
    # The same arguments, then the limit, then --check.
    assert limited == [*plain[:-1], *scopemod.limit_args(["192.0.2.12"]), "--check"]

    argvs: list[list[str]] = []

    class Proc:
        stdout = ""
        stderr = "nothing"
        returncode = 1

    def run(argv: list[str], env: dict[str, str], cwd: Path) -> Proc:
        argvs.append(argv)
        return Proc()

    receipts.fetch_receipts(cx_of(world), "production", run=run)
    receipts.fetch_receipts(cx_of(world), "production", run=run, hosts=["192.0.2.12"])
    assert "-l" not in argvs[0]
    assert argvs[1] == [*argvs[0], "-l", "192.0.2.12"]


def test_the_health_check_probes_only_the_scope_services(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import yaml

    from bay_cli import healthcheck
    from bay_cli.commands import ops

    root = tmp_path / "fleet"
    (root / "group_vars" / "all").mkdir(parents=True)
    (root / "group_vars" / "all" / "services.yml").write_text(
        yaml.safe_dump(
            {
                "services": {
                    "a": {"domains": ["a.example.com"], "regions": ["one"]},
                    "b": {"domains": ["b.example.com"], "regions": ["two"]},
                    "c": {"domains": ["c.example.com"]},
                }
            }
        )
    )
    probed: list[set[str]] = []
    monkeypatch.setattr(
        healthcheck, "run_healthcheck", lambda services, **k: probed.append(set(services)) or []
    )
    ops._run_post_deploy_healthcheck("production", root, tmp_path)
    ops._run_post_deploy_healthcheck("production", root, tmp_path, groups={"two"})
    assert probed == [{"a", "b", "c"}, {"b", "c"}]


# ── integration: plan and up on two boxes ───────────────────────────────────


def _status_plan(world: dict[str, Path], **kw: Any) -> dict[str, Any]:
    proj = planmod.load_project(cx_of(world), "status", cwd=world["fleet"])
    return planmod.make_plan(proj, planmod.PlanOptions(**kw))


def test_scoped_plan_and_up_deploy_only_the_project_box(
    world: dict[str, Path], box: FakeBox
) -> None:
    _two_boxes_status_on_one(world, "box-2")
    do_up(world, all_boxes=True)  # the webapp runs on box-2, its pin is confirmed
    webapp_before = lockfile.lock_path(world["fleet"], "webapp").read_bytes()

    plan = _status_plan(world)
    jsonschema.validate(plan, PLAN_SCHEMA)
    assert plan["scope"] == {
        "all_boxes": False,
        "boxes": ["box-1"],
        "hosts": [FAKE_HOSTS["box-1"]],
        "skipped_boxes": ["box-2"],
        "reasons": {"box-1": "project box"},
        "limits": {"production": [FAKE_HOSTS["box-1"]]},
        "why": None,
    }
    assert planmod.UNPLANNED_NOTE.format(box_env="production") not in plan["notes"]
    note = scopemod.scope_note(plan["scope"], "production")
    assert note in plan["notes"] and "skips box-2" in note
    assert f"note: {note}" in planmod.render(plan)

    box.reads.clear()
    result = _up_status(world)
    assert box.limits[-1] == {"hosts": [FAKE_HOSTS["box-1"]], "boxes": ["box-1"]}
    # The receipt read after the deploy asks only the scope hosts.
    assert box.reads[-1] == [FAKE_HOSTS["box-1"]]
    assert result["scope"] == plan["scope"]
    assert result["notes"][0] == note
    # A project on a skipped box keeps its lock record: not pinned, not failed, not counted.
    assert [r["project"] for r in result["pinned"]] == ["status"]
    assert lockfile.lock_path(world["fleet"], "webapp").read_bytes() == webapp_before
    subject = git(world["fleet"], "log", "-1", "--format=%s", result["receipt_commit"])
    assert subject == "bay: receipt production (1 projects)"


def test_a_skipped_box_that_is_down_does_not_fail_the_run(
    world: dict[str, Path], box: FakeBox, monkeypatch: pytest.MonkeyPatch
) -> None:
    _two_boxes_status_on_one(world, "box-2")
    do_up(world, all_boxes=True)
    real = box.read

    def down(cx: Context, box_env: str, *, hosts: list[str] | None = None) -> Any:
        out = real(cx, box_env, hosts=hosts)
        if hosts is None:  # only a read of every host meets box-2, which is down
            out = [
                {**e, "receipt": None, "error": "unreachable"}
                if e["box"] == FAKE_HOSTS["box-2"] else e
                for e in out
            ]
        return out

    monkeypatch.setattr(planmod, "default_receipt_reader", down)
    result = _up_status(world)
    assert result["result"] == "ok" and result["receipt_unread"] == []
    assert not any("unreachable" in n or FAKE_HOSTS["box-2"] in n for n in result["notes"])


def test_the_plan_id_covers_the_scope(world: dict[str, Path], box: FakeBox) -> None:
    _two_boxes_status_on_one(world, "box-2")
    scoped, everything = _status_plan(world), _status_plan(world, all_boxes=True)
    assert scoped["scope"]["all_boxes"] is False and everything["scope"]["all_boxes"] is True
    assert scoped["plan_id"] != everything["plan_id"]
    # The same inputs give the same id.
    assert _status_plan(world)["plan_id"] == scoped["plan_id"]


def test_all_boxes_with_a_scoped_plan_id_is_refused(world: dict[str, Path], box: FakeBox) -> None:
    _two_boxes_status_on_one(world, "box-2")
    plan = _status_plan(world)
    planmod.save(cx_of(world), plan)
    with pytest.raises(BayError) as err:
        _up_status_id(world, plan["plan_id"], all_boxes=True)
    assert "runs on box box-1 only" in str(err.value)
    assert "--all-boxes" in str(err.value.hint)
    assert box.deploys == []


def _up_status_id(world: dict[str, Path], plan_id: str, **kw: Any) -> dict[str, Any]:
    proj = planmod.load_project(cx_of(world), "status", cwd=world["fleet"])
    return applymod.up(proj, planmod.PlanOptions(**kw), plan_id=plan_id)


def test_an_all_boxes_plan_id_deploys_every_box(world: dict[str, Path], box: FakeBox) -> None:
    _two_boxes_status_on_one(world, "box-2")
    plan = _status_plan(world, all_boxes=True)
    planmod.save(cx_of(world), plan)
    # The recheck plans with the saved plan's --all-boxes, so the plan is not stale.
    result = _up_status_id(world, plan["plan_id"])
    assert result["plan_id"] == plan["plan_id"]
    assert box.limits == [None]


def test_a_recheck_with_another_scope_is_stale(world: dict[str, Path], box: FakeBox) -> None:
    _two_boxes_status_on_one(world, "box-2")
    plan = _status_plan(world)
    saved = json.loads(json.dumps(plan))
    saved["scope"] = scopemod.whole(scopemod.WHY_EVERY_HOST, ["box-1", "box-2"])
    proj = planmod.load_project(cx_of(world), "status", cwd=world["fleet"])
    fresh = planmod.recheck(proj, saved, planmod.PlanOptions())
    assert fresh["verdict"] == "stale"
    assert fresh["stale"][0] == (
        "the scope changed: the plan runs on every box, a new plan runs on box box-1"
    )


def test_a_one_box_fleet_runs_as_before(world: dict[str, Path], box: FakeBox) -> None:
    plan = make(world)
    assert plan["scope"]["all_boxes"] is True and plan["scope"]["limits"] == {}
    assert planmod.UNPLANNED_NOTE.format(box_env="production") in plan["notes"]
    result = do_up(world)
    assert box.limits == [None] and box.reads[-1] is None
    assert result["scope"]["all_boxes"] is True


def test_rollback_keeps_the_scope(world: dict[str, Path], box: FakeBox) -> None:
    status = _two_boxes_status_on_one(world, "box-2")
    do_up(world, all_boxes=True)
    _up_status(world)
    status.write_text(status.read_text().replace('MODE = "one"', 'MODE = "two"'))
    commit_all(world["fleet"], "status two")
    _up_status(world)
    proj = planmod.load_project(cx_of(world), "status", cwd=world["fleet"])
    result = applymod.rollback(proj, planmod.PlanOptions())
    assert result["action"] == "rollback" and result["scope"]["boxes"] == ["box-1"]
    assert box.limits[-1] == {"hosts": [FAKE_HOSTS["box-1"]], "boxes": ["box-1"]}
    result = applymod.rollback(proj, planmod.PlanOptions(all_boxes=True))
    assert result["scope"]["all_boxes"] is True and box.limits[-1] is None


def test_up_json_and_the_cli_flag(world: dict[str, Path], box: FakeBox) -> None:
    _two_boxes_status_on_one(world, "box-2")
    result = cli(world, "plan", "production", "--project", "status", "--json", "--all-boxes")
    assert json.loads(result.stdout)["scope"]["why"] == "--all-boxes"
    result = cli(world, "up", "production", "--project", "status", "--json")
    doc = json.loads(result.stdout)
    assert result.exit_code == 0, result.stdout
    assert doc["scope"]["boxes"] == ["box-1"]


# ── fleet drift ─────────────────────────────────────────────────────────────


def test_fleet_drift_leaves_out_locks_plans_and_services(world: dict[str, Path]) -> None:
    fleet = world["fleet"]
    old = git(fleet, "rev-parse", "HEAD")
    assert scopemod.fleet_drift(fleet, old) == {"fleet_commit": old, "known": True, "paths": []}
    (fleet / "group_vars" / "all" / "main.yml").write_text("---\nstack_name: testfleet\nx: 1\n")
    (fleet / "plans").mkdir(exist_ok=True)
    (fleet / "plans" / "abc.json").write_text("{}\n")
    lock = lockfile.lock_path(fleet, "webapp")
    lock.write_text(lock.read_text() + "\n")
    services = fleet / "group_vars" / "all" / "services.yml"
    services.write_text(services.read_text() + "\n")
    commit_all(fleet, "drift")
    drift = scopemod.fleet_drift(fleet, old)
    assert drift == {"fleet_commit": old, "known": True, "paths": ["group_vars/all/main.yml"]}
    assert scopemod.drift_line("box-2", drift) == (
        f"box box-2 runs fleet commit {old[:12]}; 1 fleet file(s) changed since then "
        "(group_vars/all/main.yml)"
    )
    unknown = scopemod.fleet_drift(fleet, "0" * 40)
    assert unknown == {"fleet_commit": "0" * 40, "known": False, "paths": []}
    assert "unknown fleet commit" in str(scopemod.drift_line("box-2", unknown))
    assert scopemod.fleet_drift(fleet, None) is None


def test_plan_notes_the_drift_of_a_skipped_box(world: dict[str, Path], box: FakeBox) -> None:
    _two_boxes_status_on_one(world, "box-2")
    do_up(world, all_boxes=True)  # both boxes run this fleet commit
    for i in range(4):
        (world["fleet"] / "files").mkdir(exist_ok=True)
        (world["fleet"] / "files" / f"f{i}.conf").write_text(f"{i}\n")
    commit_all(world["fleet"], "four fleet files")
    _up_status(world)  # box-1 moves on, box-2 is skipped
    plan = _status_plan(world)
    notes = [n for n in plan["notes"] if n.startswith("box box-2 runs fleet commit")]
    assert len(notes) == 1
    assert (
        "4 fleet file(s) changed since then (files/f0.conf, files/f1.conf, files/f2.conf, ...)"
    ) in notes[0]
    assert notes[0].endswith("run `bay up production` to bring it up to date")


def test_show_prints_the_drift_of_the_project_box(world: dict[str, Path], box: FakeBox) -> None:
    _two_boxes_status_on_one(world, "box-2")
    do_up(world, all_boxes=True)
    (world["fleet"] / "group_vars" / "all" / "main.yml").write_text(
        "---\nstack_name: testfleet\nx: 1\n"
    )
    commit_all(world["fleet"], "fleet edit")
    _up_status(world)  # box-1 only; the webapp's box-2 keeps the old fleet commit
    doc = applymod.show(project(world))
    row = doc["envs"][0]
    assert row["status"] == "ok"  # the status word does not change
    assert row["fleet_drift"]["box"] == "box-2" and row["fleet_drift"]["count"] == 1
    text = applymod.render_show(doc)
    assert "box box-2 runs fleet commit" in text and "run bay up production" in text
    # The box of a project that the run deployed has no drift line.
    status = applymod.show(planmod.load_project(cx_of(world), "status", cwd=world["fleet"]))
    assert status["envs"][0]["fleet_drift"] is None
