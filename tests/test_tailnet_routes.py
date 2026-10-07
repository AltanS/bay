"""M117/07: tailnet routes as a fleet table. Plan, up, show, the receipt reader and the verbs.

The compile checks live in tests/test_compile.py and the import in
tests/test_import.py. Here every test builds a throwaway fleet repo (and, for
plan and up, an app repo) under tmp_path, with a fake deploy and a fake box,
as tests/test_plan_up.py does. No network, no SSH, no Ansible.

Names are neutral: laptop, nas, acme, example.com.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tomllib
from pathlib import Path
from typing import Any

import pytest
import yaml
from helpers import make_ansible_env
from typer.testing import CliRunner

from bay_cli import apply as applymod
from bay_cli import lockfile, routes
from bay_cli import plan as planmod
from bay_cli.cli import app
from bay_cli.context import Context
from bay_cli.fleet import GENERATED_SERVICES
from bay_reconcile import routes as box_routes

ROOT = Path(__file__).resolve().parent.parent
TRAEFIK_TEMPLATES = ROOT / "roles" / "traefik" / "templates"
HEADSCALE_TEMPLATES = ROOT / "roles" / "headscale" / "templates"

FLEET_TOML = """\
# The test fleet. This comment must survive every route edit.
name = "testfleet"
default_box = "box-1"
default_domain = "example.com"
primary_env = "production"

[boxes.box-1]
env = "production"
tailnet_ip = "100.64.0.50"

[boxes.stage-1]
env = "staging"

# The tailnet: who may reach vpn services, and where routes terminate.
[tailnet]
ingress_box = "box-1"
cert_domain = "*.ts.example.com"
allowlist = ["100.64.0.0/10"]

# Keep this comment: it belongs to the webhook below.
[webhook]
domain = "deploy.example.com"
secret = "WEBHOOK_SECRET"
"""

APP_TOML = """\
name = "webapp"
fleet = "testfleet"
image = "ghcr.io/acme/webapp:1"
port = 3000

[access]
mode = "public"

[deploy.production]
domain = "webapp.example.com"
"""

NOTES = {"domain": "notes.ts.example.com", "upstream": "http://laptop.acme.tailnet.internal:8080"}

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
    """One receipt per box env; a deploy records its tags and the routes it would render."""

    def __init__(self) -> None:
        self.receipts: dict[str, dict[str, Any]] = {}
        self.tags: list[str | None] = []

    def deploy(
        self,
        cx: Context,
        box_env: str,
        *,
        config_files_root: Path | None = None,
        tags: str | None = None,
    ) -> None:
        from bay_cli import gitrepo

        self.tags.append(tags)
        data = yaml.safe_load((cx.fleet_root / GENERATED_SERVICES).read_text().split("\n", 1)[1])
        entries = {**(data.get("accessories") or {}), **(data.get("services") or {})}
        self.receipts[box_env] = {
            "receipt_version": 1,
            "env": box_env,
            "box": "box-1",
            "deployed_at": "2026-10-07T12:00:00Z",
            "fleet_commit": gitrepo.head(cx.fleet_root),
            "result": "ok",
            "containers": [
                {
                    "name": n,
                    "image": e.get("image"),
                    "config_hash": "h",
                    "action": "create",
                    "healthy": True,
                }
                for n, e in sorted(entries.items())
            ],
            "projects": {},
            # What the receipt module writes: the routes the box renders.
            "routes": [
                {"name": k, **v} for k, v in sorted((data.get("tailnet_proxies") or {}).items())
            ],
        }

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
    for d in ("group_vars/all", "group_vars/production", "hosts", "projects"):
        (fleet / d).mkdir(parents=True)
    (fleet / "bay.fleet.toml").write_text(FLEET_TOML)
    (fleet / "group_vars" / "all" / "main.yml").write_text("---\nstack_name: testfleet\n")
    (fleet / "group_vars" / "production" / "secrets.yml").write_text(
        yaml.safe_dump({"secrets": {"WEBHOOK_SECRET": "x"}})
    )
    (fleet / "hosts" / "production").write_text("[production]\nbox-1\n")
    (fleet / "projects" / ".gitkeep").write_text("")
    git(fleet, "init", "-q")
    commit_all(fleet, "fleet")
    with planmod.compiled_fleet(Context.for_fleet_root(fleet)) as comp:
        assert comp.result is not None, comp.errors
        (fleet / GENERATED_SERVICES).write_text(comp.result.text())
    commit_all(fleet, "compile")

    remote = tmp_path / "remotes" / "webapp.git"
    remote.parent.mkdir()
    git(tmp_path, "init", "-q", "--bare", str(remote))
    app_repo = tmp_path / "webapp"
    git(tmp_path, "clone", "-q", str(remote), str(app_repo))
    (app_repo / "bay.toml").write_text(APP_TOML)
    commit_all(app_repo, "app")
    git(app_repo, "push", "-q", "-u", "origin", "main")
    lockfile.write(
        lockfile.lock_path(fleet, "webapp"), lockfile.new_lock("webapp", repo=str(remote))
    )
    commit_all(fleet, "bay: init webapp")
    return {"fleet": fleet, "app": app_repo}


def cli(world: dict[str, Path], *args: str) -> Any:
    old = Path.cwd()
    os.chdir(world["fleet"])
    try:
        return runner.invoke(app, ["--fleet", str(world["fleet"]), *args])
    finally:
        os.chdir(old)


def make(world: dict[str, Path]) -> dict[str, Any]:
    proj = planmod.load_project(Context.for_fleet_root(world["fleet"]), "webapp", cwd=world["app"])
    return planmod.make_plan(proj, planmod.PlanOptions())


def do_up(world: dict[str, Path]) -> dict[str, Any]:
    proj = planmod.load_project(Context.for_fleet_root(world["fleet"]), "webapp", cwd=world["app"])
    return applymod.up(proj, planmod.PlanOptions(), force=True, reason="test")


def fleet_text(world: dict[str, Path]) -> str:
    return (world["fleet"] / "bay.fleet.toml").read_text()


def add_route(world: dict[str, Path], name: str, *extra: str, **fields: str) -> Any:
    args = ["route", "add", name]
    for key, value in fields.items():
        args += [f"--{key}", value]
    return cli(world, *args, *extra)


# ── route_steps: the diff ───────────────────────────────────────────────────


def _compiled(**proxies: dict[str, Any]) -> dict[str, Any]:
    return {"services": {}, "accessories": {}, "tailnet_proxies": proxies}


def test_route_steps_diff() -> None:
    a = {"domains": ["a.ts.example.com"], "upstream": "http://laptop:1"}
    b = {"domains": ["b.ts.example.com"], "upstream": "http://laptop:2"}
    assert routes.route_steps({}, _compiled(a=a)) == [
        {
            "id": "",
            "kind": "route",
            "container": None,
            "resource": "a",
            "project": None,
            "action": "route_added",
            "risk": "shared",
            "source": "compile",
            "reason": "route a: new, a.ts.example.com to http://laptop:1; "
            "Headscale restarts (new DNS record)",
        }
    ]
    steps = routes.route_steps(
        _compiled(a=a, b=b), _compiled(b={**b, "upstream": "http://laptop:3"})
    )
    assert [(s["resource"], s["action"]) for s in steps] == [
        ("a", "route_removed"),
        ("b", "route_changed"),
    ]
    assert "upstream" in steps[1]["reason"]
    # A default spelled out is no change.
    explicit = {**a, "pass_host_header": True, "identity_inject": False}
    assert routes.route_steps(_compiled(a=a), _compiled(a=explicit)) == []
    assert routes.route_steps(_compiled(a=a), {"services": {}}) != []


def test_plan_says_headscale_restarts_on_dns_change() -> None:
    a = {"domains": ["a.ts.example.com"], "upstream": "http://laptop:1"}
    for new in (
        {**a, "domains": ["a2.ts.example.com"]},
        {**a, "domains": ["a.ts.example.com", "alias.ts.example.com"]},
    ):
        (step,) = routes.route_steps(_compiled(a=a), _compiled(a=new))
        assert step["action"] == "route_changed"
        assert "Headscale restarts" in step["reason"]
    (step,) = routes.route_steps(_compiled(a=a), _compiled(a={**a, "identity_inject": True}))
    assert "Headscale restarts" not in step["reason"]
    assert "Traefik reloads the route file" in step["reason"]
    (removed,) = routes.route_steps(_compiled(a=a), _compiled())
    assert "Headscale restarts" in removed["reason"]


def test_deploy_tags() -> None:
    assert routes.deploy_tags([], "deploy_stack") == "deploy_stack"
    assert routes.deploy_tags([{"kind": "container"}], "deploy_stack") == "deploy_stack"
    assert (
        routes.deploy_tags([{"kind": "route"}], "deploy_stack") == "deploy_stack,headscale,traefik"
    )


# ── plan and up ─────────────────────────────────────────────────────────────


def test_plan_route_steps_and_risk(world: dict[str, Path], box: FakeBox) -> None:
    assert add_route(world, "notes", **NOTES).exit_code == 0
    plan = make(world)
    route = [s for s in plan["steps"] if s["kind"] == "route"]
    assert [(s["resource"], s["action"], s["risk"]) for s in route] == [
        ("notes", "route_added", "shared")
    ]
    assert "Headscale restarts" in route[0]["reason"]
    assert plan["verdict"] == "approve"
    # The allowlist did not change, so there is no tailnet step for the route.
    assert not [s for s in plan["steps"] if s["kind"] == "tailnet"]
    # The plan document still matches its schema.
    import jsonschema

    schema = json.loads((ROOT / "src/bay_cli/schemas/plan.schema.json").read_text())
    jsonschema.validate(plan, schema)
    # The text form names the route and uses only plain words.
    text = planmod.render(plan)
    assert "route notes" in text
    banned = re.compile(r"\b(rigs?|regions?|roles?|reconcilers?|hosts?|playbooks?)\b", re.I)
    assert not banned.search(" ".join(s["reason"] for s in route))


def test_plan_route_change_off_the_ingress_env_blocks() -> None:
    fleet = {
        "boxes": {"box-1": {"env": "production"}, "stage-1": {"env": "staging"}},
        "tailnet": {"ingress_box": "box-1"},
    }
    new = _compiled(a={"domains": ["a.ts.example.com"], "upstream": "http://laptop:1"})
    steps, blockers = routes.plan_routes({}, new, fleet, "staging")
    assert len(steps) == 1
    assert blockers and "ingress box box-1 (env production)" in blockers[0]
    assert routes.plan_routes({}, new, fleet, "production")[1] == []


def test_up_runs_headscale_and_traefik_tags_on_route_change(
    world: dict[str, Path], box: FakeBox
) -> None:
    do_up(world)
    assert box.tags == [None], "no route step: the default tags, no tags argument"
    assert add_route(world, "notes", **NOTES).exit_code == 0
    result = do_up(world)
    assert box.tags[-1] == "deploy_stack,headscale,traefik"
    assert [s["action"] for s in result["steps"] if s["kind"] == "route"] == ["route_added"]
    # The compiled file holds the route; the next plan has no route step.
    data = yaml.safe_load((world["fleet"] / GENERATED_SERVICES).read_text().split("\n", 1)[1])
    assert data["tailnet_proxies"]["notes"]["upstream"] == NOTES["upstream"]
    assert not [s for s in make(world)["steps"] if s["kind"] == "route"]
    do_up(world)
    assert box.tags[-1] is None


# ── route-only plan and up (M118/05, gap 11) ────────────────────────────────


@pytest.fixture
def gw_world(world: dict[str, Path]) -> dict[str, Path]:
    """The world with the ingress on box gw, box env infra, where no project deploys."""
    fleet = world["fleet"]
    text = fleet_text(world).replace(
        '[boxes.stage-1]\nenv = "staging"\n',
        '[boxes.stage-1]\nenv = "staging"\n\n[boxes.gw]\nenv = "infra"\n',
    ).replace('ingress_box = "box-1"', 'ingress_box = "gw"')
    (fleet / "bay.fleet.toml").write_text(text)
    (fleet / "hosts" / "infra").write_text("[infra]\ngw\n")
    commit_all(fleet, "gw box")
    return world


def _env_plan(world: dict[str, Path], env: str) -> dict[str, Any]:
    cx = Context.for_fleet_root(world["fleet"])
    return planmod.make_env_plan(cx, planmod.PlanOptions(env=env), cwd=world["fleet"])


def test_plan_route_only_env(gw_world: dict[str, Path], box: FakeBox) -> None:
    import jsonschema

    assert add_route(gw_world, "notes", **NOTES).exit_code == 0
    plan = _env_plan(gw_world, "infra")
    assert plan["projects"] == []
    assert (plan["box_env"], plan["box"]) == ("infra", "gw")
    assert "route-only plan for infra" in plan["notes"]
    assert plan["blockers"] == []
    assert [(s["kind"], s["resource"], s["action"], s["risk"]) for s in plan["steps"]] == [
        ("route", "notes", "route_added", "shared")
    ]
    assert plan["verdict"] == "approve"
    schema = json.loads((ROOT / "src/bay_cli/schemas/plan.schema.json").read_text())
    jsonschema.validate(plan, schema)
    assert "routes only, no project" in planmod.render(plan)

    # The CLI: bay plan infra in the fleet, no --project.
    shown = cli(gw_world, "plan", "infra", "--json")
    assert shown.exit_code == 10, shown.output
    assert json.loads(shown.stdout)["plan_id"] == plan["plan_id"]

    # Another env with no project: today's note, no compile, no step.
    other = _env_plan(gw_world, "staging")
    assert other["steps"] == [] and other["box_env"] is None
    assert "no project of fleet testfleet has [deploy.staging]" in other["notes"]
    assert not any("route-only" in n for n in other["notes"])

    # A route-only up pins nothing, so a project change in the compile blocks.
    ctx = Context.for_fleet_root(gw_world["fleet"])
    data = yaml.safe_load((gw_world["fleet"] / GENERATED_SERVICES).read_text().split("\n", 1)[1])
    proj = planmod.load_project(ctx, "webapp", cwd=gw_world["app"])
    lock = dict(proj.lock)
    lock["commit"] = git(gw_world["app"], "rev-parse", "HEAD")
    lockfile.write(proj.lock_file, lock)
    commit_all(gw_world["fleet"], "pin webapp by hand")
    assert "webapp" not in (data.get("services") or {})
    blocked = _env_plan(gw_world, "infra")
    assert blocked["verdict"] == "blocked"
    assert any(
        "route-only plan for infra: the compile also changes project(s) webapp" in b
        for b in blocked["blockers"]
    ), blocked["blockers"]


def test_up_route_only_env(gw_world: dict[str, Path], box: FakeBox) -> None:
    fleet = gw_world["fleet"]
    lock_path = lockfile.lock_path(fleet, "webapp")
    lock_before = lock_path.read_bytes()
    assert add_route(gw_world, "notes", **NOTES).exit_code == 0
    cx = Context.for_fleet_root(fleet)

    # Approve, then up by plan id, as for any plan (route steps are shared).
    planned = cli(gw_world, "plan", "infra", "--json")
    plan_id = json.loads(planned.stdout)["plan_id"]
    refused = cli(gw_world, "up", "infra", "--plan-id", plan_id, "--json")
    assert refused.exit_code == 10, refused.output
    assert box.tags == []
    approved = cli(gw_world, "approve", plan_id, "--reason", "new route")
    assert approved.exit_code == 0, approved.output
    up = cli(gw_world, "up", "infra", "--plan-id", plan_id, "--json")
    assert up.exit_code == 0, up.output
    result = json.loads(up.stdout)
    assert result["route_only"] is True
    assert result["projects"] == [] and result["pinned"] == []
    assert box.tags == ["deploy_stack,headscale,traefik"]
    assert git(fleet, "log", "-1", "--format=%s") == "bay: up infra (routes)"
    assert git(fleet, "status", "--porcelain") == ""
    # The services file holds the route; no lock moved.
    data = yaml.safe_load((fleet / GENERATED_SERVICES).read_text().split("\n", 1)[1])
    assert data["tailnet_proxies"]["notes"]["upstream"] == NOTES["upstream"]
    assert lock_path.read_bytes() == lock_before
    assert [r["name"] for r in box.receipts["infra"]["routes"]] == ["notes"]
    # The receipt is from this deploy: its fleet commit is the up commit.
    assert box.receipts["infra"]["fleet_commit"] == result["fleet_commit"]

    # Zero steps: the plan is auto and the up still deploys (receipt rewrite).
    again = planmod.make_env_plan(cx, planmod.PlanOptions(env="infra"), cwd=fleet)
    assert again["steps"] == [] and again["verdict"] == "auto"
    result = applymod.up_env(cx, planmod.PlanOptions(env="infra"), cwd=fleet)
    assert box.tags[-1] is None, "no route step: the default tags"
    assert len(box.tags) == 2
    assert result["route_only"] is True and result["steps"] == []
    assert lock_path.read_bytes() == lock_before

    # Another env with no project still refuses.
    with pytest.raises(Exception, match=r"no project has \[deploy.staging\]"):
        applymod.up_env(cx, planmod.PlanOptions(env="staging"), cwd=fleet)


# ── RUNNING: the receipt reader and bay show --routes ───────────────────────


def _regex_replace(value: str, pattern: str, replacement: str) -> str:
    return re.sub(pattern, replacement, value)


def render_traefik(proxies: dict[str, Any], **extra: Any) -> str:
    env = make_ansible_env(TRAEFIK_TEMPLATES)
    env.filters["regex_replace"] = _regex_replace
    ctx = {
        "ansible_managed": "test",
        "tailnet_proxies": proxies,
        "traefik_dns_resolver_name": "letsencrypt_dns",
        "tailnet_ingress_cert_domain": "*.ts.example.com",
        "traefik_split_entrypoints": True,
        "tailnet_identity_enabled": True,
        "tailnet_identity_port": 9200,
        "tailnet_identity_device_header": "X-Tailnet-Device",
        "tailnet_identity_device_id_header": "X-Tailnet-Device-Id",
        **extra,
    }
    return env.get_template("dynamic/tailnet-proxies.yml.j2").render(**ctx)


def render_headscale(name: str, proxies: dict[str, Any]) -> str:
    env = make_ansible_env(HEADSCALE_TEMPLATES)
    ctx = {
        "ansible_managed": "test",
        "headscale_domain": "hs.example.com",
        "headscale_tailnet_cidr": ["100.64.0.0/10"],
        "headscale_magic_dns_domain": "acme.tailnet.internal",
        "headscale_server_tailnet_ip": "100.64.0.50",
        "groups": {"all": ["ingress"]},
        "inventory_hostname": "ingress",
        "hostvars": {"ingress": {"region": "", "services": {}}},
        "tailnet_proxies": proxies,
        "headscale_extra_dns_records": [],
    }
    return env.get_template(name).render(**ctx)


OLD_MAP = {
    "notes": {
        "domains": ["notes.ts.example.com", "memo.ts.example.com"],
        "upstream": "http://laptop.acme.tailnet.internal:8080",
        "pass_host_header": False,
        "identity_inject": True,
    },
    "notes-next": {
        "domains": ["notes-next.ts.example.com"],
        "upstream": "http://laptop.acme.tailnet.internal:8081",
        "pass_host_header": True,
        "identity_inject": True,
    },
    "nas": {"domains": ["nas.ts.example.com"], "upstream": "http://100.64.0.9:5000"},
}


def test_compiled_routes_render_like_the_old_map() -> None:
    """Old YAML map -> table -> compiled map: the box renders the same bytes."""
    table, problems = routes.from_proxies(OLD_MAP)
    assert problems == []
    fleet = {
        "boxes": {"box-1": {"env": "production"}},
        "tailnet": {"ingress_box": "box-1", "cert_domain": "*.ts.example.com", "routes": table},
    }
    compiled, errors = routes.compile_routes(fleet)
    assert errors == []
    assert render_traefik(compiled) == render_traefik(OLD_MAP)
    for name in ("extra-records.json.j2", "config.yaml.j2"):
        assert render_headscale(name, compiled) == render_headscale(name, OLD_MAP), name


def test_receipt_lists_rendered_routes(tmp_path: Path) -> None:
    rendered = render_traefik(OLD_MAP)
    stack = tmp_path / "stack"
    (stack / "dynamic").mkdir(parents=True)
    (stack / "dynamic" / "tailnet-proxies.yml").write_text(rendered)
    listed = box_routes.rendered_routes(stack)
    assert [r["name"] for r in listed] == ["nas", "notes", "notes-next"]
    by_name = {r["name"]: r for r in listed}
    assert by_name["notes"] == {
        "name": "notes",
        "domains": ["notes.ts.example.com", "memo.ts.example.com"],
        "upstream": "http://laptop.acme.tailnet.internal:8080",
        "pass_host_header": False,
        "identity_inject": True,
        "entrypoint": "websecure_tailnet",
    }
    assert by_name["nas"]["pass_host_header"] is True
    assert by_name["nas"]["identity_inject"] is False
    # Every route the box lists equals the compiled entry, defaults spelled out.
    for name, entry in OLD_MAP.items():
        got = {k: v for k, v in by_name[name].items() if k not in ("name", "entrypoint")}
        want = routes._norm(entry)
        assert want is not None
        assert got == {k: v for k, v in want.items() if k != "entrypoint"}
    # No route file: no routes.
    assert box_routes.rendered_routes(tmp_path / "none") == []
    # The receipt list is what bay show --routes reads as RUNNING.
    fleet = {
        "boxes": {"box-1": {"env": "production"}},
        "tailnet": {
            "ingress_box": "box-1",
            "cert_domain": "*.ts.example.com",
            "routes": routes.from_proxies(OLD_MAP)[0],
        },
    }
    entries = [{"receipt": {"routes": listed}}, {"receipt": {"routes": []}}]
    doc = routes.show_routes(fleet, {"tailnet_proxies": OLD_MAP}, entries)
    # The rendered entrypoint is the role's default, which a pinned entry
    # without an entrypoint means: no drift.
    assert {r["name"]: r["status"] for r in doc["routes"]} == {
        "nas": "ok",
        "notes": "ok",
        "notes-next": "ok",
    }
    by_name["nas"]["upstream"] = "http://100.64.0.9:5001"
    doc = routes.show_routes(fleet, {"tailnet_proxies": OLD_MAP}, entries)
    assert {r["name"]: r["status"] for r in doc["routes"]}["nas"] == "drift"


def test_show_routes_wanted_vs_running(world: dict[str, Path], box: FakeBox) -> None:
    # Before any up: no route anywhere.
    result = cli(world, "show", "--routes", "--json")
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["routes"] == []

    assert add_route(world, "notes", **NOTES).exit_code == 0
    doc = json.loads(cli(world, "show", "--routes", "--json").stdout)
    (row,) = doc["routes"]
    assert (row["name"], row["status"], row["pinned"], row["running"]) == (
        "notes",
        "pending",
        None,
        None,
    )
    assert row["wanted"]["upstream"] == NOTES["upstream"]

    do_up(world)
    doc = json.loads(cli(world, "show", "--routes", "--json").stdout)
    assert [(r["name"], r["status"]) for r in doc["routes"]] == [("notes", "ok")]

    # The box serves something else: drift.
    box.receipts["production"]["routes"][0]["upstream"] = "http://laptop:9"
    doc = json.loads(cli(world, "show", "--routes", "--json").stdout)
    assert doc["routes"][0]["status"] == "drift"

    # A receipt without a routes list (today's box): RUNNING is unknown.
    del box.receipts["production"]["routes"]
    result = cli(world, "show", "--routes")
    assert result.exit_code == 0, result.output
    assert "notes  unknown" in result.stdout
    assert "RUNNING  unknown" in result.stdout
    assert "WANTED   notes.ts.example.com -> " + NOTES["upstream"] in result.stdout


# ── the verbs ───────────────────────────────────────────────────────────────


def test_route_add_ls_rm(world: dict[str, Path], box: FakeBox) -> None:
    before = fleet_text(world)
    head = git(world["fleet"], "rev-parse", "HEAD")
    result = add_route(
        world,
        "notes",
        "--identity",
        "--alias",
        "memo.ts.example.com",
        host="upstream",
        **NOTES,
    )
    assert result.exit_code == 0, result.output
    assert "run `bay plan production`" in result.output
    text = fleet_text(world)
    # Comments and every other line stay; the route table is added.
    for line in before.splitlines():
        assert line in text
    assert '[tailnet.routes.notes]\ndomain = "notes.ts.example.com"\n' in text
    assert tomllib.loads(text)["tailnet"]["routes"]["notes"] == {
        **NOTES,
        "host": "upstream",
        "identity": True,
        "aliases": ["memo.ts.example.com"],
    }
    # The route sits with the tailnet tables, before the webhook's comment.
    assert text.index("[tailnet.routes.notes]") < text.index("# Keep this comment")
    # The fleet repo got one commit with only the fleet file.
    assert git(world["fleet"], "log", "-1", "--format=%s") == "bay: route add notes"
    assert git(world["fleet"], "rev-parse", "HEAD~1") == head
    assert git(world["fleet"], "status", "--porcelain") == ""

    assert (
        add_route(
            world, "nas", domain="nas.ts.example.com", upstream="http://100.64.0.9:5000"
        ).exit_code
        == 0
    )

    listed = cli(world, "route", "ls")
    assert listed.exit_code == 0, listed.output
    rows = [line.split() for line in listed.stdout.splitlines()]
    assert rows[0] == ["NAME", "DOMAIN", "UPSTREAM", "HOST", "IDENTITY"]
    assert ["nas", "nas.ts.example.com", "http://100.64.0.9:5000", "client", "no"] in rows
    assert ["notes", "notes.ts.example.com", "(+1)", NOTES["upstream"], "upstream", "yes"] in rows
    as_json = json.loads(cli(world, "--json", "route", "ls").stdout)
    assert [r["name"] for r in as_json["data"]["routes"]] == ["nas", "notes"]

    # A refused route writes nothing.
    snapshot = fleet_text(world)
    bad = add_route(world, "web", domain="web.example.com", upstream="http://laptop:1")
    assert bad.exit_code != 0
    assert "not under cert_domain" in str(bad.exception) + bad.output
    assert fleet_text(world) == snapshot
    dup = add_route(world, "notes", **NOTES)
    assert dup.exit_code != 0 and fleet_text(world) == snapshot

    removed = cli(world, "route", "rm", "notes")
    assert removed.exit_code == 0, removed.output
    assert "run `bay plan production`" in removed.output
    text = fleet_text(world)
    assert "[tailnet.routes.notes]" not in text and "memo.ts.example.com" not in text
    assert "[tailnet.routes.nas]" in text
    assert "# Keep this comment: it belongs to the webhook below." in text
    assert "# The test fleet. This comment must survive every route edit." in text
    assert git(world["fleet"], "log", "-1", "--format=%s") == "bay: route rm notes"
    assert cli(world, "route", "rm", "nas").exit_code == 0
    # Back to the start, byte for byte.
    assert fleet_text(world) == before
    assert cli(world, "route", "rm", "nas").exit_code != 0


def test_route_add_refuses_a_dirty_fleet_file(world: dict[str, Path], box: FakeBox) -> None:
    path = world["fleet"] / "bay.fleet.toml"
    path.write_text(path.read_text() + "\n# an edit in progress\n")
    result = add_route(world, "notes", **NOTES)
    assert result.exit_code != 0
    assert "uncommitted changes" in str(result.exception)
    assert add_route(world, "notes", "--no-commit", **NOTES).exit_code == 0
    assert git(world["fleet"], "status", "--porcelain") == "M bay.fleet.toml"


def test_route_import_moves_the_old_file(world: dict[str, Path], box: FakeBox) -> None:
    old = world["fleet"] / routes.OLD_FILE
    old.write_text(yaml.safe_dump({"tailnet_proxies": OLD_MAP}, explicit_start=True))
    commit_all(world["fleet"], "old proxies")
    result = cli(world, "route", "import")
    assert result.exit_code == 0, result.output
    assert not old.exists()
    assert git(world["fleet"], "status", "--porcelain") == ""
    assert git(world["fleet"], "log", "-1", "--format=%s") == "bay: import 3 tailnet route(s)"
    table = tomllib.loads(fleet_text(world))["tailnet"]["routes"]
    assert sorted(table) == ["nas", "notes", "notes-next"]
    assert table["notes"]["aliases"] == ["memo.ts.example.com"]
    assert "# The tailnet: who may reach vpn services" in fleet_text(world)
    # The plan shows one route_added step per route.
    added = [s for s in make(world)["steps"] if s["action"] == "route_added"]
    assert sorted(s["resource"] for s in added) == ["nas", "notes", "notes-next"]


def test_route_import_reads_the_ingress_from_group_vars(tmp_path: Path) -> None:
    fleet = tmp_path / "fleet"
    (fleet / "group_vars" / "all").mkdir(parents=True)
    (fleet / "group_vars" / "edge").mkdir(parents=True)
    (fleet / "bay.fleet.toml").write_text(
        'name = "f"\ndefault_box = "edge"\ndefault_domain = "example.com"\n\n'
        '[boxes.edge]\nenv = "production"\ngroup = "edge"\n'
    )
    (fleet / "group_vars" / "edge" / "main.yml").write_text(
        '---\ntailnet_ingress_cert_domain: "*.ts.example.com"\n'
    )
    (fleet / "group_vars" / "all" / "tailnet_proxies.yml").write_text(
        yaml.safe_dump({"tailnet_proxies": {"nas": OLD_MAP["nas"]}})
    )
    result = runner.invoke(app, ["--fleet", str(fleet), "route", "import"])
    assert result.exit_code == 0, result.output
    doc = tomllib.loads((fleet / "bay.fleet.toml").read_text())
    assert doc["tailnet"]["ingress_box"] == "edge"
    assert doc["tailnet"]["cert_domain"] == "*.ts.example.com"
    assert doc["tailnet"]["routes"]["nas"]["upstream"] == "http://100.64.0.9:5000"


# ── text edits ──────────────────────────────────────────────────────────────


def test_text_edits_on_a_fleet_with_no_tailnet_table() -> None:
    text = 'name = "f"\n\n[boxes.a]\nenv = "production"\n'
    with_keys = routes.set_tailnet_keys(
        text, {"ingress_box": "a", "cert_domain": "*.ts.example.com"}
    )
    assert with_keys.startswith(text)
    added = routes.add_route_text(
        with_keys, "nas", {"domain": "nas.ts.example.com", "upstream": "http://nas:1"}
    )
    assert tomllib.loads(added)["tailnet"]["routes"]["nas"]["domain"] == "nas.ts.example.com"
    assert routes.remove_route_text(added, "nas") == with_keys
    # An existing key is replaced in place.
    again = routes.set_tailnet_keys(with_keys, {"ingress_box": "b"})
    assert 'ingress_box = "b"' in again and again.count("ingress_box") == 1


def test_tailnet_host_refuses_localhost_and_loopback() -> None:
    """An upstream on the ingress box itself is never a tailnet route."""
    for host in (
        "localhost",
        "LOCALHOST",
        "localhost.",
        "localhost.tailnet.internal",
        "127.0.0.1",
        "127.1",
        "2130706433",
        "0x7f000001",
        "0x7f.1",
        "::1",
        "::ffff:127.0.0.1",
    ):
        assert not routes.is_tailnet_host(host), host
    for host in ("laptop", "nas.tailnet.internal", "box.example.ts.net", "100.64.0.9",
                 "fd7a:115c:a1e0::9"):
        assert routes.is_tailnet_host(host), host


def test_route_import_json(world: dict[str, Path], box: FakeBox) -> None:
    old = world["fleet"] / routes.OLD_FILE
    old.write_text(yaml.safe_dump({"tailnet_proxies": OLD_MAP}, explicit_start=True))
    commit_all(world["fleet"], "old proxies")
    result = cli(world, "route", "import", "--json")
    assert result.exit_code == 0, result.output
    doc = json.loads(result.stdout)
    assert doc["fleet_commit"] == git(world["fleet"], "rev-parse", "HEAD")
    assert [r["name"] for r in doc["routes"]] == ["nas", "notes", "notes-next"]
    assert set(doc["routes"][0]) == {"name", "domain", "upstream"}
    assert doc["deleted"] == str(old.resolve())
    assert doc["cert_domain"] == tomllib.loads(fleet_text(world))["tailnet"]["cert_domain"]
    assert not old.exists()
