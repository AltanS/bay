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
import sys
import tempfile
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
        #: Per deploy: the config files the deploy would copy, relative path -> bytes.
        self.config_files: list[dict[str, bytes] | None] = []

    def deploy(self, cx: Context, box_env: str, *, config_files_root: Path | None = None) -> None:
        from bay_cli import gitrepo

        self.deploys.append(box_env)
        self.config_files.append(
            None
            if config_files_root is None
            else {
                p.relative_to(config_files_root).as_posix(): p.read_bytes()
                for p in sorted(config_files_root.rglob("*"))
                if p.is_file()
            }
        )
        data = yaml.safe_load((cx.fleet_root / GENERATED_SERVICES).read_text().split("\n", 1)[1])
        entries = {**(data.get("accessories") or {}), **(data.get("services") or {})}
        before = {
            c["name"]: c for c in (self.receipts.get(box_env) or {}).get("containers", [])
        }

        def row(name: str, entry: dict[str, Any]) -> dict[str, Any]:
            digest = hashlib.sha256(json.dumps(entry, sort_keys=True).encode()).hexdigest()[:16]
            old = before.get(name)
            action = (
                "create"
                if old is None
                else "noop"
                if (old["config_hash"], old["image"]) == (digest, entry.get("image"))
                else "recreate"
            )
            return {
                "name": name,
                "image": entry.get("image"),
                "config_hash": digest,
                "action": action,
                "healthy": True,
            }

        self.receipts[box_env] = {
            "receipt_version": 1,
            "env": box_env,
            "box": "box-1",
            "deployed_at": "2026-10-06T12:00:00Z",
            "fleet_commit": gitrepo.head(cx.fleet_root),
            "result": "failed" if self.fail else "ok",
            "containers": [row(name, entry) for name, entry in sorted(entries.items())],
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

    # The app's remote is a bare repo; the app checkout is a clone of it. The
    # lock names the remote (repo), never the checkout.
    remote = tmp_path / "remotes" / "webapp.git"
    remote.parent.mkdir()
    git(tmp_path, "init", "-q", "--bare", str(remote))
    app_repo = tmp_path / "webapp"
    git(tmp_path, "clone", "-q", str(remote), str(app_repo))
    (app_repo / "bay.toml").write_text(APP_TOML)
    commit_all(app_repo, "app")
    git(app_repo, "push", "-q", "-u", "origin", "main")
    lockfile.write(
        lockfile.lock_path(fleet, "webapp"),
        lockfile.new_lock("webapp", repo=str(remote)),
    )
    commit_all(fleet, "bay: init webapp")
    return {"fleet": fleet, "app": app_repo, "remote": remote}


def cx_of(world: dict[str, Path]) -> Context:
    return Context.for_fleet_root(world["fleet"])


def project(world: dict[str, Path], *, cwd: Path | None = None) -> planmod.ProjectRef:
    """The webapp project as a command run inside its checkout sees it."""
    return planmod.load_project(cx_of(world), "webapp", cwd=cwd or world["app"])


def make(world: dict[str, Path], **kw: Any) -> dict[str, Any]:
    return planmod.make_plan(project(world), planmod.PlanOptions(**kw))


def do_up(world: dict[str, Path], **kw: Any) -> dict[str, Any]:
    force = kw.pop("force", False)
    reason = kw.pop("reason", None)
    plan_id = kw.pop("plan_id", None)
    return applymod.up(
        project(world), planmod.PlanOptions(**kw), force=force, reason=reason, plan_id=plan_id
    )


def edit_app(
    world: dict[str, Path], old: str, new: str, *, commit: bool = True, push: bool = True
) -> str:
    path = world["app"] / "bay.toml"
    text = path.read_text()
    assert old in text
    path.write_text(text.replace(old, new, 1))
    if not commit:
        return ""
    sha = commit_all(world["app"], f"edit {new[:20]}")
    if push:
        git(world["app"], "push", "-q", "origin", "main")
    return sha


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
    assert msgs[0] == "bay: receipt production (1 projects)"
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
    # `applied` is what the box did, from the receipt. The box had no receipt
    # before, so both containers are new.
    assert doc["applied"] == [
        {"box": "box-1", "container": "postgres", "action": "create", "healthy": True},
        {"box": "box-1", "container": "webapp", "action": "create", "healthy": True},
    ]

    edit_app(world, 'LOG_LEVEL = "info"', 'LOG_LEVEL = "debug"')
    again = json.loads(cli(world, "up", "--json").stdout)
    assert again["applied"] == [
        {"box": "box-1", "container": "webapp", "action": "recreate", "healthy": True}
    ]
    assert [s["container"] for s in again["steps"]] == ["webapp"]


def test_up_json_leaves_out_a_receipt_from_an_earlier_deploy(
    world: dict[str, Path], box: FakeBox, monkeypatch: pytest.MonkeyPatch
) -> None:
    do_up(world)
    edit_app(world, 'LOG_LEVEL = "info"', 'LOG_LEVEL = "debug"')

    def broken(cx: Context, box_env: str, **_: Any) -> None:
        from bay_cli.errors import BayError

        raise BayError("the deploy stopped before the box wrote a receipt")

    monkeypatch.setattr(applymod, "default_deploy", broken)
    result = cli(world, "up", "--json")
    assert result.exit_code == 1
    doc = json.loads(result.stdout)
    assert doc["result"] == "failed" and doc["applied"] == []
    assert any("box-1: the receipt is not from this deploy" in n for n in doc["notes"])


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


def _quiet_check(world: dict[str, Path]) -> tuple[Any, list[Path]]:
    calls: list[Path] = []

    def check(cx: Context, box_env: str, services_file: Path) -> list[dict[str, Any]] | None:
        calls.append(services_file)
        return _box_report(("webapp", "noop", []))

    return check, calls


def test_recheck_keeps_saved_box_check(
    world: dict[str, Path], box: FakeBox, monkeypatch: pytest.MonkeyPatch
) -> None:
    do_up(world)
    check, _ = _quiet_check(world)
    saved = planmod.make_plan(
        project(world), planmod.PlanOptions(box_check=True), check_box=check
    )
    assert saved["box_checked"] is True

    seen: list[planmod.PlanOptions] = []
    real = planmod.make_plan

    def spy(proj: Any, opts: planmod.PlanOptions, **kw: Any) -> dict[str, Any]:
        seen.append(opts)
        return real(proj, opts, **kw)

    monkeypatch.setattr(planmod, "make_plan", spy)
    planmod.recheck(
        project(world), saved, planmod.PlanOptions(box_check=False), check_box=check
    )
    assert [o.box_check for o in seen] == [True]

    unchecked = real(project(world), planmod.PlanOptions())
    planmod.recheck(project(world), unchecked, planmod.PlanOptions(box_check=False))
    assert [o.box_check for o in seen] == [True, False]


def test_stale_reason_names_the_changed_key(world: dict[str, Path], box: FakeBox) -> None:
    do_up(world)
    saved = make(world)
    fresh = make(world)
    fresh["box_checked"] = True
    fresh["plan_id"] = "0" * 12
    reasons = planmod.stale_reasons(saved, fresh)
    assert reasons == ["the plan inputs changed since the plan: box_checked"]
    assert not any("?" in r for r in reasons)

    fresh = dict(saved, plan_id="0" * 12)
    assert planmod.stale_reasons(saved, fresh) == ["plan id differs but no input changed"]


def test_notes_do_not_change_the_plan_id(world: dict[str, Path], box: FakeBox) -> None:
    do_up(world)
    saved = make(world)
    changed = dict(saved, notes=[*saved["notes"], "something to read"])
    assert planmod.body_sha256(changed) == saved["plan_sha256"]


def test_up_with_a_box_checked_plan_id_is_not_stale(world: dict[str, Path], box: FakeBox) -> None:
    do_up(world)
    edit_app(world, 'LOG_LEVEL = "info"', 'LOG_LEVEL = "debug"')
    check, calls = _quiet_check(world)
    saved = planmod.make_plan(
        project(world), planmod.PlanOptions(box_check=True), check_box=check
    )
    assert saved["box_checked"] is True and saved["verdict"] == "auto"
    planmod.save(cx_of(world), saved)
    planmod.approve(cx_of(world), saved["plan_id"], "checked on the box")

    result = applymod.up(
        project(world), planmod.PlanOptions(), plan_id=saved["plan_id"], check_box=check
    )
    assert len(calls) == 2
    assert result["plan_id"] == saved["plan_id"]


def test_project_flag_works_from_anywhere(
    world: dict[str, Path], tmp_path: Path, box: FakeBox
) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    result = cli(world, "plan", "--project", "webapp", "--json", cwd=elsewhere)
    assert result.exit_code == 0, result.output
    # No bay.toml, no --project and no fleet: refused. (With a fleet, a
    # project-less plan covers the whole environment, see
    # test_projectless_plan_covers_whole_env.)
    old = Path.cwd()
    os.chdir(elsewhere)
    try:
        outside = runner.invoke(app, ["plan", "--json"])
    finally:
        os.chdir(old)
    assert outside.exit_code != 0
    assert "no bay.toml" in json.loads(outside.stdout)["error"]  # still one JSON document


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
    assert "local_path" not in raw and raw["lock_version"] == 2
    assert raw["toml_path"] == "bay.toml"
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


# ── coordinator rulings: push, compile at the pin, in-fleet projects, clean JSON ─


def _with_remote(world: dict[str, Path], tmp_path: Path) -> Path:
    remote = tmp_path / "fleet-remote.git"
    git(tmp_path, "init", "-q", "--bare", str(remote))
    git(world["fleet"], "remote", "add", "origin", str(remote))
    git(world["fleet"], "push", "-q", "-u", "origin", "main")
    return remote


def test_up_pushes_the_fleet(world: dict[str, Path], tmp_path: Path, box: FakeBox) -> None:
    remote = _with_remote(world, tmp_path)
    doc = json.loads(cli(world, "up", "--json").stdout)
    assert doc["pushed"] is True and doc["push_error"] is None
    assert git(remote, "rev-parse", "main") == doc["receipt_commit"]


def test_no_push_leaves_the_remote(world: dict[str, Path], tmp_path: Path, box: FakeBox) -> None:
    remote = _with_remote(world, tmp_path)
    before = git(remote, "rev-parse", "main")
    doc = json.loads(cli(world, "up", "--json", "--no-push").stdout)
    assert doc["pushed"] is False
    assert git(remote, "rev-parse", "main") == before


def test_failed_push_is_a_warning(world: dict[str, Path], tmp_path: Path, box: FakeBox) -> None:
    remote = _with_remote(world, tmp_path)
    hook = remote / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\necho refused by test >&2\nexit 1\n")
    hook.chmod(0o755)
    result = cli(world, "up", "--json")
    assert result.exit_code == 0, result.output
    doc = json.loads(result.stdout)
    assert doc["result"] == "ok" and doc["pushed"] is False and doc["push_error"]
    assert lock_of(world)["envs"]["production"]["result"] == "ok"


def test_failed_deploy_is_still_pushed(
    world: dict[str, Path], tmp_path: Path, box: FakeBox
) -> None:
    remote = _with_remote(world, tmp_path)
    box.fail = True
    result = cli(world, "up", "--json")
    assert result.exit_code == 1
    doc = json.loads(result.stdout)
    assert doc["result"] == "failed" and doc["pushed"] is True
    assert git(remote, "rev-parse", "main") == doc["receipt_commit"]


def test_compile_reads_the_pinned_commit(world: dict[str, Path], box: FakeBox) -> None:
    do_up(world)
    edit_app(world, 'LOG_LEVEL = "info"', 'LOG_LEVEL = "debug"')  # committed, not pinned
    pinned = runner.invoke(app, ["compile", "--fleet", str(world["fleet"]), "--check"])
    assert pinned.exit_code == 0, pinned.output
    # --working-tree reads the checkout you stand in as it is.
    live = cli(world, "compile", "--check", "--working-tree")
    assert live.exit_code == 1 and "LOG_LEVEL: debug" in live.output


def test_compile_skips_an_unpinned_project_with_a_note(
    world: dict[str, Path], tmp_path: Path, box: FakeBox
) -> None:
    do_up(world)
    other = tmp_path / "other"
    other.mkdir()
    (other / "bay.toml").write_text(
        APP_TOML.replace("webapp", "other").replace('needs = ["postgres"]\n', "")
    )
    git(other, "init", "-q")
    commit_all(other, "app")
    lockfile.write(
        lockfile.lock_path(world["fleet"], "other"),
        lockfile.new_lock("other", repo=str(other)),
    )
    result = runner.invoke(app, ["compile", "--fleet", str(world["fleet"]), "--check"])
    assert result.exit_code == 0, result.output
    assert "other has no pinned commit yet" in result.stderr


STATUS_TOML = """\
name = "status"
fleet = "testfleet"
image = "ghcr.io/acme/status:1"
port = 8080
health = "none"

[env]
MODE = "one"

[access]
mode = "public"

[deploy.production]
domain = "status.example.com"
"""


def _in_fleet(world: dict[str, Path]) -> Path:
    path = world["fleet"] / "projects" / "status" / "bay.toml"
    path.parent.mkdir()
    path.write_text(STATUS_TOML)
    commit_all(world["fleet"], "add status")
    return path


def _edit_in_fleet(path: Path, world: dict[str, Path], old: str, new: str) -> str:
    path.write_text(path.read_text().replace(old, new))
    return commit_all(world["fleet"], f"status {new}")


def test_in_fleet_project_plan_up_show_rollback(
    world: dict[str, Path], tmp_path: Path, box: FakeBox
) -> None:
    path = _in_fleet(world)
    first = git(world["fleet"], "log", "-1", "--format=%H", "--", "projects/status")
    anywhere = tmp_path / "anywhere"
    anywhere.mkdir()

    def run(*args: str) -> Any:
        if args[0] == "show":
            return cli(world, "show", "status", *args[1:], cwd=anywhere)
        return cli(world, *args, "--project", "status", cwd=anywhere)

    plan = json.loads(run("plan", "--json").stdout)
    assert plan["verdict"] == "auto" and plan["wanted"]["commit"] == first
    jsonschema.validate(plan, PLAN_SCHEMA)
    assert {(s["container"], s["action"]) for s in plan["steps"]} == {("status", "create")}

    up = run("up", "--json")
    assert up.exit_code == 0, up.output
    raw = lockfile.read(lockfile.lock_path(world["fleet"], "status"))
    assert raw is not None
    assert raw["repo"] is None and "local_path" not in raw and raw["commit"] == first
    assert json.loads(run("show", "--json").stdout)["envs"][0]["status"] == "ok"

    path.write_text(path.read_text().replace('MODE = "one"', 'MODE = "two"'))
    shown = json.loads(run("show", "--json").stdout)
    assert shown["wanted"]["dirty"] is True and shown["envs"][0]["status"] == "ok"
    second = commit_all(world["fleet"], "status two")
    assert json.loads(run("show", "--json").stdout)["envs"][0]["status"] == "behind"

    assert run("up", "--json").exit_code == 0
    raw = lockfile.read(lockfile.lock_path(world["fleet"], "status"))
    assert raw is not None and raw["commit"] == second
    assert "MODE: two" in (world["fleet"] / GENERATED_SERVICES).read_text()

    back = run("rollback", "--json")
    assert back.exit_code == 0, back.output
    raw = lockfile.read(lockfile.lock_path(world["fleet"], "status"))
    assert raw is not None and raw["commit"] == first
    assert raw["envs"]["production"]["previous"]["commit"] == second
    assert "MODE: one" in (world["fleet"] / GENERATED_SERVICES).read_text()
    # the fleet file still says two, so WANTED is ahead of the pin again
    assert json.loads(run("show", "--json").stdout)["envs"][0]["status"] == "behind"


def test_in_fleet_project_with_no_lock_is_read_at_fleet_head(
    world: dict[str, Path], box: FakeBox
) -> None:
    do_up(world)
    path = _in_fleet(world)
    path.write_text(path.read_text().replace('MODE = "one"', 'MODE = "uncommitted"'))
    plan = make(world)
    assert any("status lives in the fleet" in n for n in plan["notes"])
    with planmod.compiled_fleet(cx_of(world)) as comp:
        assert comp.result is not None, comp.errors
        assert comp.result.services["status"]["env"]["clear"]["MODE"] == "one"
    # Another project's new container keeps its own risk and names its project.
    assert ("status", "safe", "status") in {
        (s["container"], s["risk"], s["project"]) for s in plan["steps"]
    }


def _noisy_deploy(box: FakeBox) -> Any:
    def deploy(cx: Context, box_env: str, **kw: Any) -> None:
        print("ANSIBLE-PRINT-NOISE")
        subprocess.run(["echo", "ANSIBLE-CHILD-NOISE"], stdout=sys.stdout, check=True)
        box.deploy(cx, box_env, **kw)

    return deploy


def test_json_stdout_is_one_document(
    world: dict[str, Path], box: FakeBox, monkeypatch: pytest.MonkeyPatch
) -> None:
    def deploy(cx: Context, box_env: str, **kw: Any) -> None:
        print("ANSIBLE-PRINT-NOISE")
        box.deploy(cx, box_env, **kw)

    monkeypatch.setattr(applymod, "default_deploy", deploy)
    result = cli(world, "up", "--json")
    assert result.exit_code == 0, result.output
    json.loads(result.stdout)  # nothing else on stdout
    assert "ANSIBLE-PRINT-NOISE" in result.stderr
    for args in (("plan", "--json"), ("show", "--json")):
        json.loads(cli(world, *args).stdout)


def test_log_file_takes_progress_and_child_output(
    world: dict[str, Path], tmp_path: Path, box: FakeBox, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(applymod, "default_deploy", _noisy_deploy(box))
    log = tmp_path / "up.log"
    log.write_text("earlier run\n")
    result = cli(world, "up", "--json", "--log", str(log))
    assert result.exit_code == 0, result.output
    json.loads(result.stdout)
    text = log.read_text()
    assert text.startswith("earlier run\n")  # appended, not replaced
    assert "ANSIBLE-PRINT-NOISE" in text and "ANSIBLE-CHILD-NOISE" in text
    assert "fleet commit" in text
    assert "NOISE" not in result.stdout and "NOISE" not in result.stderr


# ── bay plan --remote: the box prediction becomes steps ─────────────────────


def _box_report(*containers: tuple[str, str, list[str]]) -> list[dict[str, Any]]:
    """What default_box_check returns: one plan-only report per box."""
    return [
        {
            "box": "box-1",
            "error": None,
            "report": {
                "ok": True,
                "plan_only": True,
                "containers": [
                    {"name": n, "action": a, "reasons": r} for n, a, r in containers
                ],
            },
        }
    ]


_MEMORY = "memory: memswap_limit 1g -> 512m"


def _remote(world: dict[str, Path], entries: list[dict[str, Any]] | None) -> dict[str, Any]:
    calls: list[Path] = []

    def check(cx: Context, box_env: str, services_file: Path) -> list[dict[str, Any]] | None:
        calls.append(services_file)
        return entries

    plan = planmod.make_plan(
        project(world), planmod.PlanOptions(box_check=True), check_box=check
    )
    assert len(calls) == 1
    return plan


def test_box_predicts_recreates_the_compile_diff_misses(
    world: dict[str, Path], box: FakeBox
) -> None:
    do_up(world)
    assert [s for s in make(world)["steps"]] == []  # the compile diff is empty
    plan = _remote(
        world,
        _box_report(
            ("webapp", "recreate", ["config_hash: changed (aaa -> bbb)", _MEMORY]),
            ("postgres", "recreate", ["config_hash: changed (ccc -> ddd)", _MEMORY]),
            ("traefik", "noop", []),
        ),
    )
    jsonschema.validate(plan, PLAN_SCHEMA)
    assert plan["box_checked"] is True
    steps = plan["steps"]
    assert [(s["id"], s["container"], s["action"], s["source"], s["risk"]) for s in steps] == [
        ("s1", "postgres", "recreate", "box", "safe"),
        ("s2", "webapp", "recreate", "box", "safe"),
    ]
    assert steps[0]["project"] is None and steps[1]["project"] == "webapp"
    assert "memory:" in steps[1]["reason"] and "box box-1 predicts recreate" in steps[1]["reason"]
    assert plan["verdict"] == "auto"
    assert plan["box_prediction"]["checked"] is True
    assert {c["name"] for c in plan["box_prediction"]["containers"]} == {
        "webapp",
        "postgres",
        "traefik",
    }
    text = planmod.render(plan)
    assert "box prediction: 1 noop, 2 recreate" in text
    assert not BANNED.search(text), BANNED.search(text)


def test_box_remove_is_destructive(world: dict[str, Path], box: FakeBox) -> None:
    do_up(world)
    plan = _remote(world, _box_report(("old-thing", "remove", ["orphan: gone from the list"])))
    assert [(s["action"], s["risk"], s["source"]) for s in plan["steps"]] == [
        ("remove", "destructive", "box")
    ]
    assert plan["verdict"] == "approve"


def test_box_prediction_explained_by_the_compile_diff_adds_no_step(
    world: dict[str, Path], box: FakeBox
) -> None:
    do_up(world)
    edit_app(world, 'LOG_LEVEL = "info"', 'LOG_LEVEL = "debug"')
    plan = _remote(world, _box_report(("webapp", "recreate", ["env: values differ for X"])))
    assert [(s["container"], s["source"]) for s in plan["steps"]] == [("webapp", "compile")]


def test_box_check_without_a_prediction_blocks(world: dict[str, Path], box: FakeBox) -> None:
    do_up(world)
    plan = _remote(world, [])
    assert plan["verdict"] == "blocked"
    assert any("gave no prediction" in b for b in plan["blockers"])
    old = _remote(world, [{"box": "box-1", "error": None, "report": {"ok": True}}])
    assert any("older Bay" in b for b in old["blockers"])


def test_without_remote_the_prediction_is_empty(world: dict[str, Path], box: FakeBox) -> None:
    plan = make(world)
    assert plan["box_prediction"] == {"checked": False, "containers": [], "errors": []}
    assert {s["source"] for s in plan["steps"]} == {"compile"}


def test_default_box_check_reads_the_per_box_files(
    world: dict[str, Path], box: FakeBox, monkeypatch: pytest.MonkeyPatch
) -> None:
    from bay_cli.commands import ops

    seen: list[list[str]] = []

    def fake_run(cx: Context, playbook: str, env: str, tags: Any, extra: list[str]) -> None:
        seen.append(extra)
        assert "--check" in extra and "bay_reconciler_plan_only=true" in extra
        report_dir = next(
            json.loads(a)["bay_reconciler_plan_report_dir"]
            for a in extra
            if a.startswith("{") and "bay_reconciler_plan_report_dir" in a
        )
        report = {"ok": True, "containers": [{"name": "webapp", "action": "noop", "reasons": []}]}
        (Path(report_dir) / "box-1.json").write_text(json.dumps(report))
        (Path(report_dir) / "box-2.json").write_text("{not json")

    monkeypatch.setattr(ops, "_run_playbook", fake_run)
    entries = planmod.default_box_check(cx_of(world), "production", world["fleet"] / "x.yml")
    assert seen
    assert entries is not None
    assert [(e["box"], e["error"] is None) for e in entries] == [("box-1", True), ("box-2", False)]
    assert entries[0]["report"]["containers"][0]["name"] == "webapp"


# ── bay up pushes only a fleet that is its own repo ─────────────────────────


def test_fleet_root_is_toplevel_is_pushed(
    world: dict[str, Path], tmp_path: Path, box: FakeBox
) -> None:
    from bay_cli import gitrepo

    _with_remote(world, tmp_path)
    assert gitrepo.is_toplevel(world["fleet"])
    doc = json.loads(cli(world, "up", "--json").stdout)
    assert doc["pushed"] is True and doc["push_skipped"] is None


def test_fleet_inside_a_larger_repo_is_committed_not_pushed(
    world: dict[str, Path], tmp_path: Path, box: FakeBox
) -> None:
    import shutil

    from bay_cli import gitrepo

    outer = tmp_path / "workspace"
    outer.mkdir()
    fleet = outer / "fleet"
    shutil.move(str(world["fleet"]), str(fleet))
    shutil.rmtree(fleet / ".git")
    (outer / "README").write_text("other work\n")
    git(outer, "init", "-q")
    commit_all(outer, "workspace")
    remote = tmp_path / "workspace-remote.git"
    git(tmp_path, "init", "-q", "--bare", str(remote))
    git(outer, "remote", "add", "origin", str(remote))
    git(outer, "push", "-q", "-u", "origin", "main")
    before = git(remote, "rev-parse", "main")
    moved = {**world, "fleet": fleet}
    assert not gitrepo.is_toplevel(fleet)

    result = cli(moved, "up", "--json")
    assert result.exit_code == 0, result.output
    doc = json.loads(result.stdout)
    assert doc["pushed"] is False and doc["push_error"] is None
    assert "inside a larger repo" in doc["push_skipped"]
    assert "inside a larger repo" in result.stderr
    assert git(outer, "rev-parse", "HEAD") == doc["receipt_commit"]  # committed
    assert git(remote, "rev-parse", "main") == before  # not pushed


# ── --log takes the deploy child's stderr too ───────────────────────────────

_CHILD = (
    "import sys; sys.stderr.write('CHILD-STDERR-LINE\\n'); "
    "sys.stdout.write('CHILD-STDOUT-LINE\\n')"
)


def test_log_file_takes_the_child_stderr(
    world: dict[str, Path], tmp_path: Path, box: FakeBox, monkeypatch: pytest.MonkeyPatch
) -> None:
    from bay_cli import runner as runmod

    def deploy(cx: Context, box_env: str, **kw: Any) -> None:
        # The real path: ansible.run_playbook -> runner.run(capture=False),
        # which hands sys.stdout and sys.stderr to the child.
        runmod.run([sys.executable, "-c", _CHILD], capture=False)
        box.deploy(cx, box_env, **kw)

    monkeypatch.setattr(applymod, "default_deploy", deploy)
    log = tmp_path / "up.log"
    for args in (("up", "--log", str(log)), ("up", "--json", "--log", str(log))):
        result = cli(world, *args)
        assert result.exit_code == 0, result.output
        assert "CHILD" not in result.stdout and "CHILD" not in result.stderr, args
    text = log.read_text()
    assert text.count("CHILD-STDERR-LINE") == 2 and text.count("CHILD-STDOUT-LINE") == 2


def test_log_file_takes_an_inheriting_child(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    from bay_cli.commands.project_cmd import routed_output

    log = tmp_path / "x.log"
    with routed_output(False, log) as say:
        say("progress")
        subprocess.run([sys.executable, "-c", _CHILD], check=True)  # inherits fd 1 and 2
    print("after-stdout")
    print("after-stderr", file=sys.stderr)
    out, err = capfd.readouterr()
    text = log.read_text()
    assert "progress" in text and "CHILD-STDERR-LINE" in text and "CHILD-STDOUT-LINE" in text
    assert "CHILD" not in out + err
    assert "after-stdout" in out and "after-stderr" in err  # both descriptors restored


# ── plan fidelity: the check-mode plan hashes the env files a deploy writes ──

_DEPLOY_STACK = ROOT / "roles" / "deploy_stack" / "tasks"
_RECONCILE_YML = ROOT / "roles" / "container_lifecycle" / "tasks" / "reconcile.yml"
#: The reconcile.yml tasks that read the env files and build the bundle entries.
_ENTRY_TASKS = (
    "Reset the reconcile bundle entries",
    "Read the rendered env files in one pass",
    "Fail with the list of env files that could not be read",
    "Build reconcile bundle entries",
)
#: Live render in deploy_stack/tasks/main.yml -> its scratch twin in env_scratch.yml.
_MIRRORS = {
    "Generate service env files": "Render the service env files into the scratch directory",
    "Generate accessory env files": "Render the accessory env files into the scratch directory",
    "Generate webhook receiver env file": (
        "Render the webhook receiver env file into the scratch directory"
    ),
    "Generate watchtower env file": "Render the watchtower env file into the scratch directory",
}


def _flat(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for task in tasks:
        out.append(task)
        for key in ("block", "rescue", "always"):
            out.extend(_flat(task.get(key) or []))
    return out


def _named(path: Path) -> dict[str, dict[str, Any]]:
    return {t["name"]: t for t in _flat(yaml.safe_load(path.read_text())) if "name" in t}


def test_the_scratch_renders_mirror_the_live_ones_and_never_write_live() -> None:
    live = _named(_DEPLOY_STACK / "main.yml")
    scratch = _named(_DEPLOY_STACK / "env_scratch.yml")
    for live_name, scratch_name in _MIRRORS.items():
        a, b = live[live_name], scratch[scratch_name]
        for key in ("loop", "when", "vars", "loop_control"):
            assert a.get(key) == b.get(key), f"{scratch_name}: {key} drifted from {live_name}"
        ta, tb = a["ansible.builtin.template"], b["ansible.builtin.template"]
        assert ta["src"] == tb["src"]
        assert ta["dest"].startswith("{{ stack_dir }}/env/")
        assert tb["dest"].startswith("{{ _deploy_stack_env_scratch.path }}/env/")
        assert ta["dest"].removeprefix("{{ stack_dir }}") == tb["dest"].removeprefix(
            "{{ _deploy_stack_env_scratch.path }}"
        )
        assert b["check_mode"] is False and b["no_log"] is True and b["diff"] is False
    for task in scratch.values():
        assert task["check_mode"] is False, task["name"]
        assert "stack_dir" not in json.dumps(task), f"{task['name']} names a live path"
    include = live["Render the env files into a scratch directory for the check-mode plan"]
    assert include["when"] == "ansible_check_mode"
    assert include["ansible.builtin.include_tasks"] == "env_scratch.yml"
    cleanup = live["Remove the check-mode env scratch directory"]
    assert cleanup["check_mode"] is False
    assert cleanup["ansible.builtin.file"] == {
        "path": "{{ _deploy_stack_env_scratch.path }}",
        "state": "absent",
    }
    main = yaml.safe_load((_DEPLOY_STACK / "main.yml").read_text())
    stack = next(t for t in main if t.get("name") == "Deploy stack")
    assert cleanup in stack["always"], "the scratch secrets must go on every path"


_LIVE_ENV = "# Ansible managed\n# Environment file for webapp\nLOG_LEVEL=info\nAPP_MODE=web\n"


def _run_check_mode_entries(tmp_path: Path) -> dict[str, Any]:
    """Run the real scratch render and the real entry build with --check on localhost.

    The fake box: ``stack/env/webapp.env`` holds the same two variables as the
    compiled service, in the other order. Returns what the run saw.
    """
    stack = tmp_path / "stack"
    (stack / "env").mkdir(parents=True)
    live = stack / "env" / "webapp.env"
    live.write_text(_LIVE_ENV)
    os.utime(live, (1_000_000_000, 1_000_000_000))
    out = tmp_path / "out"
    out.mkdir()
    spec = {
        "name": "webapp",
        "image": "ghcr.io/acme/webapp:1",
        "type": "service",
        "env_file": str(live),
        "labels": {},
    }
    extra = {
        "stack_dir": str(stack),
        "active_services": {"webapp": {"env": {"clear": {"APP_MODE": "web", "LOG_LEVEL": "info"}}}},
        "active_accessories": {},
        "secrets": {},
        "webhook": {},
        "_reconcile_specs": [spec],
        "out_dir": str(out),
    }
    run = tmp_path / "run"
    run.mkdir()
    (run / "vars.json").write_text(json.dumps(extra))
    (run / "ansible.cfg").write_text("[defaults]\n")
    entries = [_named(_RECONCILE_YML)[n] for n in _ENTRY_TASKS]
    (run / "entries.yml").write_text(yaml.safe_dump(entries, sort_keys=False))
    (run / "play.yml").write_text(
        yaml.safe_dump(
            [
                {
                    "hosts": "localhost",
                    "connection": "local",
                    "gather_facts": False,
                    "tasks": [
                        {
                            "name": "Render the env files into a scratch directory",
                            "ansible.builtin.include_role": {
                                "name": "deploy_stack",
                                "tasks_from": "env_scratch",
                            },
                        },
                        {
                            "name": "Build the entries",
                            "ansible.builtin.include_tasks": str(run / "entries.yml"),
                        },
                        {
                            "name": "Save the entries",
                            "ansible.builtin.copy": {
                                "content": "{{ _reconcile_entries | to_json }}",
                                "dest": "{{ out_dir }}/entries.json",
                            },
                            "check_mode": False,
                        },
                        {
                            "name": "Save the scratch file and its directory name",
                            "ansible.builtin.copy": {
                                "src": "{{ _deploy_stack_env_scratch.path }}/env/webapp.env",
                                "remote_src": True,
                                "dest": "{{ out_dir }}/scratch-webapp.env",
                            },
                            "check_mode": False,
                        },
                        {
                            "name": "Save the scratch directory name",
                            "ansible.builtin.copy": {
                                "content": "{{ _deploy_stack_env_scratch.path }}",
                                "dest": "{{ out_dir }}/scratch-path",
                            },
                            "check_mode": False,
                        },
                        # deploy_stack/tasks/main.yml, always:
                        _named(_DEPLOY_STACK / "main.yml")[
                            "Remove the check-mode env scratch directory"
                        ],
                    ],
                }
            ],
            sort_keys=False,
        )
    )
    env = {
        **os.environ,
        "ANSIBLE_ROLES_PATH": str(ROOT / "roles"),
        "ANSIBLE_FILTER_PLUGINS": str(ROOT / "filter_plugins"),
        "ANSIBLE_CONFIG": str(run / "ansible.cfg"),
        "ANSIBLE_NOCOLOR": "1",
        "ANSIBLE_LOCALHOST_WARNING": "0",
        "ANSIBLE_INVENTORY_UNPARSED_WARNING": "0",
    }
    proc = subprocess.run(
        [
            sys.executable, "-m", "ansible.cli.playbook",
            "-i", "localhost,",
            "--check",
            "-e", f"@{run / 'vars.json'}",
            str(run / "play.yml"),
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert proc.returncode == 0, proc.stdout[-3000:] + proc.stderr[-3000:]
    return {
        "spec": spec,
        "live": live,
        "stack": stack,
        "entries": json.loads((out / "entries.json").read_text()),
        "scratch_bytes": (out / "scratch-webapp.env").read_bytes(),
        "scratch_path": Path((out / "scratch-path").read_text()),
    }


def test_check_mode_plan_sees_an_env_file_that_only_changed_order(
    world: dict[str, Path], tmp_path: Path, box: FakeBox
) -> None:
    sys.path.insert(0, str(ROOT / "filter_plugins"))
    from bay_filters import bay_spec_hash

    from bay_reconcile import ContainerState, load_bundle
    from bay_reconcile.__main__ import reconcile

    seen = _run_check_mode_entries(tmp_path)
    live: Path = seen["live"]

    # Check mode changed nothing live, and the scratch secrets are gone.
    assert live.read_text() == _LIVE_ENV and live.stat().st_mtime == 1_000_000_000
    assert sorted(p.relative_to(seen["stack"]).as_posix() for p in seen["stack"].rglob("*")) == [
        "env",
        "env/webapp.env",
    ]
    assert not seen["scratch_path"].exists()

    # The fake box: same variables, other order.
    new = seen["scratch_bytes"]
    old = live.read_bytes()
    assert new != old
    assert sorted(new.decode().splitlines()) == sorted(old.decode().splitlines())

    (entry,) = seen["entries"]
    assert entry["env_file_change"] == {
        "live": True,
        "added": [],
        "removed": [],
        "changed": [],
        "reordered": True,
    }
    old_hash = bay_spec_hash(seen["spec"], env_digest=hashlib.sha256(old).hexdigest())
    new_hash = bay_spec_hash(seen["spec"], env_digest=hashlib.sha256(new).hexdigest())
    assert entry["config_hash"] == new_hash != old_hash

    # The box runs the container the last deploy made, from the live file.
    class Box:
        def observe(self, managed_label: str) -> dict[str, ContainerState]:
            return {
                "webapp": ContainerState(
                    name="webapp",
                    exists=True,
                    image=entry["image"],
                    config_hash=old_hash,
                    status="running",
                    managed=True,
                    env={"APP_MODE": "web", "LOG_LEVEL": "info"},
                    labels={},
                    volumes=(),
                )
            }

    code, report = reconcile(load_bundle({"containers": [entry]}), Box(), plan_only=True)  # type: ignore[arg-type]
    assert code == 0
    (predicted,) = report["containers"]  # type: ignore[misc]
    assert predicted["action"] == "recreate"
    assert "env_file: the env file bytes differ (same variables, different order)" in (
        predicted["reasons"]
    )

    # bay plan --remote turns it into a step.
    do_up(world)
    plan = _remote(world, [{"box": "box-1", "error": None, "report": report}])
    (step,) = plan["steps"]
    assert (step["container"], step["action"], step["source"]) == ("webapp", "recreate", "box")
    assert "same variables, different order" in step["reason"]
    assert step["reason"].startswith("box box-1 predicts recreate: config_hash: changed")


# ── report directories live outside every working tree ──────────────────────

#: The real one: the ``box`` fixture swaps ``applymod.default_deploy`` for a fake.
_REAL_DEFAULT_DEPLOY = applymod.default_deploy


def _json_var(extra: list[str], key: str) -> Path:
    return Path(next(json.loads(a)[key] for a in extra if a.startswith("{") and key in a))


def test_box_check_reports_go_to_a_temp_dir_never_cwd(
    world: dict[str, Path], tmp_path: Path, box: FakeBox, monkeypatch: pytest.MonkeyPatch
) -> None:
    from bay_cli.commands import ops

    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    seen: dict[str, Path] = {}

    def fake_run(cx: Context, playbook: str, env: str, tags: Any, extra: list[str]) -> None:
        seen["plan"] = _json_var(extra, "bay_reconciler_plan_report_dir")
        seen["real"] = _json_var(extra, "bay_reconciler_report_dir")
        for d in seen.values():
            assert d.is_dir()
        report = {"ok": True, "containers": [{"name": "webapp", "action": "noop", "reasons": []}]}
        (seen["plan"] / "box-1.json").write_text(json.dumps(report))

    monkeypatch.setattr(ops, "_run_playbook", fake_run)
    cx = cx_of(world)
    entries = planmod.default_box_check(cx, "production", world["fleet"] / "x.yml")
    assert entries and entries[0]["report"]["containers"][0]["name"] == "webapp"
    for d in seen.values():
        assert not d.exists(), f"{d} was not cleaned up"
        assert not d.resolve().is_relative_to(cwd.resolve())
        assert not d.resolve().is_relative_to(cx.framework_root.resolve())
    assert list(cwd.iterdir()) == []


def test_up_deploy_reports_go_to_a_temp_dir_never_cwd(
    world: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    from bay_cli.commands import ops, validate

    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    seen: dict[str, Any] = {}

    def fake_run(cx: Context, playbook: str, env: str, tags: Any, extra: list[str]) -> None:
        seen["dir"] = _json_var(extra, "bay_reconciler_report_dir")
        (seen["dir"] / "box-1.json").write_text(json.dumps({"results": []}))

    def fake_health(env: str, root: Path, bay_dir: Path, *, report_dir: Path | None) -> None:
        seen["read"] = report_dir
        seen["files"] = sorted(p.name for p in report_dir.iterdir()) if report_dir else None

    monkeypatch.setattr(
        validate, "run_validation", lambda *a, **k: SimpleNamespace(total_issues=0)
    )
    monkeypatch.setattr(ops, "_run_playbook", fake_run)
    monkeypatch.setattr(ops, "_invalidate_rig_cache", lambda *_: None)
    monkeypatch.setattr(ops, "_run_post_deploy_healthcheck", fake_health)
    cx = cx_of(world)
    _REAL_DEFAULT_DEPLOY(cx, "production")

    assert seen["read"] == seen["dir"] and seen["files"] == ["box-1.json"]
    assert not seen["dir"].exists()
    assert not seen["dir"].resolve().is_relative_to(cwd.resolve())
    assert not seen["dir"].resolve().is_relative_to(cx.framework_root.resolve())
    assert list(cwd.iterdir()) == []


def test_up_and_plan_run_the_same_deploy_tag(
    world: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Plan must equal up: both run ``deploy_stack`` only.

rebuild.sh is rendered under that tag by the git_deploy role (see
tests/test_git_deploy_rebuild_render.py), so up needs no ``git_deploy`` tag,
which would also clone, build and pull.
"""
    from types import SimpleNamespace

    from bay_cli.commands import ops, validate

    seen: dict[str, Any] = {}

    def fake_run(cx: Context, playbook: str, env: str, tags: Any, extra: list[str]) -> None:
        seen["tags"] = tags
        seen["playbook"] = playbook

    monkeypatch.setattr(
        validate, "run_validation", lambda *a, **k: SimpleNamespace(total_issues=0)
    )
    monkeypatch.setattr(ops, "_run_playbook", fake_run)
    monkeypatch.setattr(ops, "_invalidate_rig_cache", lambda *_: None)
    monkeypatch.setattr(ops, "_run_post_deploy_healthcheck", lambda *a, **k: None)
    _REAL_DEFAULT_DEPLOY(cx_of(world), "production")

    assert seen["playbook"] == "deploy"
    assert seen["tags"] == "deploy_stack" == applymod.UP_DEPLOY_TAGS

    plan_tags: list[Any] = []

    def fake_plan_run(cx: Context, playbook: str, env: str, tags: Any, extra: list[str]) -> None:
        plan_tags.append(tags)

    monkeypatch.setattr(ops, "_run_playbook", fake_plan_run)
    planmod.default_box_check(cx_of(world), "production", world["fleet"] / "x.yml")
    assert plan_tags == [seen["tags"]]


def test_the_report_hand_off_honours_the_report_dir_var() -> None:
    tasks = _named(_RECONCILE_YML)
    for name in (
        "Ensure the control-node report directory exists",
        "Hand the reconciler report to the CLI",
    ):
        assert "bay_reconciler_report_dir | default(" in json.dumps(tasks[name]), name
    assert ".reconcile-report/" in (ROOT / ".gitignore").read_text().splitlines()


# ── bay up pins every project it deployed ───────────────────────────────────


def _three_in_fleet(world: dict[str, Path]) -> dict[str, str]:
    """Three projects in the fleet, compiled and committed, none pinned yet.

    The state right after a cutover: the services file holds all three, no
    lock pins a commit. Returns ``{name: last fleet commit of projects/<name>/}``.
    """
    for name in ("alpha", "beta", "status"):
        path = world["fleet"] / "projects" / name / "bay.toml"
        path.parent.mkdir()
        path.write_text(STATUS_TOML.replace("status", name))
        commit_all(world["fleet"], f"add {name}")
    with planmod.compiled_fleet(cx_of(world)) as comp:
        assert comp.result is not None, comp.errors
        (world["fleet"] / GENERATED_SERVICES).write_text(comp.result.text())
    commit_all(world["fleet"], "compile")
    return {
        n: git(world["fleet"], "log", "-1", "--format=%H", "--", f"projects/{n}")
        for n in ("alpha", "beta", "status")
    }


def test_up_pins_every_in_fleet_project_it_deployed(
    world: dict[str, Path], tmp_path: Path, box: FakeBox
) -> None:
    commits = _three_in_fleet(world)
    anywhere = tmp_path / "anywhere"
    anywhere.mkdir()

    up = cli(world, "up", "--json", "--project", "status", cwd=anywhere)
    assert up.exit_code == 0, up.output
    doc = json.loads(up.stdout)
    assert box.deploys == ["production"]
    assert sorted((r["project"], r["env"], r["commit"]) for r in doc["pinned"]) == [
        (n, "production", commits[n]) for n in ("alpha", "beta", "status")
    ]
    assert any(n.startswith("webapp has no pinned commit") for n in doc["notes"])
    assert git(world["fleet"], "log", "-1", "--format=%s") == "bay: receipt production (3 projects)"
    assert git(world["fleet"], "status", "--porcelain") == ""

    for name in ("alpha", "beta", "status"):
        raw = lockfile.read(lockfile.lock_path(world["fleet"], name))
        assert raw is not None, name
        record = raw["envs"]["production"]
        assert raw["commit"] == record["commit"] == commits[name]
        assert record["result"] == "ok" and record["plan_id"] == doc["plan_id"]
        assert record["deployed_at"] and record["last_receipt_sha256"]
        assert "previous" not in record  # no earlier pin
        shown = json.loads(cli(world, "show", name, "--json", cwd=anywhere).stdout)
        assert shown["envs"][0]["status"] == "ok", (name, shown["envs"][0]["reason"])

    # the unpinned repo project stays unpinned
    assert lock_of(world)["commit"] is None and lock_of(world)["envs"] == {}

    # Once pinned, a project in the fleet moves only with its own bay up: a
    # deploy through beta reads alpha at its pin, and alpha keeps it.
    path = world["fleet"] / "projects" / "alpha" / "bay.toml"
    path.write_text(path.read_text().replace('MODE = "one"', 'MODE = "two"'))
    second = commit_all(world["fleet"], "alpha two")
    assert cli(world, "up", "--json", "--project", "beta", cwd=anywhere).exit_code == 0
    raw = lockfile.read(lockfile.lock_path(world["fleet"], "alpha"))
    assert raw is not None and raw["commit"] == commits["alpha"]
    shown = json.loads(cli(world, "show", "alpha", "--json", cwd=anywhere).stdout)
    assert shown["envs"][0]["status"] == "behind"

    assert cli(world, "up", "--json", "--project", "alpha", cwd=anywhere).exit_code == 0
    raw = lockfile.read(lockfile.lock_path(world["fleet"], "alpha"))
    assert raw is not None
    assert raw["commit"] == raw["envs"]["production"]["commit"] == second
    assert raw["envs"]["production"]["previous"]["commit"] == commits["alpha"]
    for name in ("alpha", "beta", "status"):
        shown = json.loads(cli(world, "show", name, "--json", cwd=anywhere).stdout)
        assert shown["envs"][0]["status"] == "ok", (name, shown["envs"][0]["reason"])


def test_a_failed_deploy_marks_every_deployed_project_half(
    world: dict[str, Path], tmp_path: Path, box: FakeBox
) -> None:
    _three_in_fleet(world)
    anywhere = tmp_path / "anywhere"
    anywhere.mkdir()
    box.fail = True
    assert cli(world, "up", "--json", "--project", "status", cwd=anywhere).exit_code == 1
    for name in ("alpha", "beta", "status"):
        raw = lockfile.read(lockfile.lock_path(world["fleet"], name))
        assert raw is not None and raw["envs"]["production"]["result"] == "failed"
        shown = json.loads(cli(world, "show", name, "--json", cwd=anywhere).stdout)
        assert shown["envs"][0]["status"] == "HALF"


def test_a_pinned_repo_project_keeps_its_commit(world: dict[str, Path], box: FakeBox) -> None:
    pinned = git(world["app"], "rev-parse", "HEAD")
    do_up(world)
    edit_app(world, 'LOG_LEVEL = "info"', 'LOG_LEVEL = "debug"')  # committed, not pinned
    _three_in_fleet(world)
    up = applymod.up(planmod.load_project(cx_of(world), "status"), planmod.PlanOptions())
    assert up["result"] == "ok"
    raw = lock_of(world)
    assert raw["commit"] == raw["envs"]["production"]["commit"] == pinned
    assert raw["envs"]["production"]["plan_id"] == up["plan_id"]
    assert ("webapp", "production", pinned) in {
        (r["project"], r["env"], r["commit"]) for r in up["pinned"]
    }


# ── 2.1: lock v2, the repo source, one folder per project ───────────────────


def _repo_project(
    world: dict[str, Path],
    tmp_path: Path,
    name: str,
    files: dict[str, str],
    *,
    toml_path: str = "bay.toml",
    remote_name: str | None = None,
    pin: bool = True,
) -> tuple[Path, str]:
    """A repo project: a bare remote with ``files`` pushed, and its lock in the fleet.

    Returns the remote and the pushed commit. Two calls with one
    ``remote_name`` add a second project to the same repo.
    """
    remote = tmp_path / "remotes" / f"{remote_name or name}.git"
    work = tmp_path / "work" / (remote_name or name)
    if not remote.exists():
        git(tmp_path, "init", "-q", "--bare", str(remote))
        git(tmp_path, "clone", "-q", str(remote), str(work))
    for rel, text in files.items():
        (work / rel).parent.mkdir(parents=True, exist_ok=True)
        (work / rel).write_text(text)
    commit = commit_all(work, f"add {name}")
    git(work, "push", "-q", "origin", "HEAD:main")
    raw = lockfile.new_lock(name, repo=str(remote), toml_path=toml_path)
    if pin:
        raw["commit"] = commit
    lockfile.write(lockfile.lock_path(world["fleet"], name), raw)
    commit_all(world["fleet"], f"bay: init {name}")
    return remote, commit


def _toml(name: str, extra: str = "") -> str:
    return (
        f'name = "{name}"\nfleet = "testfleet"\nimage = "ghcr.io/acme/{name}:1"\nport = 80\n'
        f'health = "none"\n{extra}\n[access]\nmode = "public"\n\n'
        f'[deploy.production]\ndomain = "{name}.example.com"\n'
    )


def test_lock_v2_has_no_local_path(world: dict[str, Path], box: FakeBox) -> None:
    raw = lockfile.new_lock("demo", repo="git@example.com:acme/demo.git")
    assert set(raw) == {"lock_version", "name", "repo", "toml_path", "commit", "envs"}
    assert raw["lock_version"] == 2
    lockfile.write(lockfile.lock_path(world["fleet"], "demo"), raw)
    assert lockfile.lock_path(world["fleet"], "demo") == world["fleet"] / "projects/demo/bay.lock"
    # The schema has no local_path any more: a lock that carries one is refused.
    with pytest.raises(lockfile.LockWriteError, match="local_path"):
        lockfile.write(lockfile.lock_path(world["fleet"], "demo"), {**raw, "local_path": "/x"})
    do_up(world)
    assert "local_path" not in lock_of(world) and lock_of(world)["lock_version"] == 2


def test_lock_v1_migrates_to_v2(world: dict[str, Path], box: FakeBox) -> None:
    path = lockfile.lock_path(world["fleet"], "webapp")
    v1 = {**json.loads(path.read_text()), "lock_version": 1, "local_path": None}
    path.write_text(json.dumps(v1, indent=2) + "\n")
    commit_all(world["fleet"], "a v1 lock")
    raw = lockfile.read(path)
    assert raw is not None and raw["lock_version"] == 2 and "local_path" not in raw
    from bay_cli.fleet import load_lock

    assert load_lock(path, world["fleet"]).name == "webapp"
    # The next write (bay up) stores version 2.
    do_up(world)
    stored = json.loads(path.read_text())
    assert stored["lock_version"] == 2 and "local_path" not in stored


def test_repo_source_prefers_matching_origin_checkout(
    world: dict[str, Path], tmp_path: Path, box: FakeBox
) -> None:
    proj = project(world)
    assert proj.source == "checkout" and proj.checkout == world["app"]
    # WANTED is the checkout's HEAD, even before the push.
    local = edit_app(world, 'LOG_LEVEL = "info"', 'LOG_LEVEL = "warn"', push=False)
    assert make(world)["wanted"]["commit"] == local
    # A checkout of another repo is not used: the cache is.
    stranger = tmp_path / "stranger"
    git(tmp_path, "init", "-q", str(stranger))
    git(stranger, "remote", "add", "origin", "git@example.com:acme/stranger.git")
    other = project(world, cwd=stranger)
    assert other.source == "cache"


def test_repo_source_falls_back_to_bare_cache(
    world: dict[str, Path], tmp_path: Path, box: FakeBox
) -> None:
    from bay_cli import gitrepo, reposource

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    proj = project(world, cwd=elsewhere)
    expected = world["fleet"] / ".bay-cache" / "repos" / reposource.cache_slug(str(world["remote"]))
    assert proj.source == "cache" and proj.checkout == expected
    assert gitrepo.is_bare(expected)
    # The cache never shows in the fleet's git status.
    assert git(world["fleet"], "status", "--porcelain") == ""
    # It is fetched before each plan: a commit pushed after the clone is WANTED.
    pushed = edit_app(world, 'LOG_LEVEL = "info"', 'LOG_LEVEL = "warn"')
    plan = planmod.make_plan(project(world, cwd=elsewhere), planmod.PlanOptions())
    assert plan["wanted"]["commit"] == pushed
    assert plan["blockers"] == []


def test_two_projects_one_repo_share_one_cache(
    world: dict[str, Path], tmp_path: Path, box: FakeBox
) -> None:
    _repo_project(
        world, tmp_path, "alpha", {"apps/alpha/bay.toml": _toml("alpha")},
        toml_path="apps/alpha/bay.toml", remote_name="mono",
    )
    _repo_project(
        world, tmp_path, "beta", {"apps/beta/bay.toml": _toml("beta")},
        toml_path="apps/beta/bay.toml", remote_name="mono",
    )
    with planmod.compiled_fleet(cx_of(world)) as comp:
        assert comp.result is not None, comp.errors
        assert {"alpha", "beta"} <= set(comp.result.services)
    from bay_cli import reposource

    # webapp pins no commit yet, so only alpha and beta were read: one cache.
    caches = sorted(p.name for p in (world["fleet"] / ".bay-cache" / "repos").iterdir())
    mono = str(tmp_path / "remotes" / "mono.git")
    assert caches == [reposource.cache_slug(mono)]
    assert reposource.cache_path(world["fleet"], mono).name == caches[0]


def test_plan_missing_commit_is_an_error_not_a_skip(
    world: dict[str, Path], tmp_path: Path, box: FakeBox
) -> None:
    _repo_project(world, tmp_path, "ghost", {"bay.toml": _toml("ghost")})
    path = lockfile.lock_path(world["fleet"], "ghost")
    raw = json.loads(path.read_text())
    raw["commit"] = "0123456789ab"  # in neither the checkout nor the cache
    path.write_text(json.dumps(raw, indent=2) + "\n")
    commit_all(world["fleet"], "pin a commit nobody pushed")
    plan = make(world)
    assert plan["verdict"] == "blocked"
    assert any(
        b.startswith("ghost: commit 0123456789ab is in neither") and "push it" in b
        for b in plan["blockers"]
    ), plan["blockers"]
    result = runner.invoke(app, ["compile", "--fleet", str(world["fleet"]), "--check"])
    assert result.exit_code != 0 and "ghost: commit 0123456789ab" in result.output


def test_up_refuses_commit_not_on_remote(world: dict[str, Path], box: FakeBox) -> None:
    do_up(world)
    before = lockfile.lock_path(world["fleet"], "webapp").read_text()
    local = edit_app(world, 'LOG_LEVEL = "info"', 'LOG_LEVEL = "warn"', push=False)
    plan = make(world)
    assert plan["wanted"]["commit"] == local
    assert any("push first" in n for n in plan["notes"])  # plan warns
    with pytest.raises(applymod.Refused, match="push first"):
        do_up(world)  # up refuses
    assert lockfile.lock_path(world["fleet"], "webapp").read_text() == before
    git(world["app"], "push", "-q", "origin", "main")
    assert do_up(world)["commit"] == local


def test_init_toml_path_monorepo(world: dict[str, Path], tmp_path: Path, box: FakeBox) -> None:
    repo = tmp_path / "mono"
    git(tmp_path, "init", "-q", str(repo))
    git(repo, "remote", "add", "origin", "git@example.com:acme/mono.git")
    (repo / "services" / "api").mkdir(parents=True)
    (repo / "services" / "api" / "Dockerfile").write_text("FROM x\nEXPOSE 7000\n")
    commit_all(repo, "mono")
    result = cli(
        world, "init", "--name", "api", "--toml-path", "services/api/bay.toml", "--json", cwd=repo
    )
    assert result.exit_code == 0, result.output
    assert (repo / "services" / "api" / "bay.toml").is_file()
    assert not (repo / "bay.toml").exists()
    raw = lockfile.read(lockfile.lock_path(world["fleet"], "api"))
    assert raw is not None and raw["toml_path"] == "services/api/bay.toml"
    assert raw["repo"] == "git@example.com:acme/mono.git"
    assert "port = 7000" in (repo / "services" / "api" / "bay.toml").read_text()
    bad = cli(world, "init", "--name", "bad", "--toml-path", "../x/bay.toml", cwd=repo)
    assert bad.exit_code != 0


def test_init_refuses_a_repo_without_origin(
    world: dict[str, Path], tmp_path: Path, box: FakeBox
) -> None:
    repo = tmp_path / "lonely"
    git(tmp_path, "init", "-q", str(repo))
    (repo / "README").write_text("x\n")
    commit_all(repo, "lonely")
    result = cli(world, "init", cwd=repo)
    assert result.exit_code != 0
    assert not lockfile.lock_path(world["fleet"], "lonely").exists()


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("git@github.com:Acme/App.git", "github.com/acme/app"),
        ("ssh://git@github.com/acme/app", "github.com/acme/app"),
        ("ssh://git@github.com:22/acme/app.git", "github.com/acme/app"),
        ("https://github.com/acme/app.git/", "github.com/acme/app"),
        ("https://user:token@github.com/acme/app", "github.com/acme/app"),
        ("git://example.com/team/tool.git", "example.com/team/tool"),
    ],
)
def test_origin_url_normalization(url: str, expected: str) -> None:
    from bay_cli import reposource

    assert reposource.normalize_url(url) == expected
    assert reposource.same_repo(url, f"https://{expected}.git")


def test_origin_url_normalization_of_a_local_path(tmp_path: Path) -> None:
    from bay_cli import reposource

    path = tmp_path / "remotes" / "app.git"
    assert reposource.normalize_url(str(path)) == str(path.resolve())[: -len(".git")]
    assert reposource.same_repo(str(path), f"file://{path}")
    assert not reposource.same_repo(str(path), "git@github.com:acme/app.git")


def test_cache_slug_is_stable() -> None:
    from bay_cli import reposource

    forms = [
        "git@github.com:acme/app.git",
        "https://github.com/acme/app",
        "ssh://git@github.com/Acme/App.git",
    ]
    slugs = {reposource.cache_slug(u) for u in forms}
    assert len(slugs) == 1
    (slug,) = slugs
    assert slug.startswith("github.com-acme-app-") and len(slug.rsplit("-", 1)[1]) == 8
    # A known value: the slug is the cache's name on every machine.
    assert slug == reposource.cache_slug("git@github.com:acme/app.git")
    assert reposource.cache_slug("git@github.com:acme/app2.git") != slug
    # Two URLs that read the same after the character swap still differ.
    assert reposource.cache_slug("https://x.com/a-b/c") != reposource.cache_slug(
        "https://x.com/a/b-c"
    )


# ── folder per project ──────────────────────────────────────────────────────


def _in_fleet_with_file(world: dict[str, Path], name: str, files: dict[str, str], extra: str) -> Path:
    folder = world["fleet"] / "projects" / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "bay.toml").write_text(_toml(name, extra))
    for rel, text in files.items():
        (folder / rel).parent.mkdir(parents=True, exist_ok=True)
        (folder / rel).write_text(text)
    commit_all(world["fleet"], f"add {name}")
    return folder


MOUNT = '\n[[mounts]]\npath = "/etc/app/{leaf}"\nfrom = "{src}"\n'


def test_from_is_relative_to_toml_dir(
    world: dict[str, Path], tmp_path: Path, box: FakeBox
) -> None:
    # A repo project in a monorepo: from = is relative to apps/api/, not the repo root.
    _repo_project(
        world, tmp_path, "api",
        {
            "apps/api/bay.toml": _toml("api", MOUNT.format(leaf="app.yaml", src="conf/app.yaml")),
            "apps/api/conf/app.yaml": "api: 1\n",
            "conf/app.yaml": "the repo root copy: never read\n",
        },
        toml_path="apps/api/bay.toml",
    )
    # A project in the fleet: from = is relative to projects/<name>/.
    _in_fleet_with_file(
        world, "board", {"conf/board.yaml": "board: 1\n"},
        MOUNT.format(leaf="board.yaml", src="conf/board.yaml"),
    )
    with tempfile.TemporaryDirectory() as tmp:
        made = planmod._materialize(cx_of(world), Path(tmp), {})
        assert made.problems == []
        assert (made.root / "files/api/conf/app.yaml").read_text() == "api: 1\n"
        assert (made.root / "files/board/conf/board.yaml").read_text() == "board: 1\n"
    with planmod.compiled_fleet(cx_of(world)) as comp:
        assert comp.result is not None, comp.errors
        assert comp.result.services["api"]["config_files"] == ["api/conf/app.yaml"]
        assert comp.result.services["board"]["config_files"] == ["board/conf/board.yaml"]
        assert (
            "{{ stack_dir }}/config/api/conf/app.yaml:/etc/app/app.yaml:ro"
            in comp.result.services["api"]["volumes"]
        )


def test_in_fleet_wanted_scope_excludes_lock(world: dict[str, Path], box: FakeBox) -> None:
    _in_fleet(world)
    proj = planmod.load_project(cx_of(world), "status")
    assert proj.scope_spec == ["projects/status", ":(exclude)projects/status/bay.lock"]
    first = planmod.read_wanted(proj, None).commit
    up = applymod.up(proj, planmod.PlanOptions())
    assert up["result"] == "ok"
    # bay up committed projects/status/bay.lock twice; WANTED did not move.
    assert "projects/status/bay.lock" in git(world["fleet"], "log", "-1", "--name-only")
    after = planmod.load_project(cx_of(world), "status")
    assert planmod.read_wanted(after, None).commit == first
    assert planmod.read_wanted(after, None).dirty is False
    shown = applymod.show(after, remote=True)
    assert shown["envs"][0]["status"] == "ok"
    plan = planmod.make_plan(after, planmod.PlanOptions())
    assert plan["steps"] == [] and plan["verdict"] == "auto"


def test_materialize_maps_project_files_into_scratch_files(
    world: dict[str, Path], box: FakeBox
) -> None:
    folder = _in_fleet_with_file(
        world, "board",
        {"rules/a.yaml": "a\n", "rules/b.yaml": "b\n", "site.conf": "conf\n"},
        MOUNT.format(leaf="rules", src="rules") + MOUNT.format(leaf="site.conf", src="site.conf"),
    )
    lock = lockfile.new_lock("board", repo=None)
    # An adopted path keeps today's place under config/ and files/.
    lock["envs"] = {"production": {"adopted": {"files": {"site.conf": "legacy/site.conf"}}}}
    lockfile.write(folder / "bay.lock", lock)
    commit_all(world["fleet"], "board lock")
    with tempfile.TemporaryDirectory() as tmp:
        made = planmod._materialize(cx_of(world), Path(tmp), {})
        assert made.problems == []
        files = made.root / "files"
        assert (files / "board/rules/a.yaml").read_text() == "a\n"
        assert (files / "board/rules/b.yaml").read_text() == "b\n"
        assert (files / "legacy/site.conf").read_text() == "conf\n"
        assert not (files / "board/site.conf").exists()
        # The scratch lock is the pin as it is now.
        assert json.loads((made.root / "projects/board/bay.lock").read_text()) == lock
    with planmod.compiled_fleet(cx_of(world)) as comp:
        assert comp.result is not None, comp.errors
        board = comp.result.services["board"]
        assert board["config_files"] == ["board/rules/a.yaml", "board/rules/b.yaml", "legacy/site.conf"]
        assert "{{ stack_dir }}/config/legacy/site.conf:/etc/app/site.conf:ro" in board["volumes"]
        # The deploy copies config files from this scratch files/.
        assert (comp.files_root / "board/rules/a.yaml").read_text() == "a\n"


def test_materialize_uses_head_not_working_tree(world: dict[str, Path], box: FakeBox) -> None:
    folder = _in_fleet_with_file(
        world, "board", {"site.conf": "committed\n"}, MOUNT.format(leaf="site.conf", src="site.conf")
    )
    (folder / "site.conf").write_text("uncommitted\n")
    (folder / "draft.conf").write_text("never committed\n")
    with tempfile.TemporaryDirectory() as tmp:
        made = planmod._materialize(cx_of(world), Path(tmp), {})
        assert (made.root / "files/board/site.conf").read_text() == "committed\n"
        assert not (made.root / "projects/board/draft.conf").exists()


def test_from_fleet_prefix_resolves_to_fleet_files(world: dict[str, Path], box: FakeBox) -> None:
    shared = world["fleet"] / "files" / "shared" / "rules.yaml"
    shared.parent.mkdir(parents=True)
    shared.write_text("rules: 1\n")
    _in_fleet_with_file(world, "board", {}, MOUNT.format(leaf="rules.yaml", src="fleet:shared/rules.yaml"))
    with planmod.compiled_fleet(cx_of(world)) as comp:
        assert comp.result is not None, comp.errors
        board = comp.result.services["board"]
        assert board["config_files"] == ["shared/rules.yaml"]
        assert "{{ stack_dir }}/config/shared/rules.yaml:/etc/app/rules.yaml:ro" in board["volumes"]
        assert (comp.files_root / "shared/rules.yaml").read_text() == "rules: 1\n"
    shared.unlink()
    commit_all(world["fleet"], "drop the shared file")
    with planmod.compiled_fleet(cx_of(world)) as comp:
        assert comp.result is None
        assert any("from = 'fleet:shared/rules.yaml' cannot be listed" in e for e in comp.errors)


def test_old_files_place_is_read_with_a_deprecation_note(
    world: dict[str, Path], box: FakeBox
) -> None:
    old = world["fleet"] / "files" / "board" / "site.conf"
    old.parent.mkdir(parents=True)
    old.write_text("old place\n")
    _in_fleet_with_file(world, "board", {}, MOUNT.format(leaf="site.conf", src="site.conf"))
    with planmod.compiled_fleet(cx_of(world)) as comp:
        assert comp.result is not None, comp.errors
        assert comp.result.services["board"]["config_files"] == ["board/site.conf"]
        notes = [n for n in comp.notes if "old place" in n]
        assert notes == [
            "board: files/board/site.conf is the old place of from = 'site.conf'; "
            "move it beside the bay.toml (projects/board/site.conf)"
        ]
        assert (comp.files_root / "board/site.conf").read_text() == "old place\n"
    plan = planmod.make_plan(planmod.load_project(cx_of(world), "board"), planmod.PlanOptions())
    assert any("is the old place" in n for n in plan["notes"])
    assert plan["blockers"] == []


def test_deploy_reads_the_scratch_files_of_the_compile(
    world: dict[str, Path], box: FakeBox
) -> None:
    # The file lives only beside the toml; the fleet's own files/ has no copy.
    _in_fleet_with_file(
        world, "board", {"site.conf": "new\n"}, MOUNT.format(leaf="site.conf", src="site.conf")
    )
    assert not (world["fleet"] / "files" / "board").exists()
    plan = planmod.make_plan(planmod.load_project(cx_of(world), "board"), planmod.PlanOptions())
    assert plan["blockers"] == [] and plan["verdict"] == "auto"
    up = applymod.up(planmod.load_project(cx_of(world), "board"), planmod.PlanOptions())
    assert up["result"] == "ok"
    # The deploy got the scratch files/ with files/<name>/<from> at the commit.
    assert box.config_files[-1] == {"board/site.conf": b"new\n"}


def test_uncommitted_file_is_a_note_not_deployed(world: dict[str, Path], box: FakeBox) -> None:
    folder = _in_fleet_with_file(
        world, "board", {"site.conf": "committed\n"}, MOUNT.format(leaf="site.conf", src="site.conf")
    )
    (folder / "site.conf").write_text("edited\n")
    (world["fleet"] / "files" / "extra").mkdir(parents=True)
    (world["fleet"] / "files" / "extra" / "new.yaml").write_text("x\n")
    plan = planmod.make_plan(planmod.load_project(cx_of(world), "board"), planmod.PlanOptions())
    assert plan["blockers"] == []
    assert "uncommitted file projects/board/site.conf is not deployed; commit it first" in plan["notes"]
    assert "uncommitted file files/extra/new.yaml is not deployed; commit it first" in plan["notes"]
    applymod.up(planmod.load_project(cx_of(world), "board"), planmod.PlanOptions())
    assert box.config_files[-1] == {"board/site.conf": b"committed\n"}


def _capture_playbook(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    from types import SimpleNamespace

    from bay_cli.commands import ops, validate

    calls: list[dict[str, Any]] = []

    def fake_run(cx: Context, playbook: str, env: str, tags: Any, extra: list[str], **kw: Any) -> None:
        root = None
        for i, arg in enumerate(extra):
            if arg == "-e" and "bay_config_files_root" in extra[i + 1]:
                root = Path(json.loads(extra[i + 1])["bay_config_files_root"])
        calls.append(
            {
                "tags": tags,
                "extra": list(extra),
                "root": root,
                "files": None
                if root is None
                else sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()),
            }
        )

    monkeypatch.setattr(validate, "run_validation", lambda *a, **k: SimpleNamespace(total_issues=0))
    monkeypatch.setattr(ops, "_run_playbook", fake_run)
    monkeypatch.setattr(ops, "_invalidate_rig_cache", lambda *_: None)
    monkeypatch.setattr(ops, "_run_post_deploy_healthcheck", lambda *a, **k: None)
    return calls


def test_up_and_box_check_pass_bay_config_files_root(
    world: dict[str, Path], box: FakeBox, monkeypatch: pytest.MonkeyPatch
) -> None:
    _in_fleet_with_file(
        world, "board", {"site.conf": "new\n"}, MOUNT.format(leaf="site.conf", src="site.conf")
    )
    calls = _capture_playbook(monkeypatch)
    monkeypatch.setattr(applymod, "default_deploy", _REAL_DEFAULT_DEPLOY)
    monkeypatch.setattr(planmod, "read_box_predictions", lambda _d: [])
    proj = planmod.load_project(cx_of(world), "board")
    planmod.make_plan(proj, planmod.PlanOptions(box_check=True))
    applymod.up(planmod.load_project(cx_of(world), "board"), planmod.PlanOptions())
    check, deploy = calls
    assert "--check" in check["extra"] and "--check" not in deploy["extra"]
    for call in (check, deploy):
        # The scratch files/ existed while the playbook ran, with the mapped file.
        assert call["root"] is not None and call["root"].name == "files"
        assert call["files"] == ["board/site.conf"]
        assert not call["root"].is_relative_to(world["fleet"])
        # It is removed afterwards, as the other scratch dirs are.
        assert not call["root"].exists()


def test_plain_bay_deploy_passes_no_config_files_root(
    world: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _capture_playbook(monkeypatch)
    result = runner.invoke(
        app,
        ["--fleet", str(world["fleet"]), "deploy", "production", "--skip-validate",
         "--skip-healthcheck", "--tags", "deploy_stack"],
    )
    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    assert calls[0]["root"] is None
    assert not any("bay_config_files_root" in a for a in calls[0]["extra"])


def test_config_files_task_reads_the_var_with_the_fleet_default() -> None:
    tasks = yaml.safe_load((ROOT / "roles/deploy_stack/tasks/config_files.yml").read_text())
    copy = next(t for t in tasks if t.get("name") == "Deploy config files")
    assert copy["ansible.builtin.copy"]["src"] == (
        "{{ bay_config_files_root | default(bay_fleet_root ~ '/files') }}/{{ item }}"
    )


def test_fleet_format_2_written_and_unknown_refused(world: dict[str, Path], box: FakeBox) -> None:
    from bay_cli import layout
    from bay_cli.fleet import FleetError, load_fleet_file

    text = (world["fleet"] / "bay.fleet.toml").read_text()
    assert text.splitlines()[:2] == ['name = "testfleet"', "format = 2"]
    assert load_fleet_file(world["fleet"])["format"] == 2
    # A minimal edit: comments and order stay.
    src = '# head\nname = "x"  # the name\ndefault_box = "b"\n\n[boxes.b]\nformat = "kept"\n'
    assert layout.with_format(src) == (
        '# head\nname = "x"  # the name\nformat = 2\ndefault_box = "b"\n\n[boxes.b]\nformat = "kept"\n'
    )
    assert layout.with_format('name = "x"\nformat = 1\n') == 'name = "x"\nformat = 2\n'
    # A format this CLI does not know is refused, before anything is read.
    (world["fleet"] / "bay.fleet.toml").write_text(text.replace("format = 2", "format = 3"))
    with pytest.raises(FleetError, match="format: is 3, but this Bay knows formats up to 2"):
        load_fleet_file(world["fleet"])
    result = cli(world, "plan", "--json")
    assert result.exit_code != 0 and "knows formats up to 2" in result.output


def _flat_fleet(world: dict[str, Path]) -> Path:
    """Put webapp's lock back in the old flat place, as a 2.0 fleet has it."""
    fleet = world["fleet"]
    lock = lockfile.lock_path(fleet, "webapp")
    v1 = {**json.loads(lock.read_text()), "lock_version": 1, "local_path": None}
    git(fleet, "rm", "-q", str(lock.relative_to(fleet)))
    (fleet / "projects" / "webapp.lock").write_text(json.dumps(v1, indent=2) + "\n")
    toml = fleet / "bay.fleet.toml"
    toml.write_text(toml.read_text().replace("format = 2\n", ""))
    commit_all(fleet, "a 2.0 fleet")
    return fleet


def test_lock_moves_into_project_folder(world: dict[str, Path], box: FakeBox) -> None:
    fleet = _flat_fleet(world)
    before = git(fleet, "rev-parse", "HEAD")
    plan = make(world)  # the first command that reads the locks moves them
    assert any(n.startswith("fleet layout: moved projects/webapp.lock") for n in plan["notes"])
    assert not (fleet / "projects" / "webapp.lock").exists()
    assert (fleet / "projects" / "webapp" / "bay.lock").is_file()
    assert "format = 2" in (fleet / "bay.fleet.toml").read_text()
    assert git(fleet, "log", "-1", "--format=%s") == "bay: move locks into project folders"
    assert git(fleet, "rev-parse", "HEAD~1") == before  # one commit
    changed = git(fleet, "show", "--name-status", "--format=", "HEAD").splitlines()
    assert sorted(changed) == sorted(
        ["M\tbay.fleet.toml", "R100\tprojects/webapp.lock\tprojects/webapp/bay.lock"]
    )
    assert git(fleet, "status", "--porcelain") == ""
    # The content is untouched; the next write stores version 2.
    assert json.loads((fleet / "projects/webapp/bay.lock").read_text())["lock_version"] == 1
    do_up(world)
    assert lock_of(world)["lock_version"] == 2
    # A second run has nothing to move.
    assert not any(n.startswith("fleet layout:") for n in make(world)["notes"])


def test_layout_migration_dry_run_changes_nothing(world: dict[str, Path], box: FakeBox) -> None:
    from bay_cli import layout

    fleet = _flat_fleet(world)
    head = git(fleet, "rev-parse", "HEAD")
    dry = layout.migrate(fleet, dry_run=True)
    assert dry.moves == [("projects/webapp.lock", "projects/webapp/bay.lock")]
    assert dry.set_format and dry.commit is None
    assert dry.lines() == [
        "would move projects/webapp.lock to projects/webapp/bay.lock",
        "would set format = 2 in bay.fleet.toml",
    ]
    assert (fleet / "projects" / "webapp.lock").is_file()
    assert git(fleet, "rev-parse", "HEAD") == head
    assert git(fleet, "status", "--porcelain") == ""
    done = layout.migrate(fleet)
    assert done.commit == git(fleet, "rev-parse", "HEAD") != head
    assert not layout.migrate(fleet).changed


def test_layout_refuses_both_lock_forms(world: dict[str, Path], box: FakeBox) -> None:
    from bay_cli import layout
    from bay_cli.errors import BayError

    fleet = world["fleet"]
    flat = fleet / "projects" / "webapp.lock"
    flat.write_text(lockfile.lock_path(fleet, "webapp").read_text())
    commit_all(fleet, "both forms")
    with pytest.raises(BayError, match="webapp"):
        layout.migrate(fleet)
    with pytest.raises(BayError, match="both places"):
        make(world)
    assert flat.is_file()


def test_layout_refuses_a_dirty_fleet_file(world: dict[str, Path], box: FakeBox) -> None:
    from bay_cli import layout
    from bay_cli.errors import BayError

    fleet = _flat_fleet(world)
    toml = fleet / "bay.fleet.toml"
    toml.write_text(toml.read_text() + "# an edit in progress\n")
    with pytest.raises(BayError, match="uncommitted changes"):
        layout.migrate(fleet)
    assert (fleet / "projects" / "webapp.lock").is_file()


def test_doctor_lists_v1_leftovers(tmp_path: Path) -> None:
    from bay_cli.commands.doctor import v1_leftovers

    assert v1_leftovers(tmp_path) == []
    (tmp_path / "bin").mkdir()
    (tmp_path / ".bay-version").write_text("v0.10.0\n")
    assert v1_leftovers(tmp_path) == ["bin", ".bay-version"]


# ── 2.1: box move, whole-environment plan, cross-project risk, plans prune ──

TWO_BOXES = FLEET_TOML.replace(
    '[boxes.box-1]\nenv = "production"\n',
    '[boxes.box-1]\nenv = "production"\ngroup = "one"\n\n'
    '[boxes.box-2]\nenv = "production"\ngroup = "two"\n',
).replace('box = "box-1"\nimage = "postgres:16"', 'box = ["box-1", "box-2"]\nimage = "postgres:16"')

VOLUME_MOUNT = '[[mounts]]\npath = "/data"\nvolume = "data"\nbackup = false\n\n'


def _recompile(world: dict[str, Path], message: str) -> None:
    with planmod.compiled_fleet(cx_of(world)) as comp:
        assert comp.result is not None, comp.errors
        (world["fleet"] / GENERATED_SERVICES).write_text(comp.result.text())
    commit_all(world["fleet"], message)


def _moving_app(world: dict[str, Path], *, volumes: bool = True, database: bool = True) -> None:
    """The webapp runs on box-1 (pinned by a bay up), then bay.toml asks for box-2."""
    (world["fleet"] / "bay.fleet.toml").write_text(TWO_BOXES)
    commit_all(world["fleet"], "two boxes")
    _recompile(world, "compile two boxes")
    if not database:
        edit_app(world, 'needs = ["postgres"]\n', "")
    if not volumes:
        edit_app(world, VOLUME_MOUNT, "")
    do_up(world)
    assert lock_of(world)["envs"]["production"]["box"] == "box-1"
    edit_app(world, "[deploy.production]\n", '[deploy.production]\nbox = "box-2"\n')


def _kinds(plan: dict[str, Any]) -> set[tuple[str, str, str, str | None]]:
    return {(s["kind"], s["action"], s["risk"], s["project"]) for s in plan["steps"]}


def test_box_change_is_a_move_step(world: dict[str, Path], box: FakeBox) -> None:
    _moving_app(world)
    plan = make(world)
    moves = [s for s in plan["steps"] if s["kind"] == "move"]
    assert len(moves) == 1
    assert (moves[0]["action"], moves[0]["project"]) == ("move", "webapp")
    assert moves[0]["resource"] == "box-1 -> box-2"
    assert plan["steps"][0] is moves[0]  # the move leads the plan
    assert (plan["box"], plan["box_env"]) == ("box-2", "production")
    move = plan["moves"][0]
    assert (move["from"], move["to"], move["env"]) == ("box-1", "box-2", "production")
    # The old box loses the container, the new box gets it.
    assert ("container", "remove", "destructive", "webapp") in _kinds(plan)
    assert ("container", "create", "safe", "webapp") in _kinds(plan)
    jsonschema.validate(plan, PLAN_SCHEMA)
    # The compiler alone keeps the pinned box: a stray edit moves nothing.
    with planmod.compiled_fleet(cx_of(world)) as comp:
        assert comp.result is not None, comp.errors
        assert comp.result.services["webapp"]["regions"] == ["one"]


def test_move_with_volumes_is_destructive_and_blocked(world: dict[str, Path], box: FakeBox) -> None:
    _moving_app(world, database=False)
    plan = make(world)
    move = plan["moves"][0]
    assert move["risk"] == "destructive"
    assert (move["volumes"], move["databases"]) == (["webapp-data"], [])
    assert ("move", "move", "destructive", "webapp") in _kinds(plan)
    assert plan["verdict"] == "blocked"
    blocker = next(b for b in plan["blockers"] if "moves from box box-1" in b)
    assert "volumes webapp-data" in blocker and "--data keep" in blocker
    result = cli(world, "plan", "--json")
    assert result.exit_code == 20


def test_move_with_database_is_destructive_and_blocked(
    world: dict[str, Path], box: FakeBox
) -> None:
    _moving_app(world, volumes=False)
    plan = make(world)
    move = plan["moves"][0]
    assert move["risk"] == "destructive" and move["volumes"] == []
    assert move["databases"] == [{"name": "webapp", "resource": "postgres"}]
    assert plan["verdict"] == "blocked"
    blocker = next(b for b in plan["blockers"] if "moves from box box-1" in b)
    assert "database webapp" in blocker and "--data keep" in blocker


def test_move_without_data_is_shared(world: dict[str, Path], box: FakeBox) -> None:
    _moving_app(world, volumes=False, database=False)
    plan = make(world)
    assert plan["moves"][0]["risk"] == "shared"
    assert ("move", "move", "shared", "webapp") in _kinds(plan)
    assert not plan["blockers"]
    assert (plan["verdict"], plan["exit_code"]) == ("approve", 10)


def test_data_keep_unblocks_move_and_data_move_refused(
    world: dict[str, Path], box: FakeBox
) -> None:
    from bay_cli.errors import BayError

    _moving_app(world)
    plan = make(world, data="keep")
    assert not plan["blockers"]
    # The old containers are still removed: approve stays required.
    assert (plan["verdict"], plan["exit_code"]) == ("approve", 10)
    assert plan["moves"][0]["data"] == "keep"
    notes = " ".join(plan["notes"])
    assert "docker volume rm webapp-data" in notes and "DROP DATABASE webapp" in notes

    with pytest.raises(BayError, match="deferred"):
        make(world, data="move")
    for verb in ("plan", "up"):
        refused = cli(world, verb, "--data", "move", "--json")
        assert refused.exit_code != 0
        assert "deferred" in json.loads(refused.stdout)["error"]
    assert box.deploys == ["production"]  # only the first up deployed


def test_up_applies_move_and_writes_lock_box(world: dict[str, Path], box: FakeBox) -> None:
    _moving_app(world)
    first = lock_of(world)["envs"]["production"]["plan_id"]
    planned = json.loads(cli(world, "plan", "--data", "keep", "--json").stdout)
    assert planned["verdict"] == "approve"
    assert cli(world, "approve", planned["plan_id"], "--reason", "test data").exit_code == 0
    done = cli(world, "up", "--data", "keep", "--json")
    assert done.exit_code == 0, done.output
    record = lock_of(world)["envs"]["production"]
    assert record["box"] == "box-2"
    assert record["previous"]["plan_id"] == first
    services = yaml.safe_load(
        (world["fleet"] / GENERATED_SERVICES).read_text().split("\n", 1)[1]
    )
    assert services["services"]["webapp"]["regions"] == ["two"]
    # One box env holds both boxes: one deploy, and its reconciler removes
    # the container that left box-1.
    assert box.deploys == ["production", "production"]
    again = make(world)
    assert "moves" not in again and not any(s["kind"] == "move" for s in again["steps"])


def _two_in_fleet(world: dict[str, Path]) -> None:
    for name in ("alpha", "beta"):
        path = world["fleet"] / "projects" / name / "bay.toml"
        path.parent.mkdir()
        path.write_text(STATUS_TOML.replace("status", name))
    commit_all(world["fleet"], "add alpha and beta")
    _recompile(world, "compile")


def _edit_both(world: dict[str, Path], old: str, new: str) -> None:
    for name in ("alpha", "beta"):
        path = world["fleet"] / "projects" / name / "bay.toml"
        path.write_text(path.read_text().replace(old, new))
    commit_all(world["fleet"], f"alpha and beta: {new.strip()}")


def test_cross_project_step_keeps_own_risk_and_verdict_auto(
    world: dict[str, Path], tmp_path: Path, box: FakeBox
) -> None:
    _two_in_fleet(world)
    _edit_both(world, 'health = "none"\n', 'health = "none"\nmemory = "256m"\n')
    anywhere = tmp_path / "anywhere"
    anywhere.mkdir()
    result = cli(world, "plan", "--project", "alpha", "--json", cwd=anywhere)
    plan = json.loads(result.stdout)
    by_container = {s["container"]: s for s in plan["steps"] if s["kind"] == "container"}
    assert (by_container["alpha"]["risk"], by_container["alpha"]["project"]) == ("safe", "alpha")
    assert (by_container["beta"]["risk"], by_container["beta"]["project"]) == ("safe", "beta")
    assert "mem_limit" in by_container["beta"]["reason"]
    assert (plan["verdict"], result.exit_code) == ("auto", 0)


def test_projectless_plan_covers_whole_env(
    world: dict[str, Path], tmp_path: Path, box: FakeBox
) -> None:
    _two_in_fleet(world)
    anywhere = tmp_path / "anywhere"
    anywhere.mkdir()
    assert cli(world, "up", "--project", "alpha", "--json", cwd=anywhere).exit_code == 0
    _edit_both(world, 'MODE = "one"', 'MODE = "two"')

    result = cli(world, "plan", "--json", cwd=world["fleet"])
    plan = json.loads(result.stdout)
    assert result.exit_code in (0, 10) and result.exit_code == plan["exit_code"]
    assert plan["project"] is None
    assert [p["name"] for p in plan["projects"]] == ["alpha", "beta", "webapp"]
    jsonschema.validate(plan, PLAN_SCHEMA)
    updates = {
        s["project"]: s
        for s in plan["steps"]
        if (s["kind"], s["action"]) == ("container", "update")
    }
    assert set(updates) == {"alpha", "beta"}
    assert all(s["project"] for s in plan["steps"] if s["kind"] != "tailnet")
    # alpha and beta change safely, webapp is new: nothing needs approval.
    assert (plan["verdict"], result.exit_code) == ("auto", 0)

    # Standing in the fleet directory, with no --fleet at all, is the same plan.
    old = Path.cwd()
    os.chdir(world["fleet"])
    try:
        here = runner.invoke(app, ["plan", "--json"])
    finally:
        os.chdir(old)
    assert json.loads(here.stdout)["plan_id"] == plan["plan_id"]

    # bay up <env> with no --project applies that plan id.
    done = cli(world, "up", "--plan-id", plan["plan_id"], "--json", cwd=world["fleet"])
    assert done.exit_code == 0, done.output
    doc = json.loads(done.stdout)
    assert doc["project"] is None and doc["projects"] == ["alpha", "beta", "webapp"]
    assert git(world["fleet"], "log", "--format=%s", "-2").splitlines()[1] == (
        "bay: up production (3 projects)"
    )
    for name in ("alpha", "beta"):
        raw = lockfile.read(lockfile.lock_path(world["fleet"], name))
        assert raw is not None and raw["envs"]["production"]["plan_id"] == plan["plan_id"]
    assert lock_of(world)["commit"] == git(world["app"], "rev-parse", "HEAD")
    after = json.loads(cli(world, "plan", "--json", cwd=world["fleet"]).stdout)
    assert [s for s in after["steps"] if s["kind"] != "tailnet"] == []

    # In an app repo, a project-less plan keeps its one-project meaning.
    assert json.loads(cli(world, "plan", "--json").stdout)["project"] == "webapp"


def _fake_records(world: dict[str, Path], count: int) -> list[str]:
    plans = world["fleet"] / "plans"
    plans.mkdir(exist_ok=True)
    ids = [f"{i:012x}" for i in range(1, count + 1)]
    for i, pid in enumerate(ids):
        stamp = f"2026-01-01T00:{i // 60:02d}:{i % 60:02d}Z"
        (plans / f"{pid}.json").write_text(json.dumps({"plan_id": pid, "created_at": stamp}))
    return ids


def test_plans_prune_keeps_last_50_and_lock_referenced(
    world: dict[str, Path], box: FakeBox
) -> None:
    do_up(world)
    ids = _fake_records(world, 60)
    commit_all(world["fleet"], "old plans")
    raw = lock_of(world)
    record = raw["envs"]["production"]
    real = record["plan_id"]
    record["plan_id"] = ids[1]
    record["previous"] = {"commit": raw["commit"], "plan_id": ids[0]}
    lockfile.write(lockfile.lock_path(world["fleet"], "webapp"), raw)
    commit_all(world["fleet"], "a lock names two old plans")
    untracked = world["fleet"] / "plans" / "0000000000ff.json"
    untracked.write_text(json.dumps({"plan_id": "0000000000ff", "created_at": "2000-01-01"}))

    gone = applymod.prune_plans(cx_of(world))
    # 61 records: the newest 50 are the real one and ids[11:]; the lock keeps ids[0:2].
    assert gone == [f"plans/{pid}.json" for pid in ids[2:11]]
    kept = git(world["fleet"], "ls-files", "plans").splitlines()
    assert f"plans/{real}.json" in kept
    assert {f"plans/{pid}.json" for pid in ids[:2] + ids[11:]} <= set(kept)
    assert len([k for k in kept if k.endswith(".json")]) == 52
    assert git(world["fleet"], "log", "-1", "--format=%s") == "bay: prune plans (9 files)"
    assert untracked.is_file()  # never touched
    status = git(world["fleet"], "status", "--porcelain", "--", "plans")
    assert status == "?? plans/0000000000ff.json"
    assert applymod.prune_plans(cx_of(world)) == []


def test_plans_prune_takes_the_approval_with_its_record(
    world: dict[str, Path], box: FakeBox
) -> None:
    do_up(world)
    ids = _fake_records(world, 55)
    plans = world["fleet"] / "plans"
    for pid in (ids[0], ids[-1]):
        (plans / f"{pid}.approved").write_text(json.dumps({"plan_id": pid}))
    commit_all(world["fleet"], "old plans")
    edit_app(world, 'LOG_LEVEL = "info"', 'LOG_LEVEL = "debug"')
    result = do_up(world)
    # 57 records: the two real ones and ids[7:] are the newest 50.
    assert result["pruned"] == sorted(
        [f"plans/{pid}.json" for pid in ids[:7]] + [f"plans/{ids[0]}.approved"]
    )
    tracked = set(git(world["fleet"], "ls-files", "plans").splitlines())
    assert f"plans/{ids[-1]}.approved" in tracked
    assert f"plans/{ids[0]}.approved" not in tracked
    assert git(world["fleet"], "log", "-1", "--format=%s") == "bay: prune plans (8 files)"
    assert git(world["fleet"], "status", "--porcelain") == ""


# ── M117/05: track mode, hold release, code against config ─────────────────


def _build_app(world: dict[str, Path], *, track: str | None = None) -> str:
    """Turn webapp into a project that builds from source. Returns the app commit."""
    text = (world["app"] / "bay.toml").read_text()
    text = text.replace('image = "ghcr.io/acme/webapp:1"\n', "")
    text += '\n[build]\ndockerfile = "Dockerfile"\n'
    if track:
        text = text.replace("[deploy.production]\n", f'[deploy.production]\ntrack = "{track}"\n')
    (world["app"] / "bay.toml").write_text(text)
    sha = commit_all(world["app"], "build from source")
    git(world["app"], "push", "-q", "origin", "main")
    return sha


def _code_deploys(monkeypatch: pytest.MonkeyPatch, box: FakeBox) -> list[Any]:
    """Swap in a deploy that records the code targets bay up passes to the box."""
    seen: list[Any] = []

    def deploy(
        cx: Context,
        box_env: str,
        *,
        config_files_root: Path | None = None,
        code_targets: Any = None,
    ) -> None:
        seen.append(code_targets)
        box.deploy(cx, box_env, config_files_root=config_files_root)

    monkeypatch.setattr(applymod, "default_deploy", deploy)
    return seen


def _push_code(world: dict[str, Path], text: str, *, push: bool = True) -> str:
    """A code-only commit in the app (bay.toml unchanged). Returns its commit."""
    (world["app"] / "app.txt").write_text(text + "\n")
    sha = commit_all(world["app"], f"code {text}")
    if push:
        git(world["app"], "push", "-q", "origin", "main")
    return sha


def _stamp(box: FakeBox, name: str, commit: str) -> None:
    """What a webhook build does on the box: the receipt names the code it runs."""
    row = box.container(name)
    row["commit"] = commit[:12]
    row["image_ref"] = row["image"]


class _Images:
    """A box's local images for bay_reconcile.codepin: ref -> image id."""

    def __init__(self, ids: dict[str, str]) -> None:
        self.ids = dict(ids)

    def image_id(self, ref: str) -> str | None:
        return self.ids.get(ref)

    def pull(self, ref: str) -> bool:
        return False

    def tag(self, source: str, target: str) -> None:
        self.ids[target] = self.ids[source]

    def commit_tags(self, repo: str) -> list[str]:
        tags = (r.rsplit(":", 1)[1] for r in self.ids if r.startswith(repo + ":"))
        return sorted(t for t in tags if t not in ("latest", "previous"))


def test_up_releases_hold_and_retags_latest(
    world: dict[str, Path], box: FakeBox, monkeypatch: pytest.MonkeyPatch
) -> None:
    from bay_reconcile import codepin, tomlhash

    seen = _code_deploys(monkeypatch, box)
    first = _build_app(world)
    assert do_up(world)["result"] == "ok"
    assert seen[-1] == {"webapp": {"commit": first, "strict": False}}

    def compiled_hash() -> str:
        text = (world["fleet"] / GENERATED_SERVICES).read_text().split("\n", 1)[1]
        return yaml.safe_load(text)["services"]["webapp"]["build"]["bay_toml_hash"]

    assert compiled_hash() == tomlhash.canonical_hash((world["app"] / "bay.toml").read_bytes())

    # A push that changes config: the build side sees another hash and holds.
    held = edit_app(world, 'LOG_LEVEL = "info"', 'LOG_LEVEL = "debug"')
    pushed = tomlhash.canonical_hash(git(world["app"], "show", f"{held}:bay.toml").encode())
    assert pushed != compiled_hash()

    # bay up releases it: pins the commit, compiles its config, asks the box to
    # point :latest at that commit's image.
    up = do_up(world)
    assert up["result"] == "ok" and up["commit"] == held
    assert lock_of(world)["envs"]["production"]["commit"] == held
    assert compiled_hash() == pushed
    assert seen[-1] == {"webapp": {"commit": held, "strict": False}}
    assert up["code_targets"] == seen[-1]

    # The box side of that target: :latest moves to <repo>:<commit12>.
    images = _Images(
        {
            "app/webapp:latest": "sha256:old",
            f"app/webapp:{first[:12]}": "sha256:old",
            f"app/webapp:{held[:12]}": "sha256:new",
        }
    )
    targets = json.dumps(seen[-1])
    spec = json.dumps({"webapp": "app/webapp:latest"})
    assert codepin.main(["--targets", targets, "--images", spec], images=images) == 0
    assert images.ids["app/webapp:latest"] == "sha256:new"
    assert images.ids["app/webapp:previous"] == "sha256:old"

    # track = "pin": the image must be on the box (strict).
    edit_app(world, "[deploy.production]\n", '[deploy.production]\ntrack = "pin"\n')
    pinned = git(world["app"], "rev-parse", "HEAD")
    assert do_up(world)["result"] == "ok"
    assert seen[-1] == {"webapp": {"commit": pinned, "strict": True}}
    services = yaml.safe_load(
        (world["fleet"] / GENERATED_SERVICES).read_text().split("\n", 1)[1]
    )["services"]
    assert services["webapp"]["build"]["track"] == "pin"
    (missing,) = codepin.plan_moves(
        [{"name": "webapp", "image": "app/webapp:latest", "commit": pinned, "strict": True}],
        images,
    )
    assert missing.status == "missing"
    assert missing.available == sorted([first[:12], held[:12]])


def test_plan_prints_code_and_config_commits(
    world: dict[str, Path], box: FakeBox, monkeypatch: pytest.MonkeyPatch
) -> None:
    _code_deploys(monkeypatch, box)
    config = _build_app(world)
    do_up(world)

    # Branch mode: a push deployed new code under the pinned config. Information, not a step.
    code = _push_code(world, "v2")
    _stamp(box, "webapp", code)
    plan = make(world, at=config)
    assert plan["steps"] == [] and plan["verdict"] == "auto"
    line = f"code at {code[:12]}, config pinned at {config[:12]}"
    assert line in plan["notes"]
    assert f"note: {line}" in planmod.render(plan)
    assert plan["code"] == {"keep": ["webapp"]}
    jsonschema.validate(plan, PLAN_SCHEMA)
    _stamp(box, "webapp", config)
    again = make(world, at=config)
    assert "code" not in again
    assert not any(n.startswith("code at") for n in again["notes"])

    # Pin mode: code that is not the pin is a step, kind image, risk safe.
    edit_app(world, "[deploy.production]\n", '[deploy.production]\ntrack = "pin"\n')
    do_up(world)
    pin = lock_of(world)["envs"]["production"]["commit"]
    _stamp(box, "webapp", pin)
    assert make(world)["steps"] == []
    _stamp(box, "webapp", "abcdef0123456789")
    plan = make(world)
    (step,) = plan["steps"]
    assert (step["kind"], step["action"], step["risk"], step["container"]) == (
        "image",
        "update",
        "safe",
        "webapp",
    )
    assert "abcdef012345" in step["reason"] and pin[:12] in step["reason"]
    assert plan["verdict"] == "auto"
    assert not any(n.startswith("code at") for n in plan["notes"])
    jsonschema.validate(plan, PLAN_SCHEMA)


# ── branch mode: bay up moves code only forward ────────────────────────────


def test_branch_mode_up_never_moves_code_backwards(
    world: dict[str, Path], box: FakeBox, monkeypatch: pytest.MonkeyPatch
) -> None:
    from bay_reconcile import tomlhash

    seen = _code_deploys(monkeypatch, box)
    _build_app(world)
    assert do_up(world)["result"] == "ok"

    # A config commit, then a stale checkout made at it.
    config = edit_app(world, 'LOG_LEVEL = "info"', 'LOG_LEVEL = "debug"')
    stale = world["app"].parent / "stale"
    git(world["app"].parent, "clone", "-q", str(world["remote"]), str(stale))
    # A newer code commit that a push built and deployed. The stale checkout
    # has never seen it; the fleet's repo cache has.
    code = _push_code(world, "v2")
    _stamp(box, "webapp", code)

    proj = planmod.load_project(cx_of(world), "webapp", cwd=stale)
    up = applymod.up(proj, planmod.PlanOptions())
    assert up["result"] == "ok" and up["commit"] == config
    # CONFIG at the pin.
    assert lock_of(world)["envs"]["production"]["commit"] == config
    text = (world["fleet"] / GENERATED_SERVICES).read_text().split("\n", 1)[1]
    built = yaml.safe_load(text)["services"]["webapp"]["build"]
    assert built["bay_toml_hash"] == tomlhash.canonical_hash(
        git(stale, "show", f"{config}:bay.toml").encode()
    )
    # CODE stays: no target for webapp, so :latest is not touched.
    assert not seen[-1] and up["code_targets"] == {}
    plan = planmod.load_saved(cx_of(world), up["plan_id"])
    assert f"code at {code[:12]}, config pinned at {config[:12]}" in plan["notes"]
    assert plan["code"] == {"keep": ["webapp"]}
    assert any(n.startswith("webapp runs code newer than") for n in up["notes"])

    # Pin mode is not affected: code follows the pin, backwards included.
    edit_app(world, "[deploy.production]\n", '[deploy.production]\ntrack = "pin"\n')
    pin = git(world["app"], "rev-parse", "HEAD")
    later = _push_code(world, "v3")
    _stamp(box, "webapp", later)
    assert do_up(world, at=pin)["result"] == "ok"
    assert seen[-1] == {"webapp": {"commit": pin, "strict": True}}


def test_branch_mode_up_releases_held_build_forward(
    world: dict[str, Path], box: FakeBox, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _code_deploys(monkeypatch, box)
    first = _build_app(world)
    # The first deploy: no commit on the box yet, so code moves to the pin.
    assert do_up(world)["result"] == "ok"
    assert seen[-1] == {"webapp": {"commit": first, "strict": False}}

    # A push changed config and was held: the box still runs the older commit.
    _stamp(box, "webapp", first)
    held = edit_app(world, 'LOG_LEVEL = "info"', 'LOG_LEVEL = "debug"')
    plan = make(world)
    assert "code" not in plan and not plan["blockers"]
    assert do_up(world)["result"] == "ok"
    assert seen[-1] == {"webapp": {"commit": held, "strict": False}}

    # The same commit runs: bay up still points :latest at the pin.
    _stamp(box, "webapp", held)
    assert do_up(world)["result"] == "ok"
    assert seen[-1] == {"webapp": {"commit": held, "strict": False}}


def test_branch_mode_up_refuses_when_order_unknown_unless_force_code(
    world: dict[str, Path], box: FakeBox, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _code_deploys(monkeypatch, box)
    _build_app(world)
    assert do_up(world)["result"] == "ok"
    config = edit_app(world, 'LOG_LEVEL = "info"', 'LOG_LEVEL = "debug"')
    # The box runs a commit that neither the checkout nor the cache knows.
    _stamp(box, "webapp", "fedcba9876543210")
    deploys = len(seen)

    refused = cli(world, "up", "--json")
    assert refused.exit_code == 20, refused.output
    plan = json.loads(refused.stdout)
    message = f"cannot order {config[:12]} and fedcba987654; fetch the repo or pass --force-code"
    assert message in plan["blockers"]
    assert len(seen) == deploys

    # --force-code: a destructive image step, so it needs approval or --force.
    forced = cli(world, "plan", "--force-code", "--json")
    plan = json.loads(forced.stdout)
    assert forced.exit_code == 10 and plan["verdict"] == "approve"
    (step,) = [s for s in plan["steps"] if s["kind"] == "image"]
    assert (step["action"], step["risk"], step["container"]) == (
        "update",
        "destructive",
        "webapp",
    )
    jsonschema.validate(plan, PLAN_SCHEMA)
    assert cli(world, "up", "--force-code", "--json").exit_code == 10
    assert len(seen) == deploys

    done = cli(world, "up", "--force-code", "--force", "--reason", "rebuilt box", "--json")
    assert done.exit_code == 0, done.output
    assert seen[-1] == {"webapp": {"commit": config, "strict": False}}


# ── bay adopt (M117/06) ─────────────────────────────────────────────────────

SHOP_TOML = """\
name = "shop"
fleet = "testfleet"
image = "ghcr.io/acme/shop:1"
port = 8080
health = "none"

[env]
MODE = "one"

[access]
mode = "public"

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

BUILD_SHOP_TOML = """\
name = "shop"
fleet = "testfleet"
port = 8080
health = "none"

[build]
dockerfile = "Dockerfile"

[access]
mode = "public"

[deploy.production]
domain = "shop.example.com"
"""


def _shop(world: dict[str, Path], tmp_path: Path, *, toml: str = SHOP_TOML) -> dict[str, Path]:
    """An in-fleet project ``shop`` that bay up deployed, and its app repo (not adopted yet).

    Its mounts read a file beside the toml, one in a subdirectory, one in the
    old place files/shop/ and one shared fleet file (``fleet:``). The lock
    keeps an adopted container name, which the adopt must not touch.
    """
    fleet = world["fleet"]
    remote = tmp_path / "remotes" / "shop.git"
    git(tmp_path, "init", "-q", "--bare", str(remote))
    app_repo = tmp_path / "shop"
    git(tmp_path, "clone", "-q", str(remote), str(app_repo))
    (app_repo / "Dockerfile").write_text("FROM scratch\n")
    commit_all(app_repo, "app")
    git(app_repo, "push", "-q", "-u", "origin", "main")

    folder = fleet / "projects" / "shop"
    (folder / "deploy").mkdir(parents=True)
    (folder / "bay.toml").write_text(toml)
    (folder / "site.conf").write_text("site: 1\n")
    (folder / "deploy" / "x.yaml").write_text("x: 1\n")
    (fleet / "files" / "shop").mkdir(parents=True)
    (fleet / "files" / "shop" / "legacy.conf").write_text("legacy: 1\n")
    (fleet / "files" / "shared").mkdir(parents=True)
    (fleet / "files" / "shared" / "rules.yaml").write_text("rules: 1\n")
    lock = lockfile.new_lock("shop", repo=str(remote) if "[build]" in toml else None)
    lock["envs"] = {"production": {"adopted": {"containers": {"web": "old-shop"}}}}
    lockfile.write(folder / "bay.lock", lock)
    commit_all(fleet, "add shop")
    up = applymod.up(planmod.load_project(cx_of(world), "shop"), planmod.PlanOptions())
    assert up["result"] == "ok", up
    return {"app": app_repo, "remote": remote, "folder": folder}


def _adopt(world: dict[str, Path], shop: dict[str, Path], *args: str) -> Any:
    return cli(world, "adopt", "shop", *args, cwd=shop["app"])


def _said(result: Any) -> str:
    try:
        err = result.stderr
    except ValueError:
        err = ""
    return f"{result.output}\n{err}"


def _shop_lock(world: dict[str, Path]) -> dict[str, Any]:
    raw = lockfile.read(lockfile.lock_path(world["fleet"], "shop"))
    assert raw is not None
    return raw


def _body(text: str) -> str:
    from bay_cli import compiler

    return compiler.split_header(text)[1]


def test_adopt_copies_toml_and_files(world: dict[str, Path], tmp_path: Path, box: FakeBox) -> None:
    shop = _shop(world, tmp_path)
    result = _adopt(world, shop)
    assert result.exit_code == 0, _said(result)
    app_repo = shop["app"]
    assert (app_repo / "bay.toml").read_text() == SHOP_TOML
    assert (app_repo / "site.conf").read_text() == "site: 1\n"
    # A subdirectory keeps its place beside the toml.
    assert (app_repo / "deploy" / "x.yaml").read_text() == "x: 1\n"
    # The old place files/shop/<from> moves beside the toml as well.
    assert (app_repo / "legacy.conf").read_text() == "legacy: 1\n"
    # A fleet: mount stays in the fleet.
    assert not (app_repo / "rules.yaml").exists() and not (app_repo / "shared").exists()
    assert (world["fleet"] / "files" / "shared" / "rules.yaml").is_file()
    assert "files/shared/rules.yaml" in git(world["fleet"], "ls-files", "files/shared")


def test_adopt_rewrites_lock_to_repo_form(
    world: dict[str, Path], tmp_path: Path, box: FakeBox
) -> None:
    shop = _shop(world, tmp_path)
    before = _shop_lock(world)
    assert before["repo"] is None and before["envs"]["production"]["commit"]
    assert _adopt(world, shop).exit_code == 0
    raw = _shop_lock(world)
    head = git(shop["app"], "rev-parse", "HEAD")
    assert raw["repo"] == str(shop["remote"])
    assert raw["toml_path"] == "bay.toml"
    assert raw["commit"] == head
    record = raw["envs"]["production"]
    assert record["commit"] == head
    assert "previous" not in record
    # The other adopted names and the deploy record stay as they were.
    assert record["adopted"]["containers"] == {"web": "old-shop"}
    for key in ("box", "deployed_at", "result", "plan_id", "last_receipt_sha256"):
        assert record[key] == before["envs"]["production"][key]
    assert not lockfile.problems(raw)


def test_adopt_records_from_fleet_commit(
    world: dict[str, Path], tmp_path: Path, box: FakeBox
) -> None:
    shop = _shop(world, tmp_path)
    fleet_head = git(world["fleet"], "rev-parse", "HEAD")
    assert _adopt(world, shop).exit_code == 0
    raw = _shop_lock(world)
    assert raw["envs"]["production"]["adopted"]["from_fleet_commit"] == fleet_head


def test_adopt_clears_fleet_folder_except_lock(
    world: dict[str, Path], tmp_path: Path, box: FakeBox
) -> None:
    shop = _shop(world, tmp_path)
    assert _adopt(world, shop).exit_code == 0
    fleet = world["fleet"]
    assert git(fleet, "ls-files", "projects/shop").splitlines() == ["projects/shop/bay.lock"]
    assert sorted(p.name for p in shop["folder"].rglob("*") if p.is_file()) == ["bay.lock"]
    assert git(fleet, "ls-files", "files/shop") == ""
    assert not (fleet / "files" / "shop" / "legacy.conf").exists()
    assert git(fleet, "status", "--porcelain", "--", "projects", "files") == ""


def test_adopt_commits_both_repos_without_push(
    world: dict[str, Path], tmp_path: Path, box: FakeBox
) -> None:
    shop = _shop(world, tmp_path)
    remote_before = git(shop["remote"], "rev-parse", "main")
    assert _adopt(world, shop).exit_code == 0
    app_repo, fleet = shop["app"], world["fleet"]
    assert git(app_repo, "log", "-1", "--format=%s") == (
        "chore: add bay.toml (adopted from fleet testfleet)"
    )
    changed = git(app_repo, "show", "--name-only", "--format=", "HEAD").splitlines()
    assert sorted(changed) == ["bay.toml", "deploy/x.yaml", "legacy.conf", "site.conf"]
    assert git(app_repo, "status", "--porcelain") == ""
    # Never pushed: the remote is where it was, and the commit is ahead of it.
    assert git(shop["remote"], "rev-parse", "main") == remote_before
    assert git(app_repo, "rev-list", "--count", "origin/main..HEAD") == "1"
    assert git(fleet, "log", "-1", "--format=%s") == f"bay: adopt shop into {shop['remote']}"
    fleet_changed = git(fleet, "show", "--name-only", "--format=", "HEAD").splitlines()
    assert sorted(fleet_changed) == [
        "files/shop/legacy.conf",
        "projects/shop/bay.lock",
        "projects/shop/bay.toml",
        "projects/shop/deploy/x.yaml",
        "projects/shop/site.conf",
    ]


def test_adopt_prints_plan_hint(world: dict[str, Path], tmp_path: Path, box: FakeBox) -> None:
    shop = _shop(world, tmp_path)
    result = _adopt(world, shop)
    assert result.exit_code == 0, _said(result)
    said = _said(result)
    assert "now run `bay plan production`" in said
    assert "push it before bay up" in said and "`git push`" in said
    second = _second_in_fleet(world, tmp_path)
    doc = json.loads(cli(world, "adopt", "board", "--json", cwd=second).stdout)
    assert doc["next"] == ["git push", "bay plan production", "bay up production"]


def _second_in_fleet(world: dict[str, Path], tmp_path: Path) -> Path:
    """A second in-fleet project ``board`` with no lock, and its app checkout."""
    base = tmp_path / "second"
    base.mkdir()
    remote = base / "board.git"
    git(base, "init", "-q", "--bare", str(remote))
    app_repo = base / "board"
    git(base, "clone", "-q", str(remote), str(app_repo))
    (app_repo / "README").write_text("board\n")
    commit_all(app_repo, "app")
    git(app_repo, "push", "-q", "-u", "origin", "main")
    folder = world["fleet"] / "projects" / "board"
    folder.mkdir()
    (folder / "bay.toml").write_text(_toml("board"))
    commit_all(world["fleet"], "add board")
    return app_repo


def test_adopt_check_changes_nothing(world: dict[str, Path], tmp_path: Path, box: FakeBox) -> None:
    shop = _shop(world, tmp_path)
    fleet_head = git(world["fleet"], "rev-parse", "HEAD")
    app_head = git(shop["app"], "rev-parse", "HEAD")
    lock_before = lockfile.lock_path(world["fleet"], "shop").read_bytes()
    result = _adopt(world, shop, "--check", "--json")
    assert result.exit_code == 0, _said(result)
    doc = json.loads(result.stdout)
    assert doc["check"] is True and doc["app_commit"] is None and doc["fleet_commit"] is None
    assert {(f["from"], f["to"]) for f in doc["files"]} == {
        ("projects/shop/bay.toml", "bay.toml"),
        ("projects/shop/site.conf", "site.conf"),
        ("projects/shop/deploy/x.yaml", "deploy/x.yaml"),
        ("files/shop/legacy.conf", "legacy.conf"),
    }
    assert any('"from_fleet_commit"' in line for line in doc["lock_diff"])
    assert any("<the adopt commit>" in line for line in doc["lock_diff"])
    assert git(world["fleet"], "rev-parse", "HEAD") == fleet_head
    assert git(shop["app"], "rev-parse", "HEAD") == app_head
    assert lockfile.lock_path(world["fleet"], "shop").read_bytes() == lock_before
    assert git(world["fleet"], "status", "--porcelain") == ""
    assert git(shop["app"], "status", "--porcelain") == ""
    # The text form prints the same list and says nothing changed.
    said = _said(_adopt(world, shop, "--check"))
    assert "would copy files/shop/legacy.conf -> legacy.conf" in said
    assert "nothing changed" in said
    assert git(world["fleet"], "rev-parse", "HEAD") == fleet_head


def test_adopt_refusals_change_nothing(
    world: dict[str, Path], tmp_path: Path, box: FakeBox
) -> None:
    shop = _shop(world, tmp_path)
    fleet, app_repo = world["fleet"], shop["app"]
    heads = [git(fleet, "rev-parse", "HEAD"), git(app_repo, "rev-parse", "HEAD")]

    def refused(text: str, *args: str, name: str = "shop", cwd: Path | None = None) -> None:
        result = cli(world, "adopt", name, *args, cwd=cwd or app_repo)
        assert result.exit_code != 0, _said(result)
        assert text in str(result.exception), str(result.exception)
        assert [git(fleet, "rev-parse", "HEAD"), git(app_repo, "rev-parse", "HEAD")] == heads

    # Not a project of the fleet, or one that already lives in its repo.
    refused("fleet testfleet has no projects/ghost/bay.toml", name="ghost")
    refused("it already lives in its repo", name="webapp")
    # The working directory is no git repo, or has no origin remote.
    plain = tmp_path / "plain"
    plain.mkdir()
    refused("is not a git repo", cwd=plain)
    git(plain, "init", "-q")
    (plain / "README").write_text("x\n")
    commit_all(plain, "x")
    refused("has no origin remote", cwd=plain)
    # A bay.toml is already there.
    (app_repo / "bay.toml").write_text("name = 'x'\n")
    refused("already exists")
    # The app repo has uncommitted changes.
    (app_repo / "bay.toml").unlink()
    (app_repo / "notes.txt").write_text("draft\n")
    refused("has uncommitted changes")
    (app_repo / "notes.txt").unlink()
    # The fleet folder has uncommitted changes.
    site = shop["folder"] / "site.conf"
    site.write_text("site: 2\n")
    refused("projects/shop has uncommitted changes in the fleet")
    site.write_text("site: 1\n")
    # The name in bay.toml is not the folder name.
    toml = shop["folder"] / "bay.toml"
    toml.write_text(SHOP_TOML.replace('name = "shop"', 'name = "store"'))
    heads[0] = commit_all(fleet, "rename by hand")
    refused("name is 'store', not 'shop'")
    # The fleet changed after bay up pinned the project.
    toml.write_text(SHOP_TOML.replace('MODE = "one"', 'MODE = "two"'))
    heads[0] = commit_all(fleet, "undeployed change")
    refused("changed after bay up pinned it")
    # The lock names another repo.
    up = applymod.up(planmod.load_project(cx_of(world), "shop"), planmod.PlanOptions())
    assert up["result"] == "ok"
    raw = _shop_lock(world)
    raw["repo"] = "git@example.com:acme/elsewhere.git"
    lockfile.write(lockfile.lock_path(fleet, "shop"), raw)
    heads[0] = commit_all(fleet, "other repo")
    refused("names repo git@example.com:acme/elsewhere.git, but this checkout's origin is")


def test_adopt_golden_compile_is_byte_identical(
    world: dict[str, Path], tmp_path: Path, box: FakeBox
) -> None:
    shop = _shop(world, tmp_path)
    before = _body((world["fleet"] / GENERATED_SERVICES).read_text())
    assert "{{ stack_dir }}/config/shop/deploy/x.yaml:/etc/shop/x.yaml:ro" in before
    assert _adopt(world, shop).exit_code == 0
    git(shop["app"], "push", "-q", "origin", "main")
    cx = cx_of(world)
    with planmod.compiled_fleet(cx, cwd=shop["app"]) as comp:
        assert comp.result is not None, comp.errors
        assert _body(comp.result.text()) == before
        for rel, text in {
            "shop/site.conf": "site: 1\n",
            "shop/deploy/x.yaml": "x: 1\n",
            "shop/legacy.conf": "legacy: 1\n",
            "shared/rules.yaml": "rules: 1\n",
        }.items():
            assert (comp.files_root / rel).read_text() == text
    # The same from anywhere: the fleet's repo cache serves the pushed commit.
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    with planmod.compiled_fleet(cx, cwd=elsewhere) as comp:
        assert comp.result is not None, comp.errors
        assert _body(comp.result.text()) == before

    proj = planmod.load_project(cx, "shop", cwd=shop["app"])
    assert proj.in_fleet is False and proj.source == "checkout"
    plan = planmod.make_plan(proj, planmod.PlanOptions())
    assert plan["steps"] == [] and plan["verdict"] == "auto", plan
    assert applymod.show(proj, remote=True)["envs"][0]["status"] == "ok"
    up = applymod.up(proj, planmod.PlanOptions())
    assert up["result"] == "ok"
    assert {c["action"] for c in box.receipts["production"]["containers"]} == {"noop"}
    assert _body((world["fleet"] / GENERATED_SERVICES).read_text()) == before


def test_adopt_build_project_plans_zero_steps(
    world: dict[str, Path], tmp_path: Path, box: FakeBox
) -> None:
    from bay_reconcile import tomlhash

    shop = _shop(world, tmp_path, toml=BUILD_SHOP_TOML)
    before = yaml.safe_load(_body((world["fleet"] / GENERATED_SERVICES).read_text()))
    assert "bay_toml_hash" not in before["services"]["old-shop"]["build"]
    assert _adopt(world, shop).exit_code == 0
    git(shop["app"], "push", "-q", "origin", "main")
    proj = planmod.load_project(cx_of(world), "shop", cwd=shop["app"])
    with planmod.compiled_fleet(cx_of(world), cwd=shop["app"]) as comp:
        assert comp.result is not None, comp.errors
        after = yaml.safe_load(_body(comp.result.text()))
    # The toml now lives in the app repo, so the build gains the hold-guard
    # keys. Nothing else changes, and the container hash leaves build out.
    build = after["services"]["old-shop"]["build"]
    assert build.pop("bay_toml_path") == "bay.toml"
    assert build.pop("bay_toml_hash") == tomlhash.canonical_hash(BUILD_SHOP_TOML.encode())
    build.pop("bay_toml_files", None)
    assert after == before
    plan = planmod.make_plan(proj, planmod.PlanOptions())
    assert plan["steps"] == [] and plan["verdict"] == "auto", plan


def test_adopt_toml_path_monorepo(world: dict[str, Path], tmp_path: Path, box: FakeBox) -> None:
    shop = _shop(world, tmp_path)
    before = _body((world["fleet"] / GENERATED_SERVICES).read_text())
    result = _adopt(world, shop, "--toml-path", "services/shop/bay.toml")
    assert result.exit_code == 0, _said(result)
    base = shop["app"] / "services" / "shop"
    assert (base / "bay.toml").read_text() == SHOP_TOML
    assert (base / "deploy" / "x.yaml").read_text() == "x: 1\n"
    assert (base / "legacy.conf").read_text() == "legacy: 1\n"
    assert not (shop["app"] / "bay.toml").exists()
    assert _shop_lock(world)["toml_path"] == "services/shop/bay.toml"
    git(shop["app"], "push", "-q", "origin", "main")
    with planmod.compiled_fleet(cx_of(world), cwd=shop["app"]) as comp:
        assert comp.result is not None, comp.errors
        assert _body(comp.result.text()) == before
    proj = planmod.load_project(cx_of(world), "shop", cwd=shop["app"])
    assert planmod.make_plan(proj, planmod.PlanOptions())["steps"] == []


def test_rollback_after_adopt_refused(
    world: dict[str, Path], tmp_path: Path, box: FakeBox
) -> None:
    shop = _shop(world, tmp_path)
    toml = shop["folder"] / "bay.toml"
    toml.write_text(SHOP_TOML.replace('MODE = "one"', 'MODE = "two"'))
    commit_all(world["fleet"], "shop two")
    up = applymod.up(planmod.load_project(cx_of(world), "shop"), planmod.PlanOptions())
    assert up["result"] == "ok"
    assert _shop_lock(world)["envs"]["production"]["previous"]["commit"]
    assert _adopt(world, shop).exit_code == 0
    git(shop["app"], "push", "-q", "origin", "main")
    fleet_head = git(world["fleet"], "rev-parse", "HEAD")

    result = cli(world, "rollback", cwd=shop["app"])
    assert result.exit_code != 0
    message = str(result.exception)
    assert "the previous pin is a fleet commit; use `bay up --at <commit>`" in message
    assert git(world["fleet"], "rev-parse", "HEAD") == fleet_head
    # Still refused after a bay up to the same commit: there is no earlier app pin.
    assert cli(world, "up", "--json", cwd=shop["app"]).exit_code == 0
    again = cli(world, "rollback", cwd=shop["app"])
    assert "the previous pin is a fleet commit" in str(again.exception)


def test_up_at_commit(world: dict[str, Path], tmp_path: Path, box: FakeBox) -> None:
    path = _in_fleet(world)
    first = git(world["fleet"], "log", "-1", "--format=%H", "--", "projects/status")
    _edit_in_fleet(path, world, 'MODE = "one"', 'MODE = "two"')
    anywhere = tmp_path / "anywhere"
    anywhere.mkdir()
    result = cli(world, "up", "--project", "status", "--at", first[:12], "--json", cwd=anywhere)
    assert result.exit_code == 0, _said(result)
    raw = lockfile.read(lockfile.lock_path(world["fleet"], "status"))
    assert raw is not None
    assert raw["commit"] == first and raw["envs"]["production"]["commit"] == first
    assert "MODE: one" in (world["fleet"] / GENERATED_SERVICES).read_text()
