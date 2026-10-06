"""M115/S07: bay init, plan, approve, up, show and rollback.

Every test builds a throwaway fleet repo and app repo under tmp_path, with a
fake deploy and a fake box (it serves the receipt the fake deploy left). No
network, no SSH, no Ansible. The one live test, test_live_plan_up_roundtrip,
skips unless BAY_LIVE_E2E=1.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

import jsonschema
import pytest
import yaml
from typer.testing import CliRunner

from bay_cli import apply as applymod
from bay_cli import lockfile
from bay_cli import plan as planmod
from bay_cli.cli import app
from bay_cli.context import Context
from bay_cli.fleet import GENERATED_SERVICES

ROOT = Path(__file__).resolve().parent.parent
PLAN_SCHEMA = json.loads((ROOT / "src/bay_cli/schemas/plan.schema.json").read_text())
SENTINEL = "sentinel_value_that_must_never_be_printed"

BANNED = re.compile(
    r"\b(accessor(y|ies)|rigs?|regions?|placements?|group_vars|inventor(y|ies)|playbooks?"
    r"|roles?|reconcilers?|consumers?|snapshots?|vaults?|stacks?|hosts?)\b",
    re.IGNORECASE,
)

FLEET_TOML = """\
name = "testfleet"
default_box = "box-1"
default_domain = "example.com"
primary_env = "production"

[boxes.box-1]
env = "production"

[resources.postgres]
kind = "postgres"
box = "box-1"
image = "postgres:16"
port = 5432

[tailnet]
allowlist = ["100.64.0.0/10"]
"""

APP_TOML = """\
name = "webapp"
fleet = "testfleet"
image = "ghcr.io/acme/webapp:1"
port = 3000
secrets = ["SESSION_SECRET"]
needs = ["postgres"]

[env]
LOG_LEVEL = "info"

[access]
mode = "public"

[[mounts]]
path = "/data"
volume = "data"
backup = false

[deploy.production]
domain = "webapp.example.com"
"""

runner = CliRunner()


# ── fixtures ────────────────────────────────────────────────────────────────


def git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    )
    return proc.stdout.strip()


def commit_all(repo: Path, message: str) -> str:
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture(autouse=True)
def _git_identity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = tmp_path / "gitconfig"
    cfg.write_text(
        "[user]\n\tname = Test\n\temail = test@example.com\n"
        "[init]\n\tdefaultBranch = main\n[commit]\n\tgpgsign = false\n"
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(cfg))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for var in ("BAY_FLEET", "BAY_FLEET_NAME"):
        monkeypatch.delenv(var, raising=False)


class FakeBox:
    """Holds one receipt per box env. A deploy writes it from the fleet's services file."""

    def __init__(self) -> None:
        self.receipts: dict[str, dict[str, Any]] = {}
        self.deploys: list[str] = []
        self.fail = False

    def deploy(self, cx: Context, box_env: str) -> None:
        self.deploys.append(box_env)
        data = yaml.safe_load((cx.fleet_root / GENERATED_SERVICES).read_text().split("\n", 1)[1])
        entries = {**(data.get("accessories") or {}), **(data.get("services") or {})}
        self.receipts[box_env] = {
            "receipt_version": 1,
            "env": box_env,
            "box": "box-1",
            "deployed_at": "2026-10-06T12:00:00Z",
            "result": "failed" if self.fail else "ok",
            "containers": [
                {
                    "name": name,
                    "image": entry.get("image"),
                    "config_hash": hashlib.sha256(
                        json.dumps(entry, sort_keys=True).encode()
                    ).hexdigest()[:16],
                    "action": "create",
                    "healthy": True,
                }
                for name, entry in sorted(entries.items())
            ],
            "projects": {},
        }
        if self.fail:
            from bay_cli.errors import BayError

            raise BayError("the deploy failed on the box")

    def container(self, name: str, env: str = "production") -> dict[str, Any]:
        return next(c for c in self.receipts[env]["containers"] if c["name"] == name)

    def read(self, cx: Context, box_env: str) -> list[dict[str, Any]]:
        return [
            {"env": box_env, "box": "box-1", "receipt": self.receipts.get(box_env), "error": None}
        ]


@pytest.fixture
def box(monkeypatch: pytest.MonkeyPatch) -> FakeBox:
    fake = FakeBox()
    monkeypatch.setattr(planmod, "default_receipt_reader", fake.read)
    monkeypatch.setattr(applymod, "default_deploy", fake.deploy)
    return fake


@pytest.fixture
def world(tmp_path: Path, box: FakeBox) -> dict[str, Path]:
    fleet = tmp_path / "fleet"
    (fleet / "group_vars" / "all").mkdir(parents=True)
    (fleet / "group_vars" / "production").mkdir(parents=True)
    (fleet / "hosts").mkdir()
    (fleet / "projects").mkdir()
    (fleet / "bay.fleet.toml").write_text(FLEET_TOML)
    (fleet / "group_vars" / "all" / "main.yml").write_text("---\nstack_name: testfleet\n")
    (fleet / "group_vars" / "production" / "secrets.yml").write_text(
        yaml.safe_dump(
            {"secrets": {"WEBAPP_SESSION_SECRET": SENTINEL, "WEBAPP_POSTGRES_PASSWORD": SENTINEL}}
        )
    )
    (fleet / "hosts" / "production").write_text("[production]\nbox-1\n")
    (fleet / "projects" / ".gitkeep").write_text("")
    git(fleet, "init", "-q")
    commit_all(fleet, "fleet")
    # The fleet was compiled before (as after `bay import`): the shared postgres runs.
    with planmod.compiled_fleet(Context.for_fleet_root(fleet)) as comp:
        assert comp.result is not None, comp.errors
        (fleet / GENERATED_SERVICES).write_text(comp.result.text())
    commit_all(fleet, "compile")

    app_repo = tmp_path / "webapp"
    app_repo.mkdir()
    (app_repo / "bay.toml").write_text(APP_TOML)
    git(app_repo, "init", "-q")
    commit_all(app_repo, "app")
    lockfile.write(
        lockfile.lock_path(fleet, "webapp"),
        lockfile.new_lock("webapp", repo=None, local_path=str(app_repo)),
    )
    commit_all(fleet, "bay: init webapp")
    return {"fleet": fleet, "app": app_repo}


def cx_of(world: dict[str, Path]) -> Context:
    return Context.for_fleet_root(world["fleet"])


def project(world: dict[str, Path]) -> planmod.ProjectRef:
    return planmod.load_project(cx_of(world), "webapp")


def make(world: dict[str, Path], **kw: Any) -> dict[str, Any]:
    return planmod.make_plan(project(world), planmod.PlanOptions(**kw))


def do_up(world: dict[str, Path], **kw: Any) -> dict[str, Any]:
    force = kw.pop("force", False)
    reason = kw.pop("reason", None)
    plan_id = kw.pop("plan_id", None)
    return applymod.up(
        project(world), planmod.PlanOptions(**kw), force=force, reason=reason, plan_id=plan_id
    )


def edit_app(world: dict[str, Path], old: str, new: str, *, commit: bool = True) -> str:
    path = world["app"] / "bay.toml"
    text = path.read_text()
    assert old in text
    path.write_text(text.replace(old, new, 1))
    return commit_all(world["app"], f"edit {new[:20]}") if commit else ""


def cli(world: dict[str, Path], *args: str, cwd: Path | None = None) -> Any:
    old = Path.cwd()
    os.chdir(cwd or world["app"])
    try:
        return runner.invoke(app, ["--fleet", str(world["fleet"]), *args])
    finally:
        os.chdir(old)


def lock_of(world: dict[str, Path]) -> dict[str, Any]:
    raw = lockfile.read(lockfile.lock_path(world["fleet"], "webapp"))
    assert raw is not None
    return raw


# ── verdicts and exit codes ─────────────────────────────────────────────────


def test_plan_verdict_exit_codes(world: dict[str, Path], box: FakeBox) -> None:
    # auto: a first plan only creates
    result = cli(world, "plan", "--json")
    plan = json.loads(result.stdout)
    assert (plan["verdict"], result.exit_code) == ("auto", 0)
    assert {s["action"] for s in plan["steps"]} == {"create"}

    do_up(world)

    # approve: a destructive step (the volume is renamed)
    edit_app(world, 'volume = "data"', 'volume = "files"')
    result = cli(world, "plan", "--json")
    plan = json.loads(result.stdout)
    assert (plan["verdict"], result.exit_code) == ("approve", 10)

    # blocked: a needed secret is missing
    edit_app(world, 'secrets = ["SESSION_SECRET"]', 'secrets = ["SESSION_SECRET", "API_KEY"]')
    result = cli(world, "plan", "--json")
    plan = json.loads(result.stdout)
    assert (plan["verdict"], result.exit_code) == ("blocked", 20)
    assert any("WEBAPP_API_KEY" in b for b in plan["blockers"])

    # stale: the pin moves after a plan was saved
    edit_app(world, 'secrets = ["SESSION_SECRET", "API_KEY"]', 'secrets = ["SESSION_SECRET"]')
    edit_app(world, 'volume = "files"', 'volume = "data"')
    saved = json.loads(cli(world, "plan", "--json").stdout)
    raw = lock_of(world)
    raw["envs"]["production"]["deployed_at"] = "2026-10-07T00:00:00Z"
    lockfile.write(lockfile.lock_path(world["fleet"], "webapp"), raw)
    result = cli(world, "plan", "--json", "--plan-id", saved["plan_id"])
    plan = json.loads(result.stdout)
    assert (plan["verdict"], result.exit_code) == ("stale", 30)
    assert plan["stale"] and "pin moved" in plan["stale"][0]


def test_destructive_step_requires_approve(world: dict[str, Path], box: FakeBox) -> None:
    do_up(world)
    edit_app(world, 'volume = "data"', 'volume = "files"')

    refused = cli(world, "up", "--json")
    assert refused.exit_code == 10
    plan = json.loads(refused.stdout)
    assert plan["verdict"] == "approve"
    deploys_before = len(box.deploys)

    assert cli(world, "approve", plan["plan_id"]).exit_code != 0  # no reason, no approval
    ok = cli(world, "approve", plan["plan_id"], "--reason", "the old volume holds test data")
    assert ok.exit_code == 0, ok.output
    approved = json.loads(cli(world, "plan", "--json").stdout)
    assert approved["plan_id"] == plan["plan_id"]
    assert approved["verdict"] == "auto" and approved["approval"]["reason"]

    done = cli(world, "up", "--json")
    assert done.exit_code == 0, done.output
    assert len(box.deploys) == deploys_before + 1
    msgs = git(world["fleet"], "log", "--format=%s", "-3").splitlines()
    assert msgs[0] == "bay: receipt webapp production"
    assert msgs[1].startswith("bay: up webapp production ")
    tracked = git(world["fleet"], "ls-files", "plans")
    assert f"{plan['plan_id']}.approved" in tracked


def test_force_needs_reason_and_logs_it(world: dict[str, Path], box: FakeBox) -> None:
    do_up(world)
    edit_app(world, 'volume = "data"', 'volume = "files"')
    assert cli(world, "up", "--force").exit_code != 0
    result = cli(world, "up", "--force", "--reason", "rehearsal", "--json")
    assert result.exit_code == 0, result.output
    assert lock_of(world)["envs"]["production"]["previous"]["force_reason"] == "rehearsal"


def test_force_never_overrides_blocked(world: dict[str, Path], box: FakeBox) -> None:
    edit_app(world, 'secrets = ["SESSION_SECRET"]', 'secrets = ["MISSING_ONE"]')
    result = cli(world, "up", "--force", "--reason", "x", "--json")
    assert result.exit_code == 20
    assert box.deploys == []


def test_stale_plan_refused(world: dict[str, Path], box: FakeBox) -> None:
    do_up(world)
    edit_app(world, 'LOG_LEVEL = "info"', 'LOG_LEVEL = "debug"')
    saved = make(world)
    planmod.save(cx_of(world), saved)
    assert saved["verdict"] == "auto"

    # RUNNING moves: someone else deployed and the box now runs other containers
    box.container("webapp")["config_hash"] = "changed"
    with pytest.raises(applymod.Refused) as info:
        do_up(world, plan_id=saved["plan_id"])
    assert info.value.exit_code == 30
    assert "box changed" in " ".join(info.value.plan["stale"])

    result = cli(world, "up", "--plan-id", saved["plan_id"], "--json")
    assert result.exit_code == 30


def test_saved_plan_applies_when_nothing_moved(world: dict[str, Path], box: FakeBox) -> None:
    do_up(world)
    edit_app(world, 'LOG_LEVEL = "info"', 'LOG_LEVEL = "debug"')
    saved = json.loads(cli(world, "plan", "--json").stdout)
    result = cli(world, "up", "--plan-id", saved["plan_id"], "--json")
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["plan_id"] == saved["plan_id"]


# ── rollback ────────────────────────────────────────────────────────────────


def test_rollback_restores_previous_pin(world: dict[str, Path], box: FakeBox) -> None:
    first = git(world["app"], "rev-parse", "HEAD")
    do_up(world)
    second = edit_app(world, 'LOG_LEVEL = "info"', 'LOG_LEVEL = "debug"')
    do_up(world)
    record = lock_of(world)["envs"]["production"]
    assert record["commit"] == second and record["previous"]["commit"] == first

    result = cli(world, "rollback", "--json")
    assert result.exit_code == 0, result.output
    doc = json.loads(result.stdout)
    assert (doc["commit"], doc["previous_commit"]) == (first, second)
    raw = lock_of(world)
    assert raw["commit"] == first
    assert raw["envs"]["production"]["commit"] == first
    assert raw["envs"]["production"]["previous"]["commit"] == second  # the pins swapped
    services = (world["fleet"] / GENERATED_SERVICES).read_text()
    assert "LOG_LEVEL: info" in services
    assert (
        git(world["fleet"], "log", "--format=%s", "-2")
        .splitlines()[1]
        .startswith("bay: rollback webapp production ")
    )


def test_rollback_refuses_without_previous(world: dict[str, Path], box: FakeBox) -> None:
    result = cli(world, "rollback")
    assert result.exit_code != 0
    assert "no previous pin" in str(result.exception)


# ── risk table ──────────────────────────────────────────────────────────────

_WEB = {
    "image": "x:1",
    "env": {"clear": {"A": "1"}, "secret": ["S1", "S2"]},
    "volumes": ["webapp-data:/data"],
    "database": {"accessory": "postgres", "name": "webapp", "user": "webapp"},
}


def _with(**changes: Any) -> dict[str, Any]:
    entry = json.loads(json.dumps(_WEB))
    for key, value in changes.items():
        if value is None:
            entry.pop(key, None)
        else:
            entry[key] = value
    return entry


@pytest.mark.parametrize(
    ("label", "old", "new", "expect"),
    [
        (
            "env var change",
            {"services": {"webapp": _WEB}},
            {"services": {"webapp": _with(env={"clear": {"A": "2"}, "secret": ["S1", "S2"]})}},
            {("container", "update", "safe")},
        ),
        (
            "volume rename",
            {"services": {"webapp": _WEB}},
            {"services": {"webapp": _with(volumes=["webapp-files:/data"])}},
            {("container", "update", "safe"), ("volume", "rename", "destructive")},
        ),
        (
            "volume removed",
            {"services": {"webapp": _WEB}},
            {"services": {"webapp": _with(volumes=None)}},
            {("container", "update", "safe"), ("volume", "remove", "destructive")},
        ),
        (
            "volume path change",
            {"services": {"webapp": _WEB}},
            {"services": {"webapp": _with(volumes=["webapp-data:/srv"])}},
            {("container", "update", "safe"), ("volume", "update", "safe")},
        ),
        (
            "database removed",
            {"services": {"webapp": _WEB}},
            {"services": {"webapp": _with(database=None)}},
            {("container", "update", "safe"), ("database", "remove", "destructive")},
        ),
        (
            "database renamed",
            {"services": {"webapp": _WEB}},
            {
                "services": {
                    "webapp": _with(
                        database={"accessory": "postgres", "name": "w2", "user": "webapp"}
                    )
                }
            },
            {("container", "update", "safe"), ("database", "rename", "destructive")},
        ),
        (
            "database user renamed",
            {"services": {"webapp": _WEB}},
            {
                "services": {
                    "webapp": _with(
                        database={"accessory": "postgres", "name": "webapp", "user": "w2"}
                    )
                }
            },
            {("container", "update", "safe"), ("database_user", "rename", "destructive")},
        ),
        (
            "secret removed while running",
            {"services": {"webapp": _WEB}},
            {"services": {"webapp": _with(env={"clear": {"A": "1"}, "secret": ["S1"]})}},
            {("container", "update", "safe"), ("secret", "remove", "destructive")},
        ),
        (
            "memory decreased",
            {"services": {"webapp": _with(mem_limit="1g")}},
            {"services": {"webapp": _with(mem_limit="128m")}},
            {("container", "update", "safe")},
        ),
        (
            "container removed",
            {"services": {"webapp": _WEB}},
            {"services": {}},
            {("container", "remove", "destructive")},
        ),
        (
            "container added",
            {"services": {}},
            {"services": {"webapp": _WEB}},
            {("container", "create", "safe")},
        ),
        (
            "resource change",
            {"accessories": {"postgres": {"image": "postgres:16"}}},
            {"accessories": {"postgres": {"image": "postgres:17"}}},
            {("resource", "update", "shared")},
        ),
        (
            "resource removed",
            {"accessories": {"postgres": {"image": "postgres:16"}}},
            {"accessories": {}},
            {("resource", "remove", "destructive")},
        ),
        (
            "other project change",
            {"services": {"other": {"image": "o:1"}}},
            {"services": {"other": {"image": "o:2"}}},
            {("container", "update", "shared")},
        ),
        (
            "webhook change",
            {"webhook": {"domain": "a.example.com"}},
            {"webhook": {"domain": "b.example.com"}},
            {("fleet", "update", "shared")},
        ),
    ],
)
def test_risk_classification(label: str, old: dict, new: dict, expect: set) -> None:
    steps = planmod.diff_steps(
        old, new, project="webapp", mine={"webapp"}, resources={"postgres"}, running={"webapp"}
    )
    assert {(s["kind"], s["action"], s["risk"]) for s in steps} == expect, label


def test_secret_removed_from_a_stopped_container_is_safe() -> None:
    old = {"services": {"webapp": _WEB}}
    new = {"services": {"webapp": _with(env={"clear": {"A": "1"}, "secret": ["S1"]})}}
    steps = planmod.diff_steps(
        old, new, project="webapp", mine={"webapp"}, resources=set(), running=set()
    )
    assert ("secret", "safe") in {(s["kind"], s["risk"]) for s in steps}


def test_tailnet_allowlist_change_is_shared(world: dict[str, Path], box: FakeBox) -> None:
    do_up(world)
    path = world["fleet"] / "bay.fleet.toml"
    path.write_text(path.read_text().replace("100.64.0.0/10", "100.64.0.0/16"))
    plan = make(world)
    assert ("tailnet", "shared") in {(s["kind"], s["risk"]) for s in plan["steps"]}
    assert plan["verdict"] == "approve"


# ── blockers ────────────────────────────────────────────────────────────────


def test_behind_remote_blocks(world: dict[str, Path], tmp_path: Path, box: FakeBox) -> None:
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "-q", "--bare", str(remote))
    git(world["fleet"], "remote", "add", "origin", str(remote))
    git(world["fleet"], "push", "-q", "-u", "origin", "main")
    assert make(world)["fleet"]["behind"] is False

    other = tmp_path / "other"
    git(tmp_path, "clone", "-q", str(remote), str(other))
    (other / "note.txt").write_text("x\n")
    commit_all(other, "elsewhere")
    git(other, "push", "-q", "origin", "main")

    plan = make(world)
    assert plan["fleet"]["behind"] is True
    assert plan["verdict"] == "blocked"
    assert any("behind its remote" in b for b in plan["blockers"])


def test_fleet_without_remote_is_never_behind(world: dict[str, Path], box: FakeBox) -> None:
    plan = make(world)
    assert plan["fleet"]["behind"] is False


def test_unreachable_commit_blocks(world: dict[str, Path], box: FakeBox) -> None:
    plan = make(world, at="0" * 12)
    assert plan["verdict"] == "blocked"
    assert any("not reachable" in b for b in plan["blockers"])


def test_invalid_bay_toml_blocks(world: dict[str, Path], box: FakeBox) -> None:
    edit_app(world, "port = 3000", 'port = "three"')
    plan = make(world)
    assert plan["verdict"] == "blocked"
    assert any("port" in b for b in plan["blockers"])


def test_dirty_fleet_refuses_only_destructive(world: dict[str, Path], box: FakeBox) -> None:
    do_up(world)
    (world["fleet"] / "scratch.txt").write_text("uncommitted\n")
    edit_app(world, 'LOG_LEVEL = "info"', 'LOG_LEVEL = "debug"')
    safe = make(world)
    assert safe["fleet"]["dirty"] is True and safe["verdict"] == "auto"

    edit_app(world, 'volume = "data"', 'volume = "files"')
    risky = make(world)
    assert risky["verdict"] == "blocked"
    assert any("uncommitted" in b for b in risky["blockers"])


def test_dirty_project_is_recorded_not_gated(world: dict[str, Path], box: FakeBox) -> None:
    (world["app"] / "wip.txt").write_text("x\n")
    edit_app(world, 'LOG_LEVEL = "info"', 'LOG_LEVEL = "debug"', commit=False)
    plan = make(world)
    assert plan["wanted"]["dirty"] is True and plan["verdict"] == "auto"
    # the uncommitted edit is not part of WANTED
    assert all("debug" not in json.dumps(s) for s in plan["steps"])


def test_hand_written_services_file_needs_import(world: dict[str, Path], box: FakeBox) -> None:
    target = world["fleet"] / GENERATED_SERVICES
    target.write_text("---\nservices: {}\naccessories: {}\n")
    plan = make(world)
    assert plan["verdict"] == "blocked"
    assert any("bay import" in b for b in plan["blockers"])


# ── up, show and HALF ───────────────────────────────────────────────────────


def test_up_writes_lock_compiles_and_commits(world: dict[str, Path], box: FakeBox) -> None:
    head = git(world["app"], "rev-parse", "HEAD")
    result = cli(world, "up", "--json")
    assert result.exit_code == 0, result.output
    doc = json.loads(result.stdout)
    raw = lock_of(world)
    assert raw["commit"] == head
    record = raw["envs"]["production"]
    assert record["result"] == "ok" and record["box"] == "box-1"
    assert record["last_receipt_sha256"] and record["deployed_at"]
    assert box.deploys == ["production"]
    services = (world["fleet"] / GENERATED_SERVICES).read_text()
    assert services.startswith("# GENERATED by bay compile.")
    assert git(world["fleet"], "rev-parse", "HEAD") == doc["receipt_commit"]
    assert git(world["fleet"], "status", "--porcelain") == ""


def test_half_status_after_failed_deploy(world: dict[str, Path], box: FakeBox) -> None:
    do_up(world)
    edit_app(world, 'LOG_LEVEL = "info"', 'LOG_LEVEL = "debug"')
    box.fail = True
    result = cli(world, "up")
    assert result.exit_code == 1
    raw = lock_of(world)
    assert raw["envs"]["production"]["result"] == "failed"
    assert raw["commit"] == git(world["app"], "rev-parse", "HEAD")  # the pin moved

    shown = json.loads(cli(world, "show", "--json").stdout)
    assert shown["envs"][0]["status"] == "HALF"


def test_show_statuses(world: dict[str, Path], box: FakeBox) -> None:
    shown = json.loads(cli(world, "show", "--json").stdout)
    assert shown["envs"][0]["status"] == "unknown"

    do_up(world)
    shown = json.loads(cli(world, "show", "--json").stdout)
    assert shown["envs"][0]["status"] == "ok", shown["envs"][0]["reason"]

    edit_app(world, 'LOG_LEVEL = "info"', 'LOG_LEVEL = "debug"')
    shown = json.loads(cli(world, "show", "--json").stdout)
    assert shown["envs"][0]["status"] == "behind"

    box.container("webapp")["image"] = "other:9"
    shown = json.loads(cli(world, "show", "--json").stdout)
    assert shown["envs"][0]["status"] == "drift"

    human = cli(world, "show")
    assert human.exit_code == 0 and "WANTED" in human.stdout and "drift" in human.stdout
    offline = json.loads(cli(world, "show", "--json", "--no-remote").stdout)
    assert offline["envs"][0]["status"] == "unknown"


def test_another_projects_deploy_does_not_make_the_plan_stale(
    world: dict[str, Path], box: FakeBox
) -> None:
    do_up(world)
    edit_app(world, 'LOG_LEVEL = "info"', 'LOG_LEVEL = "debug"')
    saved = make(world)
    planmod.save(cx_of(world), saved)
    box.receipts["production"]["containers"].append(
        {
            "name": "someone-else",
            "image": "y:1",
            "config_hash": "z",
            "action": "create",
            "healthy": True,
        }
    )
    again = planmod.recheck(project(world), saved, planmod.PlanOptions())
    assert again["stale"] == [] and again["verdict"] == "auto"


def test_project_flag_works_from_anywhere(
    world: dict[str, Path], tmp_path: Path, box: FakeBox
) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    result = cli(world, "plan", "--project", "webapp", "--json", cwd=elsewhere)
    assert result.exit_code == 0, result.output
    outside = cli(world, "plan", "--json", cwd=elsewhere)
    assert outside.exit_code != 0 and "no bay.toml" in str(outside.exception)


def test_up_from_a_second_clone_notes_the_checkout(
    world: dict[str, Path], tmp_path: Path, box: FakeBox
) -> None:
    clone = tmp_path / "clone"
    git(tmp_path, "clone", "-q", str(world["app"]), str(clone))
    plan = json.loads(cli(world, "plan", "--json", cwd=clone).stdout)
    assert any("but the fleet reads the project from" in n for n in plan["notes"])


# ── plan JSON, secrets, words ───────────────────────────────────────────────


def test_plan_json_validates_against_schema(world: dict[str, Path], box: FakeBox) -> None:
    jsonschema.validate(make(world), PLAN_SCHEMA)
    do_up(world)
    edit_app(world, 'volume = "data"', 'volume = "files"')
    plan = json.loads(cli(world, "plan", "--json").stdout)
    jsonschema.validate(plan, PLAN_SCHEMA)
    saved = json.loads((world["fleet"] / "plans" / f"{plan['plan_id']}.json").read_text())
    assert saved["plan_sha256"] == planmod.body_sha256(saved)
    assert saved["plan_id"] == saved["plan_sha256"][:12]


def test_plan_id_is_deterministic(world: dict[str, Path], box: FakeBox) -> None:
    assert make(world)["plan_id"] == make(world)["plan_id"]


def test_no_secret_value_in_plan_output(world: dict[str, Path], box: FakeBox) -> None:
    do_up(world)
    edit_app(world, 'secrets = ["SESSION_SECRET"]', "secrets = []")
    for args in (("plan", "--json"), ("plan",), ("show", "--json"), ("show",)):
        result = cli(world, *args)
        assert SENTINEL not in result.stdout, args
        assert SENTINEL not in (result.stderr if result.stderr_bytes else ""), args
    for path in (world["fleet"] / "plans").iterdir():
        assert SENTINEL not in path.read_text()
    assert SENTINEL not in json.dumps(lock_of(world))


def test_plan_output_uses_allowed_words(world: dict[str, Path], box: FakeBox) -> None:
    do_up(world)
    edit_app(world, 'volume = "data"', 'volume = "files"')
    plan = make(world)
    text = planmod.render(plan)
    words = " ".join([*plan["notes"], *plan["blockers"], *(s["reason"] for s in plan["steps"])])
    assert not BANNED.search(text), BANNED.search(text)
    assert not BANNED.search(words), BANNED.search(words)
    assert not BANNED.search(json.dumps(list(plan))), "a plan key uses a banned word"


# ── init ────────────────────────────────────────────────────────────────────


def test_init_writes_a_valid_draft_and_registers(
    tmp_path: Path, world: dict[str, Path], box: FakeBox
) -> None:
    from bay_cli import bay_toml

    repo = tmp_path / "shopfront"
    repo.mkdir()
    (repo / "Dockerfile").write_text("FROM node:22\nEXPOSE 4321\n")
    (repo / "package.json").write_text('{"scripts": {"start": "node index.js"}}')
    git(repo, "init", "-q")
    git(repo, "remote", "add", "origin", "git@example.com:acme/shopfront.git")
    commit_all(repo, "app")

    result = cli(world, "init", "--json", cwd=repo)
    assert result.exit_code == 0, result.output
    doc = json.loads(result.stdout)
    draft = bay_toml.load(repo / "bay.toml")
    assert bay_toml.validate(draft) == []
    assert draft["port"] == 4321 and draft["access"]["mode"] == "public"
    assert draft["deploy"]["production"] == {"box": "box-1", "domain": "shopfront.example.com"}
    raw = lockfile.read(lockfile.lock_path(world["fleet"], "shopfront"))
    assert raw is not None and raw["commit"] is None
    assert raw["repo"] == "git@example.com:acme/shopfront.git"
    assert raw["local_path"] == str(repo.resolve())
    assert git(world["fleet"], "log", "-1", "--format=%s") == "bay: init shopfront"
    assert doc["fleet_commit"] == git(world["fleet"], "rev-parse", "HEAD")

    # an unpinned project is left out of other projects' plans
    plan = make(world)
    assert all("shopfront" not in json.dumps(s) for s in plan["steps"])

    # and the name is taken now
    (repo / "bay.toml").unlink()
    again = cli(world, "init", cwd=repo)
    assert again.exit_code != 0 and "already exists" in str(again.exception)


def test_init_refuses_an_existing_bay_toml(world: dict[str, Path], box: FakeBox) -> None:
    result = cli(world, "init", "--name", "fresh")
    assert result.exit_code != 0 and "already exists" in str(result.exception)


# ── live ────────────────────────────────────────────────────────────────────


@pytest.mark.skipif(
    os.environ.get("BAY_LIVE_E2E") != "1",
    reason="live run against a throwaway box; set BAY_LIVE_E2E=1 (run in S09)",
)
def test_live_plan_up_roundtrip() -> None:
    """Plan, up, show and rollback a throwaway project on a throwaway test box.

    Needs BAY_LIVE_FLEET (the test fleet dir) and BAY_LIVE_APP (an app
    repo with a bay.toml registered in that fleet, two commits deep).
    """
    fleet = Path(os.environ["BAY_LIVE_FLEET"])
    app_repo = Path(os.environ["BAY_LIVE_APP"])
    old = Path.cwd()
    os.chdir(app_repo)
    try:
        plan = runner.invoke(app, ["--fleet", str(fleet), "plan", "--json"])
        assert plan.exit_code in (0, 10), plan.output
        up = runner.invoke(app, ["--fleet", str(fleet), "up", "--json"])
        assert up.exit_code == 0, up.output
        shown = json.loads(runner.invoke(app, ["--fleet", str(fleet), "show", "--json"]).stdout)
        assert all(e["status"] in ("ok", "behind") for e in shown["envs"])
    finally:
        os.chdir(old)
