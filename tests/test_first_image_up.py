"""``bay up`` builds the first image of a new build container (2.27.0).

The plan gets one step ``image``/``create`` per build container with no image
yet: the plan creates it (a), or the receipt lists no running container of
that name (b, the retry after a failed first ``bay up``). ``bay up`` then adds
the tag ``first_image`` and passes ``bay_first_image``. Exit 40 stays as the
fallback for what that build cannot cover.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import jsonschema
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_plan_up import (  # noqa: E402,F401  (fixtures are used by name)
    PLAN_SCHEMA,
    FakeBox,
    _build_app,
    _capture_playbook,
    _failing_deploy,
    _git_identity,
    _REAL_DEFAULT_DEPLOY,
    _stamp,
    box,
    cli,
    commit_all,
    cx_of,
    do_up,
    lock_of,
    make,
    world,
)

from bay_cli import apply as applymod  # noqa: E402
from bay_cli import plan as planmod  # noqa: E402
from bay_cli import routes  # noqa: E402
from bay_cli.context import Context  # noqa: E402


def _firsts(plan: dict[str, Any]) -> list[dict[str, Any]]:
    return [s for s in plan["steps"] if s["kind"] == "image" and s["action"] == "create"]


def _no_image(monkeypatch: pytest.MonkeyPatch) -> None:
    """The box has no image tag at all (the exit 40 check asks it)."""
    monkeypatch.setattr(applymod, "default_image_tags", lambda *_a: {"box-1": []})


def _strategy(world: dict[str, Path], value: str) -> None:
    path = world["fleet"] / "group_vars" / "production" / "main.yml"
    path.write_text(f"---\ngit_deploy_build_strategy: {value}\n")
    commit_all(world["fleet"], f"strategy {value}")


# ── the plan ─────────────────────────────────────────────────────────────────


def test_new_build_container_gets_a_first_image_step(
    world: dict[str, Path], box: FakeBox
) -> None:
    """(a): the plan creates the container, so the plan builds its image first."""
    commit = _build_app(world)
    plan = make(world)
    jsonschema.validate(plan, PLAN_SCHEMA)
    (step,) = _firsts(plan)
    assert step["container"] == "webapp" and step["project"] == "webapp"
    assert step["risk"] == "safe" and step["source"] == "compile"
    assert step["reason"] == (
        f"first image of webapp: bay up builds {commit[:12]} on the box before it creates "
        "the container; this takes as long as a build"
    )
    assert plan["verdict"] == "auto"
    assert any(
        s["kind"] == "container" and s["action"] == "create" and s["container"] == "webapp"
        for s in plan["steps"]
    )


def test_remote_strategy_names_the_build_server(world: dict[str, Path], box: FakeBox) -> None:
    _strategy(world, "remote")
    _build_app(world)
    (step,) = _firsts(make(world))
    assert "on the build server before it creates the container" in step["reason"]


def test_jinja_strategy_names_both_places(world: dict[str, Path], box: FakeBox) -> None:
    _strategy(world, '"{{ some_var }}"')
    _build_app(world)
    (step,) = _firsts(make(world))
    assert "on the build server or on the box" in step["reason"]


def test_no_step_for_registry_strategy_or_an_image_app(
    world: dict[str, Path], box: FakeBox
) -> None:
    # The app runs an image: nothing to build.
    assert _firsts(make(world)) == []
    # A build with strategy registry: Bay does not build it.
    _strategy(world, "registry")
    _build_app(world)
    plan = make(world)
    assert _firsts(plan) == [] and plan["verdict"] == "auto"


def test_no_step_once_the_container_runs(
    world: dict[str, Path], box: FakeBox, monkeypatch: pytest.MonkeyPatch
) -> None:
    _build_app(world)
    assert do_up(world)["result"] == "ok"
    # The receipt lists the container (running, no commit yet): no step.
    assert _firsts(make(world)) == []
    # It runs a commit: still none.
    _stamp(box, "webapp", lock_of(world)["commit"])
    assert _firsts(make(world)) == []


def test_retry_after_a_failed_first_up_builds_again(
    world: dict[str, Path], box: FakeBox, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(b): the compile already holds the container, the receipt marks it failed."""
    _no_image(monkeypatch)
    commit = _build_app(world)
    monkeypatch.setattr(applymod, "default_deploy", _failing_deploy(box, {"webapp"}))
    failed = cli(world, "up", "--json")
    assert failed.exit_code == 40, failed.output

    plan = make(world)
    assert not [s for s in plan["steps"] if s["kind"] == "container"]
    (step,) = _firsts(plan)
    assert step["source"] == "box" and step["container"] == "webapp"
    assert plan["verdict"] == "auto"

    # The same plan with no receipt read: rule (a) only, so no step.
    assert _firsts(make(world, read_running=False)) == []

    # A box that could not be read: Bay cannot tell, so no step either.
    real = planmod.default_receipt_reader

    def broken(cx: Context, box_env: str, **kw: Any) -> list[dict[str, Any]]:
        return [{**e, "error": "ssh failed", "receipt": None} for e in real(cx, box_env, **kw)]

    monkeypatch.setattr(planmod, "default_receipt_reader", broken)
    assert _firsts(make(world)) == []
    monkeypatch.setattr(planmod, "default_receipt_reader", box.read)

    # The retry builds it and succeeds: lock ok, show ok, no second up.
    monkeypatch.setattr(applymod, "default_deploy", box.deploy)
    result = do_up(world)
    assert result["result"] == "ok"
    assert box.first_images[-1] == {"webapp": commit}
    assert box.tags[-1] == "deploy_stack,backup,first_image"
    assert lock_of(world)["envs"]["production"]["result"] == "ok"


def test_not_running_reads_the_receipt() -> None:
    names = {"a", "b", "c"}
    ok = {"receipt": {"containers": [
        {"name": "a"},
        {"name": "b", "failed": True},
        {"name": "c", "failed": True, "commit": "f" * 40},
    ]}, "error": None}
    assert planmod.not_running([ok], names) == {"b"}
    # No receipt read, a box error, a receipt with no container list: unknown.
    assert planmod.not_running([], names) is None
    assert planmod.not_running([ok, {"receipt": None, "error": "down"}], names) is None
    assert planmod.not_running([{"receipt": {"result": "ok"}, "error": None}], names) is None
    # A box with no receipt file yet runs nothing.
    assert planmod.not_running([{"receipt": None, "error": None}], names) == names


def test_first_image_steps_for_a_project_in_the_fleet(tmp_path: Path) -> None:
    """A project in the fleet has no app commit to pin: the text names the branch head."""
    cx = SimpleNamespace(fleet_root=tmp_path)
    proj = SimpleNamespace(name="board", in_fleet=True, lock={})
    wanted = SimpleNamespace(doc={"name": "board"}, commit="a" * 40)
    data = {"services": {
        "board": {"build": {"repo": "https://git.example/o/r", "branch": "live"}},
        "board-de": {"build": {"repo": "https://git.example/o/r", "shared_from": "board"}},
        "img": {"image": "reg.example/img:1"},
    }}
    diff = SimpleNamespace(
        wanted_data=data,
        steps=[
            {"kind": "container", "action": "create", "container": n}
            for n in ("board", "board-de", "img")
        ],
    )
    steps = planmod.first_image_steps(
        cx, proj, wanted, "production", "production", {"board", "board-de", "img"}, diff, []
    )
    assert [s["container"] for s in steps] == ["board"]
    assert "bay up builds the head of branch live on the box" in steps[0]["reason"]


# ── bay up ───────────────────────────────────────────────────────────────────


def test_up_adds_the_tag_and_the_var(world: dict[str, Path], box: FakeBox) -> None:
    commit = _build_app(world)
    result = do_up(world)
    assert result["result"] == "ok"
    assert result["deploy_tags"] == "deploy_stack,backup,first_image"
    assert box.tags[-1] == "deploy_stack,backup,first_image"
    assert box.first_images[-1] == {"webapp": commit}
    assert _firsts({"steps": result["steps"]})
    # The next bay up has no step: no tag, no var.
    again = do_up(world)
    assert again["deploy_tags"] == applymod.UP_DEPLOY_TAGS
    assert box.tags[-1] is None and box.first_images[-1] is None


def test_up_with_a_saved_plan_is_not_stale(world: dict[str, Path], box: FakeBox) -> None:
    _build_app(world)
    plan = make(world)
    planmod.save(cx_of(world), plan)
    result = do_up(world, plan_id=plan["plan_id"])
    assert result["result"] == "ok" and result["plan_id"] == plan["plan_id"]
    assert box.first_images[-1] is not None


def test_default_deploy_passes_bay_first_image(
    world: dict[str, Path], box: FakeBox, monkeypatch: pytest.MonkeyPatch
) -> None:
    commit = _build_app(world)
    calls = _capture_playbook(monkeypatch, on_deploy=box.deploy)
    monkeypatch.setattr(applymod, "default_deploy", _REAL_DEFAULT_DEPLOY)
    applymod.up(planmod.load_project(cx_of(world), "webapp", cwd=world["app"]),
                planmod.PlanOptions())
    (deploy,) = calls
    assert deploy["tags"] == "deploy_stack,backup,first_image"
    extra = deploy["extra"]
    values = [json.loads(extra[i + 1]) for i, a in enumerate(extra) if a == "-e"
              and extra[i + 1].startswith("{") and "bay_first_image" in extra[i + 1]]
    assert values == [{"bay_first_image": {"webapp": commit}}]


def test_first_image_builds_values_and_siblings() -> None:
    app = SimpleNamespace(name="app", in_fleet=False, lock={})
    fleet = SimpleNamespace(name="fl", in_fleet=True, lock={})
    sha = "b" * 40
    plan = {
        "env": "production",
        "steps": [
            {"kind": "image", "action": "create", "container": "app", "project": "app"},
            {"kind": "image", "action": "create", "container": "fl", "project": "fl"},
            {"kind": "image", "action": "update", "container": "x", "project": "app"},
            {"kind": "container", "action": "create", "container": "app-de", "project": "app"},
        ],
    }
    compiled = {"services": {
        "app": {"build": {}},
        "app-de": {"build": {"shared_from": "app"}},
        "app-fr": {"build": {"shared_from": "app"}},
        "fl": {"build": {}},
    }}
    running = [{"error": None, "receipt": {"containers": [{"name": "app-fr"}]}}]
    out = applymod.first_image_builds(
        plan, [(app, sha), (fleet, "c" * 40)], compiled, running  # type: ignore[list-item]
    )
    # app-fr runs already: a local build must not move its :latest.
    assert out == {"app": sha, "app-de": sha, "fl": None}


def test_routes_deploy_tags_adds_first_image() -> None:
    steps = [{"kind": "image", "action": "create"}]
    assert routes.deploy_tags(steps, "deploy_stack,backup") == "deploy_stack,backup,first_image"
    assert routes.deploy_tags([{"kind": "image", "action": "update"}], "a") == "a"


def test_retry_line_leaves_the_first_image_tag_out() -> None:
    from bay_cli.commands.project_cmd import _retry_deploy

    tags = "deploy_stack,backup,first_image"
    assert _retry_deploy({"deploy_tags": tags, "box_env": "production"}) is None
    line = _retry_deploy(
        {"deploy_tags": "deploy_stack,backup,headscale,traefik,first_image", "box_env": "p"}
    )
    assert line is not None and line.endswith("--tags deploy_stack,backup,headscale,traefik")


# ── the exit 40 fallback ─────────────────────────────────────────────────────


def test_exit_40_when_the_asked_build_made_no_image(
    world: dict[str, Path], box: FakeBox, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_image(monkeypatch)
    commit = _build_app(world)
    monkeypatch.setattr(applymod, "default_deploy", _failing_deploy(box, {"webapp"}))
    result = cli(world, "up", "--json")
    assert result.exit_code == 40, result.output
    doc = json.loads(result.stdout)
    assert doc["first_image"] == ["webapp"]
    note = next(n for n in doc["notes"] if n.startswith("first deploy of webapp"))
    assert note.startswith(f"first deploy of webapp: the box has no image for {commit[:12]} yet.")
    assert "bay up asked for the first image of webapp" in note
    assert "deploy key" in note


def test_exit_40_note_for_registry_and_for_no_receipt(
    world: dict[str, Path], box: FakeBox, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_image(monkeypatch)
    _strategy(world, "registry")
    _build_app(world)
    monkeypatch.setattr(applymod, "default_deploy", _failing_deploy(box, {"webapp"}))
    doc = json.loads(cli(world, "up", "--json").stdout)
    note = next(n for n in doc["notes"] if n.startswith("first deploy of webapp"))
    assert "Its build strategy is registry, so Bay does not build it" in note
    assert box.first_images[-1] is None

    # No registry, and a run that read no receipt and created nothing: not asked.
    assert applymod._first_image_note("p", "c", "abc", "local", False).endswith(
        "Run bay up again: when its plan reads the box receipt, it builds the first image "
        "of the container before it creates it."
    )
