"""2.17.0: ``bay toml move <name> --to fleet|repo``.

``--to repo`` is ``bay adopt`` (tests/test_plan_up.py covers it, and the alias
here). ``--to fleet`` moves a project whose bay.toml lives in its app repo into
the fleet: the pinned bytes, the files its mounts read, the lock in fleet form,
and no step in the next plan. Throwaway repos and the fake box of
test_plan_up. No network, no SSH, no Ansible.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_plan_up import (  # noqa: E402,F401  (fixtures are used by name)
    STATUS_TOML,
    FakeBox,
    _git_identity,
    box,
    cli,
    commit_all,
    cx_of,
    git,
    world,
)

from bay_cli import apply as applymod  # noqa: E402
from bay_cli import compiler, lockfile  # noqa: E402
from bay_cli import plan as planmod  # noqa: E402
from bay_cli.fleet import GENERATED_SERVICES  # noqa: E402

SHOP_TOML = """\
name = "shop"
fleet = "testfleet"
port = 8080
health = "none"
needs = ["postgres"]

[build]
dockerfile = "Dockerfile"

[env]
MODE = "one"

[access]
mode = "public"

[[mounts]]
path = "/data"
volume = "data"
backup = false

[[mounts]]
path = "/etc/shop/site.conf"
from = "site.conf"

[[mounts]]
path = "/etc/shop/x.yaml"
from = "deploy/x.yaml"

[[mounts]]
path = "/etc/shop/legacy.conf"
from = "legacy.conf"

[[mounts]]
path = "/etc/shop/rules.yaml"
from = "fleet:shared/rules.yaml"

[deploy.production]
domain = "shop.example.com"
"""

#: The adopted names that must survive every move.
ADOPTED = {
    "database": "shop_prod",
    "role": "shop_app",
    "volumes": {"data": "shop_data"},
    "containers": {"web": "old-shop"},
}


def _body(text: str) -> str:
    return compiler.split_header(text)[1]


def _said(result: Any) -> str:
    try:
        err = result.stderr
    except ValueError:
        err = ""
    return f"{result.output}\n{err}\n{result.exception or ''}"


def _deploy(box: FakeBox, seen: list[dict[str, Any]] | None = None) -> Any:
    def deploy(cx: Any, box_env: str, **kw: Any) -> None:
        if seen is not None:
            seen.append(kw)
        box.deploy(
            cx, box_env, config_files_root=kw.get("config_files_root"), limit=kw.get("limit")
        )

    return deploy


def _lock(world: dict[str, Path]) -> dict[str, Any]:
    raw = lockfile.read(lockfile.lock_path(world["fleet"], "shop"))
    assert raw is not None
    return raw


def _up(world: dict[str, Path], box: FakeBox, cwd: Path, **kw: Any) -> dict[str, Any]:
    proj = planmod.load_project(cx_of(world), "shop", cwd=cwd)
    seen: list[dict[str, Any]] = kw.pop("seen", [])
    result = applymod.up(proj, planmod.PlanOptions(), deploy=_deploy(box, seen), **kw)
    assert result["result"] == "ok", result
    return result


@pytest.fixture
def shop(world: dict[str, Path], tmp_path: Path, box: FakeBox) -> dict[str, Path]:
    """``shop``: an app-repo project that builds from source, deployed by bay up.

    Its bay.toml mounts two files beside it, one file the compile reads from
    the fleet's old place ``files/shop/``, and one ``fleet:`` file. The lock
    keeps an adopted database, role, volume and container name.
    """
    fleet = world["fleet"]
    secrets = fleet / "group_vars" / "production" / "secrets.yml"
    doc = yaml.safe_load(secrets.read_text())
    doc["secrets"]["SHOP_APP_POSTGRES_PASSWORD"] = "pw"
    secrets.write_text(yaml.safe_dump(doc))
    (fleet / "files" / "shop").mkdir(parents=True)
    (fleet / "files" / "shop" / "legacy.conf").write_text("legacy: 1\n")
    (fleet / "files" / "shared").mkdir(parents=True)
    (fleet / "files" / "shared" / "rules.yaml").write_text("rules: 1\n")

    remote = tmp_path / "remotes" / "shop.git"
    git(tmp_path, "init", "-q", "--bare", str(remote))
    app = tmp_path / "shop"
    git(tmp_path, "clone", "-q", str(remote), str(app))
    (app / "bay.toml").write_text(SHOP_TOML)
    (app / "Dockerfile").write_text("FROM scratch\n")
    (app / "site.conf").write_text("site: 1\n")
    (app / "deploy").mkdir()
    (app / "deploy" / "x.yaml").write_text("x: 1\n")
    commit_all(app, "app")
    git(app, "push", "-q", "-u", "origin", "main")

    lock = lockfile.new_lock("shop", repo=str(remote))
    lock["envs"] = {"production": {"adopted": dict(ADOPTED)}}
    lockfile.write(lockfile.lock_path(fleet, "shop"), lock)
    commit_all(fleet, "bay: init shop")
    _up(world, box, app)
    return {"app": app, "remote": remote, "folder": fleet / "projects" / "shop"}


def _steps_but_webapp(plan: dict[str, Any]) -> list[dict[str, Any]]:
    """The steps of an env plan; the world's webapp was never deployed, so it creates."""
    return [s for s in plan["steps"] if s.get("container") != "webapp"]


def _without_build(entry: dict[str, Any]) -> dict[str, Any]:
    """An entry as the container spec sees it: build_specs.yml keeps only ``build: true``."""
    return {**entry, "build": "build" in entry}


def _receipt_actions(box: FakeBox) -> dict[str, str]:
    return {c["name"]: c["action"] for c in box.receipts["production"]["containers"]}


def _move(world: dict[str, Path], *args: str, cwd: Path | None = None) -> Any:
    return cli(world, "toml", "move", "shop", "--to", "fleet", *args, cwd=cwd)


def _elsewhere(tmp_path: Path) -> Path:
    path = tmp_path / "elsewhere"
    path.mkdir(exist_ok=True)
    return path


def _strip_hold(data: dict[str, Any]) -> dict[str, Any]:
    for table in ("services", "accessories"):
        for entry in (data.get(table) or {}).values():
            build = entry.get("build") if isinstance(entry, dict) else None
            if isinstance(build, dict):
                for key in planmod._HOLD_KEYS:
                    build.pop(key, None)
    return data


# ── the happy path ──────────────────────────────────────────────────────────


def test_move_copies_the_pinned_bytes_and_the_mounted_files(
    world: dict[str, Path], shop: dict[str, Path], tmp_path: Path
) -> None:
    pinned = _lock(world)["commit"]
    # A code push after bay up: WANTED moves, the config does not. The move
    # copies the file as PINNED and does not refuse.
    (shop["app"] / "README").write_text("later\n")
    commit_all(shop["app"], "code only")
    git(shop["app"], "push", "-q", "origin", "main")

    result = _move(world, cwd=_elsewhere(tmp_path))
    assert result.exit_code == 0, _said(result)
    folder = shop["folder"]
    assert (folder / "bay.toml").read_bytes() == git_show(shop["app"], pinned, "bay.toml")
    assert (folder / "site.conf").read_text() == "site: 1\n"
    assert (folder / "deploy" / "x.yaml").read_text() == "x: 1\n"
    # The file the compile reads from the fleet stays there; a fleet: file too.
    assert not (folder / "legacy.conf").exists()
    assert (world["fleet"] / "files" / "shop" / "legacy.conf").is_file()
    assert (world["fleet"] / "files" / "shared" / "rules.yaml").is_file()
    assert git(world["fleet"], "status", "--porcelain") == ""


def git_show(repo: Path, commit: str, rel: str) -> bytes:
    import subprocess

    return subprocess.run(
        ["git", "-C", str(repo), "show", f"{commit}:{rel}"], capture_output=True, check=True
    ).stdout


def test_move_rewrites_the_lock_to_fleet_form(
    world: dict[str, Path], shop: dict[str, Path], tmp_path: Path
) -> None:
    before = _lock(world)
    app_pin = before["commit"]
    assert before["envs"]["production"]["commit"] == app_pin
    assert _move(world, cwd=_elsewhere(tmp_path)).exit_code == 0
    raw = _lock(world)
    move_commit = git(world["fleet"], "rev-parse", "HEAD~1")
    assert raw["repo"] == str(shop["remote"]), "the repo stays: the box builds from it"
    assert "toml_path" not in raw
    assert raw["commit"] == move_commit
    record = raw["envs"]["production"]
    assert record["commit"] == move_commit
    assert "previous" not in record
    adopted = dict(record["adopted"])
    assert adopted.pop("from_app_commit") == app_pin
    # Nothing that names data or containers changes.
    assert adopted == ADOPTED
    for key in ("box", "deployed_at", "result", "plan_id", "last_receipt_sha256"):
        assert record[key] == before["envs"]["production"][key]
    assert not lockfile.problems(raw)


def test_move_makes_two_fleet_commits_and_pushes_nothing(
    world: dict[str, Path], shop: dict[str, Path], tmp_path: Path
) -> None:
    fleet = world["fleet"]
    remote = tmp_path / "fleet-remote.git"
    git(tmp_path, "init", "-q", "--bare", str(remote))
    git(fleet, "remote", "add", "origin", str(remote))
    git(fleet, "push", "-q", "-u", "origin", "main")
    app_head = git(shop["app"], "rev-parse", "HEAD")

    result = _move(world, "--json", cwd=_elsewhere(tmp_path))
    assert result.exit_code == 0, _said(result)
    doc = json.loads(result.stdout)
    assert git(fleet, "log", "-2", "--format=%s").splitlines() == [
        "bay: pin shop after the move",
        "bay: move shop bay.toml to the fleet",
    ]
    assert doc["fleet_commit"] == git(fleet, "rev-parse", "HEAD~1")
    assert doc["lock_commit"] == git(fleet, "rev-parse", "HEAD")
    assert sorted(git(fleet, "show", "--name-only", "--format=", "HEAD~1").splitlines()) == [
        "projects/shop/bay.toml",
        "projects/shop/deploy/x.yaml",
        "projects/shop/site.conf",
    ]
    assert git(fleet, "show", "--name-only", "--format=", "HEAD").splitlines() == [
        "projects/shop/bay.lock"
    ]
    # Local only: the fleet is two commits ahead, the app repo is untouched.
    assert git(fleet, "rev-list", "--count", "origin/main..HEAD") == "2"
    assert git(shop["app"], "rev-parse", "HEAD") == app_head
    assert git(shop["app"], "status", "--porcelain") == ""
    assert doc["delete"] == ["bay.toml", "site.conf", "deploy/x.yaml"]
    assert doc["kept"] == ["files/shop/legacy.conf"]
    assert doc["next"] == [
        "bay plan production --project shop",
        "bay up production --project shop",
        "git rm -r bay.toml site.conf deploy/x.yaml",
        "git commit",
        "git push",
    ]
    # bay up accepts a fleet that is only ahead, and its claim pushes it.
    plan = planmod.make_plan(
        planmod.load_project(cx_of(world), "shop"), planmod.PlanOptions(read_running=False)
    )
    assert plan["blockers"] == [] and plan["steps"] == [], plan


def test_move_keeps_the_compile_and_plans_zero_steps(
    world: dict[str, Path], shop: dict[str, Path], box: FakeBox, tmp_path: Path
) -> None:
    """THE CORE: same compile apart from the hold-guard keys, zero steps, no recreate."""
    from bay_reconcile import tomlhash

    fleet = world["fleet"]
    before_text = _body((fleet / GENERATED_SERVICES).read_text())
    before = yaml.safe_load(before_text)
    build = before["services"]["old-shop"]["build"]
    # The project builds from source, under its adopted names.
    assert build["repo"] == str(shop["remote"])
    assert build["bay_toml_path"] == "bay.toml"
    assert build["bay_toml_hash"] == tomlhash.canonical_hash(SHOP_TOML.encode())
    entry = before["services"]["old-shop"]
    assert entry["database"]["name"] == "shop_prod" and entry["database"]["user"] == "shop_app"
    assert "shop_data:/data" in " ".join(entry["volumes"])

    assert _move(world, cwd=_elsewhere(tmp_path)).exit_code == 0
    cx = cx_of(world)
    with planmod.compiled_fleet(cx, cwd=_elsewhere(tmp_path)) as comp:
        assert comp.result is not None, comp.errors
        after_text = _body(comp.result.text())
        files = {
            rel: (comp.files_root / rel).read_text()
            for rel in ("shop/site.conf", "shop/deploy/x.yaml", "shop/legacy.conf")
        }
    after = yaml.safe_load(after_text)
    # The only difference: the four hold-guard keys of the build go (a box
    # file, rebuild.sh, reads them). The container hash leaves build out.
    old_build = before["services"]["old-shop"]["build"]
    gone = sorted(set(old_build) - set(after["services"]["old-shop"]["build"]))
    assert gone == ["bay_build_hash", "bay_toml_files", "bay_toml_hash", "bay_toml_path"]
    assert _strip_hold(after) == _strip_hold(before)
    assert after_text != before_text
    # Byte-identical once those keys are gone from the text too.
    assert _drop_hold_lines(after_text) == _drop_hold_lines(before_text)
    assert files == {
        "shop/site.conf": "site: 1\n",
        "shop/deploy/x.yaml": "x: 1\n",
        "shop/legacy.conf": "legacy: 1\n",
    }

    proj = planmod.load_project(cx, "shop", cwd=_elsewhere(tmp_path))
    assert proj.in_fleet is True
    plan = planmod.make_plan(proj, planmod.PlanOptions())
    assert plan["steps"] == [] and plan["verdict"] == "auto", plan
    assert plan["wanted"]["commit"] == plan["pinned"]["commit"]
    env_plan = planmod.make_env_plan(cx, planmod.PlanOptions(), cwd=_elsewhere(tmp_path))
    assert _steps_but_webapp(env_plan) == [] and env_plan["verdict"] == "auto", env_plan

    # The first bay up after the move: no code target, every container a noop.
    seen: list[dict[str, Any]] = []
    up = _up(world, box, _elsewhere(tmp_path), seen=seen)
    assert up["code_targets"] == {} and "code_targets" not in seen[0]
    # The fake box hashes the whole entry, build table included, so it calls
    # old-shop a recreate. The real container spec carries only `build: true`
    # (roles/container_lifecycle/tasks/build_specs.yml), and that entry is the same.
    actions = _receipt_actions(box)
    assert {a for n, a in actions.items() if n != "old-shop"} == {"noop"}
    assert _without_build(after["services"]["old-shop"]) == _without_build(
        before["services"]["old-shop"]
    )
    assert _body((fleet / GENERATED_SERVICES).read_text()) == after_text
    # Steady state: a second plan and up change nothing.
    again = planmod.make_plan(
        planmod.load_project(cx, "shop", cwd=_elsewhere(tmp_path)), planmod.PlanOptions()
    )
    assert again["steps"] == [] and again["verdict"] == "auto", again


def _drop_hold_lines(text: str) -> str:
    keep = []
    skip_list = False
    for line in text.splitlines():
        stripped = line.strip()
        key = stripped.split(":", 1)[0]
        if skip_list and stripped.startswith("- "):
            continue
        skip_list = False
        if key in planmod._HOLD_KEYS:
            skip_list = key == "bay_toml_files"
            continue
        keep.append(line)
    return "\n".join(keep)


def test_delete_push_after_the_move_changes_no_plan(
    world: dict[str, Path], shop: dict[str, Path], box: FakeBox, tmp_path: Path
) -> None:
    """Move, bay up, then delete bay.toml in the app repo and push: the fleet ignores it."""
    assert _move(world, cwd=_elsewhere(tmp_path)).exit_code == 0
    _up(world, box, _elsewhere(tmp_path))
    git(shop["app"], "rm", "-q", "-r", "bay.toml", "site.conf", "deploy/x.yaml")
    git(shop["app"], "commit", "-q", "-m", "bay.toml lives in the fleet")
    git(shop["app"], "push", "-q", "origin", "main")
    plan = planmod.make_plan(
        planmod.load_project(cx_of(world), "shop", cwd=shop["app"]), planmod.PlanOptions()
    )
    assert plan["steps"] == [] and plan["verdict"] == "auto", plan
    build = yaml.safe_load(_body((world["fleet"] / GENERATED_SERVICES).read_text()))[
        "services"
    ]["old-shop"]["build"]
    assert not set(planmod._HOLD_KEYS) & set(build), "rebuild.sh holds no toml path any more"


def test_move_check_changes_nothing(
    world: dict[str, Path], shop: dict[str, Path], tmp_path: Path
) -> None:
    fleet = world["fleet"]
    head = git(fleet, "rev-parse", "HEAD")
    lock_bytes = lockfile.lock_path(fleet, "shop").read_bytes()
    result = _move(world, "--check", "--json", cwd=_elsewhere(tmp_path))
    assert result.exit_code == 0, _said(result)
    doc = json.loads(result.stdout)
    assert doc["check"] is True and doc["fleet_commit"] is None and doc["lock_commit"] is None
    assert {(f["from"], f["to"]) for f in doc["files"]} == {
        ("bay.toml", "projects/shop/bay.toml"),
        ("site.conf", "projects/shop/site.conf"),
        ("deploy/x.yaml", "projects/shop/deploy/x.yaml"),
    }
    assert any('"from_app_commit"' in line for line in doc["lock_diff"])
    assert any("<the move commit>" in line for line in doc["lock_diff"])
    assert git(fleet, "rev-parse", "HEAD") == head
    assert lockfile.lock_path(fleet, "shop").read_bytes() == lock_bytes
    assert git(fleet, "status", "--porcelain") == ""
    assert not (shop["folder"] / "bay.toml").exists()
    said = _said(_move(world, "--check", cwd=_elsewhere(tmp_path)))
    assert "would copy site.conf (app repo) -> projects/shop/site.conf" in said
    assert "nothing changed" in said
    assert git(fleet, "rev-parse", "HEAD") == head


def test_move_text_output_names_the_order(
    world: dict[str, Path], shop: dict[str, Path], tmp_path: Path
) -> None:
    result = _move(world, cwd=_elsewhere(tmp_path))
    assert result.exit_code == 0, _said(result)
    said = " ".join(_said(result).split())
    assert "`bay plan production --project shop` (it must show 0 steps)" in said
    assert said.index("`bay up production --project shop`") < said.index("`git rm -r bay.toml")
    assert "Pushed before bay up, the box builds it and holds it" in said


def test_move_from_inside_the_app_checkout(
    world: dict[str, Path], shop: dict[str, Path]
) -> None:
    """In the checkout, the fleet comes from the bay.toml there (or --fleet) as for bay plan."""
    result = _move(world, cwd=shop["app"])
    assert result.exit_code == 0, _said(result)
    assert (shop["folder"] / "bay.toml").read_text() == SHOP_TOML


def test_move_monorepo_toml_path(
    world: dict[str, Path], box: FakeBox, tmp_path: Path
) -> None:
    """A bay.toml in a subdirectory: its mounted files move from beside it."""
    fleet = world["fleet"]
    remote = tmp_path / "remotes" / "mono.git"
    git(tmp_path, "init", "-q", "--bare", str(remote))
    app = tmp_path / "mono"
    git(tmp_path, "clone", "-q", str(remote), str(app))
    base = app / "services" / "api"
    (base / "conf").mkdir(parents=True)
    toml = STATUS_TOML.replace('name = "status"', 'name = "api"').replace(
        "status.example.com", "api.example.com"
    ) + '\n[[mounts]]\npath = "/etc/api"\nfrom = "conf"\n'
    (base / "bay.toml").write_text(toml)
    (base / "conf" / "a.yaml").write_text("a: 1\n")
    (base / "conf" / "b.yaml").write_text("b: 1\n")
    commit_all(app, "mono")
    git(app, "push", "-q", "-u", "origin", "main")
    lockfile.write(
        lockfile.lock_path(fleet, "api"),
        lockfile.new_lock("api", repo=str(remote), toml_path="services/api/bay.toml"),
    )
    commit_all(fleet, "bay: init api")
    up = applymod.up(planmod.load_project(cx_of(world), "api", cwd=app), planmod.PlanOptions())
    assert up["result"] == "ok", up
    before = _body((fleet / GENERATED_SERVICES).read_text())

    result = cli(world, "toml", "move", "api", "--to", "fleet", "--json", cwd=_elsewhere(tmp_path))
    assert result.exit_code == 0, _said(result)
    doc = json.loads(result.stdout)
    assert doc["delete"] == ["services/api/bay.toml", "services/api/conf"]
    folder = fleet / "projects" / "api"
    assert (folder / "bay.toml").read_text() == toml
    assert (folder / "conf" / "a.yaml").read_text() == "a: 1\n"
    assert (folder / "conf" / "b.yaml").read_text() == "b: 1\n"
    with planmod.compiled_fleet(cx_of(world)) as comp:
        assert comp.result is not None, comp.errors
        # An image project: no build, so no hold keys; the compile is byte-identical.
        assert _body(comp.result.text()) == before
    plan = planmod.make_plan(planmod.load_project(cx_of(world), "api"), planmod.PlanOptions())
    assert plan["steps"] == [] and plan["verdict"] == "auto", plan


# ── refusals ────────────────────────────────────────────────────────────────


def test_move_refusals_change_nothing(
    world: dict[str, Path], shop: dict[str, Path], box: FakeBox, tmp_path: Path
) -> None:
    fleet, app = world["fleet"], shop["app"]
    where = _elsewhere(tmp_path)
    heads = [git(fleet, "rev-parse", "HEAD")]

    def refused(text: str, *args: str, name: str = "shop", code: int | None = None) -> None:
        result = cli(world, "toml", "move", name, "--to", "fleet", *args, cwd=where)
        assert result.exit_code != 0, _said(result)
        if code is not None:
            assert result.exit_code == code, _said(result)
        assert text in _said(result), _said(result)
        assert git(fleet, "rev-parse", "HEAD") == heads[0]
        assert not (shop["folder"] / "bay.toml").exists()

    # Not a project of the fleet; one that lives in the fleet already.
    refused("project ghost is not in fleet testfleet", name="ghost")
    status = fleet / "projects" / "status"
    status.mkdir()
    (status / "bay.toml").write_text(STATUS_TOML)
    heads[0] = commit_all(fleet, "add status")
    refused("status already lives in fleet testfleet", name="status")
    # A project with no pin yet (webapp was never deployed).
    refused("webapp has no pinned commit yet", name="webapp")
    # --toml-path is for --to repo.
    refused("--toml-path is for --to repo", "--toml-path", "x/bay.toml")

    # Uncommitted fleet files: the bay up gate, exit 41.
    (fleet / "notes.txt").write_text("draft\n")
    refused("uncommitted path(s): notes.txt", code=41)
    (fleet / "notes.txt").unlink()

    # A config change in the app repo that bay up has not deployed.
    (app / "bay.toml").write_text(SHOP_TOML.replace('MODE = "one"', 'MODE = "two"'))
    commit_all(app, "undeployed config")
    git(app, "push", "-q", "origin", "main")
    refused("bay.toml changed after bay up pinned it")
    git(app, "revert", "--no-edit", "HEAD")
    git(app, "push", "-q", "origin", "main")
    # ... and in a file its mounts read.
    (app / "site.conf").write_text("site: 2\n")
    commit_all(app, "undeployed file")
    git(app, "push", "-q", "origin", "main")
    refused("site.conf changed after bay up pinned it")
    git(app, "revert", "--no-edit", "HEAD")
    git(app, "push", "-q", "origin", "main")

    # The lock is not settled: pending, claimed, failed.
    for change, text in (
        ({"result": "pending"}, "has not ended (result pending)"),
        ({"claimed_by": "other-machine"}, "has not ended"),
        ({"result": "failed"}, "the last bay up failed"),
        ({"frozen": True}, "frozen by bay rollback in production"),
    ):
        raw = _lock(world)
        saved = json.loads(json.dumps(raw))
        raw["envs"]["production"].update(change)
        if "claimed_by" in change:
            raw["envs"]["production"]["claimed_at"] = "2026-11-10T10:00:00Z"
        lockfile.write(lockfile.lock_path(fleet, "shop"), raw)
        heads[0] = commit_all(fleet, f"lock {change}")
        refused(text)
        lockfile.write(lockfile.lock_path(fleet, "shop"), saved)
        heads[0] = commit_all(fleet, "lock back")

    # The project folder holds another file.
    (shop["folder"] / "leftover.txt").write_text("x\n")
    heads[0] = commit_all(fleet, "leftover")
    refused("projects/shop holds files other than bay.lock: projects/shop/leftover.txt")
    git(fleet, "rm", "-q", "projects/shop/leftover.txt")
    heads[0] = commit_all(fleet, "no leftover")

    # Another lock names the same bay.toml of the same repo.
    twin = lockfile.new_lock("twin", repo=str(shop["remote"]))
    lockfile.write(lockfile.lock_path(fleet, "twin"), twin)
    heads[0] = commit_all(fleet, "twin")
    refused("projects shop and twin both name bay.toml in")
    git(fleet, "rm", "-q", "-r", "projects/twin")
    heads[0] = commit_all(fleet, "no twin")

    # The fleet's Bay floor names a newer Bay: exit 45.
    (fleet / "bay.compat.json").write_text('{"compat_version": 1, "deployed_with": "99.0.0"}\n')
    heads[0] = commit_all(fleet, "newer bay")
    refused("99.0.0", code=45)


def test_move_refuses_track_pin(
    world: dict[str, Path], shop: dict[str, Path], box: FakeBox, tmp_path: Path
) -> None:
    app = shop["app"]
    (app / "bay.toml").write_text(SHOP_TOML + 'track = "pin"\n')
    commit_all(app, "track pin")
    git(app, "push", "-q", "origin", "main")
    _up(world, box, app)
    head = git(world["fleet"], "rev-parse", "HEAD")
    result = _move(world, cwd=_elsewhere(tmp_path))
    assert result.exit_code != 0
    said = _said(result)
    assert 'shop sets track = "pin" in production; a project in the fleet cannot use it' in said
    assert 'Set track = "branch"' in str(result.exception.hint)
    assert git(world["fleet"], "rev-parse", "HEAD") == head


def test_move_refuses_a_diverged_fleet(
    world: dict[str, Path], shop: dict[str, Path], tmp_path: Path
) -> None:
    fleet = world["fleet"]
    remote = tmp_path / "fleet-remote.git"
    git(tmp_path, "init", "-q", "--bare", str(remote))
    git(fleet, "remote", "add", "origin", str(remote))
    git(fleet, "push", "-q", "-u", "origin", "main")
    other = tmp_path / "other-machine"
    git(tmp_path, "clone", "-q", str(remote), str(other))
    (other / "README.md").write_text("theirs\n")
    commit_all(other, "theirs")
    git(other, "push", "-q", "origin", "main")
    (fleet / "NOTES.md").write_text("mine\n")
    commit_all(fleet, "mine")
    head = git(fleet, "rev-parse", "HEAD")
    result = _move(world, "--json", cwd=_elsewhere(tmp_path))
    assert result.exit_code == 41, _said(result)
    doc = json.loads(result.stdout)
    assert doc["reason"] == "diverged" and "have diverged" in doc["error"]
    assert git(fleet, "rev-parse", "HEAD") == head


def test_move_fast_forwards_a_fleet_that_is_behind(
    world: dict[str, Path], shop: dict[str, Path], tmp_path: Path
) -> None:
    fleet = world["fleet"]
    remote = tmp_path / "fleet-remote.git"
    git(tmp_path, "init", "-q", "--bare", str(remote))
    git(fleet, "remote", "add", "origin", str(remote))
    git(fleet, "push", "-q", "-u", "origin", "main")
    other = tmp_path / "other-machine"
    git(tmp_path, "clone", "-q", str(remote), str(other))
    (other / "README.md").write_text("theirs\n")
    theirs = commit_all(other, "theirs")
    git(other, "push", "-q", "origin", "main")
    result = _move(world, cwd=_elsewhere(tmp_path))
    assert result.exit_code == 0, _said(result)
    assert git(fleet, "rev-parse", "HEAD~2") == theirs


# ── after the move ──────────────────────────────────────────────────────────


def test_rollback_after_move_refused(
    world: dict[str, Path], shop: dict[str, Path], box: FakeBox, tmp_path: Path
) -> None:
    # A second app pin, so previous names an app commit before the move.
    (shop["app"] / "bay.toml").write_text(SHOP_TOML.replace('MODE = "one"', 'MODE = "two"'))
    commit_all(shop["app"], "two")
    git(shop["app"], "push", "-q", "origin", "main")
    _up(world, box, shop["app"])
    assert _lock(world)["envs"]["production"]["previous"]["commit"]
    assert _move(world, cwd=_elsewhere(tmp_path)).exit_code == 0
    head = git(world["fleet"], "rev-parse", "HEAD")
    result = cli(world, "rollback", "--project", "shop", cwd=_elsewhere(tmp_path))
    assert result.exit_code != 0
    said = _said(result)
    assert "the previous pin is an app repo commit; use `bay up --at <commit>`" in said
    assert git(world["fleet"], "rev-parse", "HEAD") == head
    assert "bay rollback --to <commit>" in str(result.exception.hint)
    # Still refused after a bay up to the same commit: there is no earlier fleet pin.
    _up(world, box, _elsewhere(tmp_path))
    again = cli(world, "rollback", "--project", "shop", cwd=_elsewhere(tmp_path))
    assert "the previous pin is an app repo commit" in _said(again)
    # After a config change in the fleet, previous is a fleet commit: rollback works.
    toml = shop["folder"] / "bay.toml"
    toml.write_text(toml.read_text().replace('MODE = "two"', 'MODE = "three"'))
    moved = _lock(world)["commit"]
    commit_all(world["fleet"], "shop three")
    _up(world, box, _elsewhere(tmp_path))
    assert _lock(world)["envs"]["production"]["previous"]["commit"] == moved
    proj = planmod.load_project(cx_of(world), "shop", cwd=_elsewhere(tmp_path))
    rolled = applymod.rollback(proj, planmod.PlanOptions(), deploy=_deploy(box))
    assert rolled["result"] == "ok", rolled
    assert _lock(world)["commit"] == moved


def test_round_trip_fleet_then_repo(
    world: dict[str, Path], shop: dict[str, Path], box: FakeBox, tmp_path: Path
) -> None:
    """--to fleet, bay up, delete; then --to repo, bay up, push: 0 steps and the same bytes."""
    fleet, app = world["fleet"], shop["app"]
    original = (app / "bay.toml").read_bytes()
    before = yaml.safe_load(_body((fleet / GENERATED_SERVICES).read_text()))
    assert _move(world, cwd=_elsewhere(tmp_path)).exit_code == 0
    _up(world, box, _elsewhere(tmp_path))
    git(app, "rm", "-q", "-r", "bay.toml", "site.conf", "deploy/x.yaml")
    git(app, "commit", "-q", "-m", "bay.toml lives in the fleet")
    git(app, "push", "-q", "origin", "main")

    back = cli(world, "toml", "move", "shop", "--to", "repo", cwd=app)
    assert back.exit_code == 0, _said(back)
    assert (app / "bay.toml").read_bytes() == original
    assert (app / "site.conf").read_text() == "site: 1\n"
    assert (app / "deploy" / "x.yaml").read_text() == "x: 1\n"
    raw = _lock(world)
    assert raw["toml_path"] == "bay.toml" and raw["repo"] == str(shop["remote"])
    adopted = dict(raw["envs"]["production"]["adopted"])
    assert "from_app_commit" not in adopted, "adopt drops the mark of the move"
    for key in ("from_fleet_commit", "app_commit"):
        adopted.pop(key)
    assert adopted == ADOPTED
    proj = planmod.load_project(cx_of(world), "shop", cwd=app)
    assert proj.in_fleet is False
    plan = planmod.make_plan(proj, planmod.PlanOptions())
    assert plan["steps"] == [] and plan["verdict"] == "auto", plan
    seen: list[dict[str, Any]] = []
    up = _up(world, box, app, seen=seen)
    assert up["code_targets"] == {}, "the adopt commit moves no code"
    assert {a for n, a in _receipt_actions(box).items() if n != "old-shop"} == {"noop"}
    git(app, "push", "-q", "origin", "main")
    after = yaml.safe_load(_body((fleet / GENERATED_SERVICES).read_text()))
    # Back in the app repo: the hold keys come back with the same values.
    assert after == before


# ── two projects in one app repo ────────────────────────────────────────────


def test_move_with_a_sibling_project_in_the_same_repo(
    world: dict[str, Path], shop: dict[str, Path], box: FakeBox, tmp_path: Path
) -> None:
    """The simple case: another project keeps its bay.toml in the repo. The move goes on,
    names it, and keeps the files it reads in the app repo."""
    fleet, app = world["fleet"], shop["app"]
    # The sibling's toml sits at the repo root too and reads site.conf.
    admin_toml = (
        STATUS_TOML.replace('name = "status"', 'name = "admin"')
        .replace("status.example.com", "admin.example.com")
        + '\n[[mounts]]\npath = "/etc/admin/site.conf"\nfrom = "site.conf"\n'
    )
    (app / "admin.toml").write_text(admin_toml)
    commit_all(app, "admin")
    git(app, "push", "-q", "origin", "main")
    lockfile.write(
        lockfile.lock_path(fleet, "admin"),
        lockfile.new_lock("admin", repo=str(shop["remote"]), toml_path="admin.toml"),
    )
    commit_all(fleet, "bay: init admin")
    up = applymod.up(
        planmod.load_project(cx_of(world), "admin", cwd=app),
        planmod.PlanOptions(),
        deploy=_deploy(box),
    )
    assert up["result"] == "ok", up

    result = _move(world, "--json", cwd=_elsewhere(tmp_path))
    assert result.exit_code == 0, _said(result)
    doc = json.loads(result.stdout)
    assert doc["keep_in_repo"] == ["site.conf"]
    assert doc["delete"] == ["bay.toml", "deploy/x.yaml"]
    assert any(n.startswith("admin also keeps its bay.toml in") for n in doc["notes"])
    plan = planmod.make_env_plan(cx_of(world), planmod.PlanOptions(), cwd=_elsewhere(tmp_path))
    assert _steps_but_webapp(plan) == [] and plan["verdict"] == "auto", plan


# ── the adopt alias ─────────────────────────────────────────────────────────


def test_adopt_alias_prints_the_note_and_toml_move_repo_works(
    world: dict[str, Path], box: FakeBox, tmp_path: Path
) -> None:
    fleet = world["fleet"]
    for name in ("one", "two"):
        folder = fleet / "projects" / name
        folder.mkdir()
        (folder / "bay.toml").write_text(
            STATUS_TOML.replace('name = "status"', f'name = "{name}"').replace(
                "status.example.com", f"{name}.example.com"
            )
        )
    commit_all(fleet, "two in-fleet projects")
    apps = {}
    for name in ("one", "two"):
        remote = tmp_path / "remotes" / f"{name}.git"
        git(tmp_path, "init", "-q", "--bare", str(remote))
        app = tmp_path / name
        git(tmp_path, "clone", "-q", str(remote), str(app))
        (app / "README").write_text(f"{name}\n")
        commit_all(app, "app")
        git(app, "push", "-q", "-u", "origin", "main")
        apps[name] = app

    old = cli(world, "adopt", "one", "--json", cwd=apps["one"])
    assert old.exit_code == 0, _said(old)
    assert "note: bay adopt is now `bay toml move one --to repo`" in old.stderr
    new = cli(world, "toml", "move", "two", "--to", "repo", "--json", cwd=apps["two"])
    assert new.exit_code == 0, _said(new)
    assert "bay adopt is now" not in new.stderr
    for name in ("one", "two"):
        assert (apps[name] / "bay.toml").is_file()
        assert git(apps[name], "log", "-1", "--format=%s") == (
            "chore: add bay.toml (adopted from fleet testfleet)"
        )

    # The same document apart from the names and the commit ids. (The text form
    # wraps long paths at the terminal width, so compare the JSON.)
    def norm(text: str, name: str) -> str:
        import re

        return re.sub(r"[0-9a-f]{12,40}", "<sha>", text.replace(name, "X"))

    assert norm(old.stdout, "one") == norm(new.stdout, "two")


def test_adopt_is_marked_deprecated_in_help() -> None:
    from typer.testing import CliRunner

    from bay_cli.cli import app

    out = CliRunner().invoke(app, ["--help"]).output
    assert "Deprecated: use `bay toml move" in " ".join(out.split())
    move_help = CliRunner().invoke(app, ["toml", "move", "--help"]).output
    assert "--to" in move_help and "--check" in move_help
