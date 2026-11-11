"""The Headscale admin API is closed to the internet (2.19.0).

Three parts, each pinned here:

1. Traefik: a second router on the Headscale host name takes `/api` and
   `/swagger` and sends them through `vpn-chain` (the vpn-only ipAllowList).
   Its labels are built in container_lifecycle/tasks/build_specs.yml, the
   place the reconciler reads; the compose partial only documents them. The
   tailscale protocol paths of Headscale 0.29.7 must never match the router.
   `headscale_admin_api_public: true` adds no label, so the spec stays as it
   was before.
2. tailscale_register: a box outside the control region registers by tasks
   delegated to the control host (`docker exec headscale headscale ...`), not
   by the public REST API. The API path runs only with
   `headscale_register_via_api: true`.
3. CLI: `bay gateway apikey list` and `bay gateway apikey expire
   <prefix-or-id>` run the headscale CLI on the control host.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml
from helpers import make_ansible_env
from jinja2 import Environment
from typer.testing import CliRunner

from bay_cli.cli import app
from bay_cli.commands import gateway
from bay_cli.commands.gateway_backend import LocalHeadscaleBackend, NullGatewayBackend
from bay_cli.context import Context

REPO = Path(__file__).resolve().parent.parent
BUILD_SPECS = REPO / "roles" / "container_lifecycle" / "tasks" / "build_specs.yml"
COMPOSE_DIR = REPO / "roles" / "deploy_stack" / "templates"
REGISTER = REPO / "roles" / "tailscale_register" / "tasks" / "main.yml"
REGISTER_DEFAULTS = REPO / "roles" / "tailscale_register" / "defaults" / "main.yml"
GATEWAY_DEFAULTS = REPO / "roles" / "access_gateway" / "defaults" / "main.yml"
HEADSCALE_DEFAULTS = REPO / "roles" / "headscale" / "defaults" / "main.yml"

DOMAIN = "hs.example.com"
ADMIN = "traefik.http.routers.headscale-admin"
ALLOW = "traefik.http.middlewares.headscale-admin-allow.ipallowlist.sourcerange"


def _yaml(path: Path) -> Any:
    return yaml.safe_load(path.read_text())


def _task(tasks: list[dict], name: str) -> dict:
    for task in tasks:
        if task.get("name") == name:
            return task
    raise AssertionError(f"no task named {name!r}")


def _ansible_bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("yes", "on", "1", "true", "y", "t")
    return bool(value)


def _env() -> Environment:
    """A Jinja env with the few Ansible filters and tests these expressions use."""
    env = Environment()
    env.filters["bool"] = _ansible_bool
    env.filters["combine"] = lambda a, *more: {k: v for d in (a, *more) for k, v in d.items()}
    env.tests["skipped"] = lambda r: isinstance(r, dict) and bool(r.get("skipped"))
    return env


def _render(value: Any, **ctx: Any) -> Any:
    if isinstance(value, dict):
        return {k: _render(v, **ctx) for k, v in value.items()}
    if isinstance(value, str) and "{{" in value:
        return _env().from_string(value).render(**ctx)
    return value


# ── 1. Traefik router ─────────────────────────────────────────────────────


def _headscale_task() -> dict:
    return _task(_yaml(BUILD_SPECS), "Build headscale container spec")


def _built_labels(**ctx: Any) -> dict[str, str]:
    """Evaluate the set_fact expression of the headscale spec task, as Ansible does."""
    task = _headscale_task()
    v = task["vars"]
    ctx = {"headscale_domain": DOMAIN, **ctx}
    spec = {**v["_headscale_spec"], "labels": _render(v["_headscale_spec"]["labels"], **ctx)}
    admin_labels = _render(v["_headscale_admin_router_labels"], **ctx)
    gate = v["_headscale_admin_router"].strip().removeprefix("{{").removesuffix("}}")
    admin = _env().compile_expression(gate)(
        _headscale_admin_router_labels=admin_labels, **ctx
    )
    expr = task["ansible.builtin.set_fact"]["_infra_specs"].strip()
    expr = expr.removeprefix("{{").removesuffix("}}")
    specs = _env().compile_expression(expr)(
        _infra_specs=[], _headscale_spec=spec, _headscale_admin_router=admin,
        _infra_log_rotation={},
    )
    assert len(specs) == 1
    return specs[0]["labels"]


def test_default_is_closed():
    assert _yaml(GATEWAY_DEFAULTS)["headscale_admin_api_public"] is False


def test_default_adds_the_admin_router():
    labels = _built_labels()
    assert labels[f"{ADMIN}.rule"] == (
        f"Host(`{DOMAIN}`) && (PathPrefix(`/api`) || PathPrefix(`/swagger`))"
    )
    assert labels[f"{ADMIN}.middlewares"] == "headscale-client-ip,public-chain,headscale-admin-allow"
    assert labels[ALLOW] == "127.0.0.1/32,::1/128"
    assert labels[f"{ADMIN}.service"] == "headscale"
    assert labels[f"{ADMIN}.tls.certresolver"] == "letsencrypt"
    assert labels[f"{ADMIN}.entrypoints"] == "websecure"
    # The main router keeps its rule and the public chain (2.20.0 puts the
    # True-Client-IP strip in front, test_headscale_trusted_proxies.py).
    assert labels["traefik.http.routers.headscale.rule"] == f"Host(`{DOMAIN}`)"
    assert labels["traefik.http.routers.headscale.middlewares"] == "headscale-client-ip,public-chain"


@pytest.mark.parametrize("value", [False, "false", "no"])
def test_false_values_close_it(value):
    assert f"{ADMIN}.rule" in _built_labels(headscale_admin_api_public=value)


@pytest.mark.parametrize("value", [True, "true", "yes"])
def test_public_true_keeps_the_labels_of_before(value):
    labels = _built_labels(headscale_admin_api_public=value)
    assert not any(k.startswith(ADMIN) for k in labels)
    assert labels == _render(_headscale_task()["vars"]["_headscale_spec"]["labels"],
                             headscale_domain=DOMAIN)


def test_split_entrypoints_bind_the_tailnet_listener_too():
    labels = _built_labels(traefik_split_entrypoints=True)
    assert labels[f"{ADMIN}.entrypoints"] == "websecure,websecure_tailnet"


def test_priority_beats_the_main_router_for_any_host_name():
    """Traefik's default priority is the rule length. The longest legal name is 253."""
    labels = _built_labels()
    priority = labels[f"{ADMIN}.priority"]
    assert isinstance(priority, str)  # Docker refuses a non-string label
    longest_main_rule = len("Host(``)") + 253
    assert int(priority) > longest_main_rule
    assert "traefik.http.routers.headscale.priority" not in labels


def _matches(rule: str, host: str, path: str) -> bool:
    """Evaluate the Host/PathPrefix/Path subset of the Traefik rule grammar."""
    expr = re.sub(r"Host\(`([^`]*)`\)", lambda m: repr(host == m.group(1)), rule)
    expr = re.sub(r"PathPrefix\(`([^`]*)`\)", lambda m: repr(path.startswith(m.group(1))), expr)
    expr = re.sub(r"Path\(`([^`]*)`\)", lambda m: repr(path == m.group(1)), expr)
    expr = expr.replace("&&", " and ").replace("||", " or ")
    assert re.fullmatch(r"[\sA-Za-z()]*", expr), f"unparsed rule part: {expr}"
    return bool(eval(expr))  # noqa: S307 - only True/False/and/or/() are left


# Every route of headscale 0.29.7 outside /api and /swagger, from
# hscontrol/app.go `createRouter` (chi). The noise routes (/machine/map,
# /machine/register ...) run inside the /ts2021 connection and never reach
# Traefik as separate requests. Recheck this list when headscale_version moves.
PROTOCOL_PATHS = [
    "/ts2021",
    "/robots.txt",
    "/health",
    "/version",
    "/key",
    "/key?v=106",
    "/register/abc123",
    "/register/confirm/abc123",
    "/auth/abc123",
    "/oidc/callback",
    "/apple",
    "/apple/macos",
    "/windows",
    "/verify",
    "/derp",
    "/derp/probe",
    "/derp/latency-check",
    "/bootstrap-dns",
    "/machine/ping-response",
    "/favicon.ico",
    "/",
]
ADMIN_PATHS = ["/api/v1/node", "/api/v1/apikey", "/api/v1/user", "/api", "/swagger",
               "/swagger/v1/openapiv2.json"]


def test_pinned_headscale_version_is_the_one_the_route_list_was_read_from():
    assert _yaml(GATEWAY_DEFAULTS)["headscale_version"] == "0.29.7", (
        "headscale moved: check PROTOCOL_PATHS against hscontrol/app.go createRouter"
    )


@pytest.mark.parametrize("path", PROTOCOL_PATHS)
def test_protocol_paths_stay_public(path):
    rule = _built_labels()[f"{ADMIN}.rule"]
    assert not _matches(rule, DOMAIN, path.split("?")[0])
    assert _matches(_built_labels()["traefik.http.routers.headscale.rule"], DOMAIN, path)


@pytest.mark.parametrize("path", ADMIN_PATHS)
def test_admin_paths_are_gated(path):
    assert _matches(_built_labels()[f"{ADMIN}.rule"], DOMAIN, path)


def test_other_host_names_are_not_matched():
    assert not _matches(_built_labels()[f"{ADMIN}.rule"], "app.example.com", "/api/v1/node")


def _compose(**ctx: Any) -> str:
    env = make_ansible_env(COMPOSE_DIR)
    env.filters["bool"] = _ansible_bool
    env.globals["macros"] = type("M", (), {"logging_block": staticmethod(lambda _: "")})
    base = {"access_gateway": "headscale", "headscale_server": True, "headscale_version": "0.29.7",
            "traefik_docker_network": "services", "headscale_domain": DOMAIN}
    return env.get_template("_headscale.j2").render(**{**base, **ctx})


def test_compose_partial_documents_the_same_labels():
    text = _compose()
    for key, value in _built_labels().items():
        if key.startswith(ADMIN):
            assert f'"{key}={value}"' in text, key
    assert "headscale-admin" not in _compose(headscale_admin_api_public=True)


def test_admin_router_does_not_use_the_tailnet_allowlist():
    """2.20.0: vpn-chain let every tailnet node (tenants too) reach the API."""
    middlewares = _built_labels()[f"{ADMIN}.middlewares"].split(",")
    assert "vpn-chain" not in middlewares and "vpn-only" not in middlewares
    # The main router's middlewares, plus the dedicated allowlist.
    assert middlewares == ["headscale-client-ip", "public-chain", "headscale-admin-allow"]


def test_default_allowlist_is_loopback_only():
    assert _yaml(GATEWAY_DEFAULTS)["headscale_admin_api_allowed_ips"] == ["127.0.0.1/32", "::1/128"]
    # build_specs and the compose partial fall back to the same list.
    assert _built_labels()[ALLOW] == "127.0.0.1/32,::1/128"
    assert f'"{ALLOW}=127.0.0.1/32,::1/128"' in _compose()


def test_allowlist_follows_the_variable():
    ips = ["127.0.0.1/32", "::1/128", "100.64.0.7/32"]
    assert _built_labels(headscale_admin_api_allowed_ips=ips)[ALLOW] == ",".join(ips)
    assert f'"{ALLOW}={",".join(ips)}"' in _compose(headscale_admin_api_allowed_ips=ips)


def test_public_true_ignores_the_allowlist():
    labels = _built_labels(headscale_admin_api_public=True,
                           headscale_admin_api_allowed_ips=["100.64.0.7/32"])
    assert ALLOW not in labels


def _allowlist_check(**ctx: Any) -> bool:
    """Run the assert task of build_specs.yml; True when every condition holds."""
    task = _task(_yaml(BUILD_SPECS), "Check the Headscale admin API allowlist")
    env = _env()
    env.tests["match"] = lambda v, p: re.match(p, v) is not None
    ips_expr = task["vars"]["_hs_admin_ips"].strip().removeprefix("{{").removesuffix("}}")
    ips = env.compile_expression(ips_expr)(**ctx)
    return all(env.compile_expression(str(c).strip())(_hs_admin_ips=ips, **ctx)
               for c in task["ansible.builtin.assert"]["that"])


@pytest.mark.parametrize("ips", [
    None,
    ["127.0.0.1/32", "::1/128"],
    ["100.64.0.7/32"],
    ["100.64.0.7", "fd7a:115c:a1e0::7/128", "10.0.0.0/8"],
])
def test_allowlist_check_accepts_ips_and_cidrs(ips):
    ctx = {} if ips is None else {"headscale_admin_api_allowed_ips": ips}
    assert _allowlist_check(**ctx)


@pytest.mark.parametrize("ips", [
    [],
    "100.64.0.7/32",
    ["100.64.0.7/32", "laptop"],
    ["100.64.0.7/32,10.0.0.0/8"],
    [""],
    [7],
])
def test_allowlist_check_refuses_what_traefik_cannot_parse(ips):
    assert not _allowlist_check(headscale_admin_api_allowed_ips=ips)


def test_allowlist_check_runs_only_when_the_router_is_built():
    task = _task(_yaml(BUILD_SPECS), "Check the Headscale admin API allowlist")
    base = {"access_gateway": "headscale", "headscale_server": True}
    assert _runs(task, **base)
    assert not _runs(task, **base, headscale_admin_api_public=True)
    assert not _runs(task, access_gateway="wireguard", headscale_server=True)


# ── 2. tailscale_register ─────────────────────────────────────────────────

SSH_TASKS = [
    "Resolve the headscale control host",
    "Check SSH access to the control host",
    "Check the headscale container on the control host",
    "Create headscale user (control host)",
    "List headscale users (control host)",
]
DELEGATED = [
    "Check SSH access to the control host",
    "Check the headscale container on the control host",
    "Create headscale user (control host)",
    "List headscale users (control host)",
    "Generate pre-auth key (control host)",
]
API_TASKS = [
    "Fail if no API key for remote registration",
    "Create headscale user (API)",
    "Look up server user ID (API)",
    "Generate pre-auth key (API)",
    "Connect tailscale to headscale (API path)",
]


def _runs(task: dict, **ctx: Any) -> bool:
    when = task.get("when", [])
    conditions = when if isinstance(when, list) else [when]
    env = _env()
    return all(env.compile_expression(str(c).strip())(**ctx) for c in conditions)


_REMOTE = {"_needs_registration": True, "headscale_server": False,
           "secrets": {"headscale_api_key": ""}}


def test_register_default_is_the_ssh_path():
    assert _yaml(REGISTER_DEFAULTS)["headscale_register_via_api"] is False


@pytest.mark.parametrize("name", SSH_TASKS)
def test_remote_box_takes_the_delegate_path_by_default(name):
    assert _runs(_task(_yaml(REGISTER), name), **_REMOTE)
    assert not _runs(_task(_yaml(REGISTER), name), **_REMOTE, headscale_register_via_api=True)


@pytest.mark.parametrize("name", API_TASKS)
def test_api_tasks_run_only_on_opt_in(name):
    task = _task(_yaml(REGISTER), name)
    assert not _runs(task, **_REMOTE)
    assert not _runs(task, **_REMOTE, headscale_register_via_api=False)
    assert _runs(task, **_REMOTE, headscale_register_via_api=True)


def test_the_default_path_never_calls_the_api():
    for task in _yaml(REGISTER):
        if "ansible.builtin.uri" in task:
            assert "headscale_register_via_api" in str(task["when"]), task["name"]


@pytest.mark.parametrize("name", SSH_TASKS + API_TASKS)
def test_control_host_and_check_mode_skip_all_remote_tasks(name):
    task = _task(_yaml(REGISTER), name)
    assert not _runs(task, _needs_registration=True, headscale_server=True,
                     headscale_register_via_api=True, secrets={})
    # Check mode: the status probe is skipped, the fact is never set.
    assert not _runs(task, headscale_server=False, headscale_register_via_api=True, secrets={})


@pytest.mark.parametrize("name", DELEGATED)
def test_delegated_tasks_target_the_control_host(name):
    task = _task(_yaml(REGISTER), name)
    assert task["delegate_to"].startswith("{{ _headscale_control_host")


@pytest.mark.parametrize("name", DELEGATED[1:])
def test_docker_tasks_run_as_root(name):
    """deploy.yml runs as app_user; docker needs root, so say so explicitly."""
    task = _task(_yaml(REGISTER), name)
    assert task["become"] is True and task["become_user"] == "root"
    assert "docker" in task["ansible.builtin.command"]


def test_control_host_is_the_first_host_of_the_control_region_group():
    task = _task(_yaml(REGISTER), "Resolve the headscale control host")
    expr = task["ansible.builtin.set_fact"]["_headscale_control_host"].strip()
    render = lambda **c: _env().from_string(expr).render(**c).strip()  # noqa: E731
    groups = {"alpha": ["alpha-1", "alpha-2"], "beta": ["beta-1"]}
    assert render(groups=groups, headscale_control_region="alpha") == "alpha-1"
    assert render(groups=groups, headscale_control_region="gamma") == ""


def test_missing_control_host_fails_with_what_to_do():
    task = _task(_yaml(REGISTER), "Fail if the control host is not in the inventory")
    assert _runs(task, **_REMOTE, _headscale_control_host="")
    assert not _runs(task, **_REMOTE, _headscale_control_host="alpha-1")
    msg = task["ansible.builtin.fail"]["msg"]
    assert "headscale_register_via_api: true" in msg and "headscale_admin_api_public: true" in msg


def test_unreachable_control_host_fails_with_what_to_do():
    task = _task(_yaml(REGISTER), "Fail if the control host is not reachable")
    ok = {"ping": "pong", "changed": False}
    assert not _runs(task, _headscale_control_ping=ok)
    assert _runs(task, _headscale_control_ping={"unreachable": True, "msg": "timeout"})
    assert not _runs(task, _headscale_control_ping={"skipped": True})
    ping = _task(_yaml(REGISTER), "Check SSH access to the control host")
    assert ping["ignore_unreachable"] is True
    assert "SSH" in task["ansible.builtin.fail"]["msg"]


def test_json_parsing_is_guarded_against_check_mode_skips():
    tasks = _yaml(REGISTER)
    extract = _task(tasks, "Extract server user ID (control host)")
    assert "_headscale_control_users is not skipped" in extract["when"]
    connect = _task(tasks, "Connect tailscale to headscale (control host path)")
    assert "_headscale_control_preauth is not skipped" in connect["when"]
    assert connect["no_log"] is True and connect["become_user"] == "root"
    assert "from_json).key" in connect["ansible.builtin.command"]


def test_user_id_comes_from_the_json_list():
    task = _task(_yaml(REGISTER), "Extract server user ID (control host)")
    expr = task["ansible.builtin.set_fact"]["_headscale_user_id"].strip()
    env = _env()
    env.filters["from_json"] = __import__("json").loads
    out = env.from_string(expr).render(
        _headscale_control_users={"stdout": '[{"id": 1, "name": "ops"}, {"id": 4, "name": "app"}]'},
        headscale_server_user="app",
    )
    assert out.strip() == "4"


def test_preauth_key_is_short_lived_json_and_not_logged():
    task = _task(_yaml(REGISTER), "Generate pre-auth key (control host)")
    cmd = task["ansible.builtin.command"]
    assert "preauthkeys create" in cmd and "--expiration 5m" in cmd and "-o json" in cmd
    assert task["no_log"] is True


# ── 3. CLI: apikey list / expire ──────────────────────────────────────────

runner = CliRunner()


class _KeyBackend:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def list_api_keys(self) -> list[dict]:
        self.calls.append(("list_api_keys",))
        return [{"id": 3, "prefix": "hskey-api-AbCdEf123456-***",
                 "expiration": {"seconds": 1893456000, "nanos": 0},
                 "created_at": {"seconds": 1784036432, "nanos": 0}}]

    def expire_api_key(self, *, prefix: str = "", key_id: int | None = None) -> None:
        self.calls.append(("expire_api_key", prefix, key_id))

    def create_api_key(self, expiration: str) -> str:
        self.calls.append(("create_api_key", expiration))
        return "hskey-api-placeholder-key"


@pytest.fixture()
def keys(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _KeyBackend:
    backend = _KeyBackend()
    monkeypatch.setattr(
        Context, "resolve",
        classmethod(lambda cls, fleet=None: Context.for_fleet_root(tmp_path, tmp_path)),
    )
    monkeypatch.setattr(gateway, "_get_gateway_config",
                        lambda *a, **k: {"access_gateway": "headscale", "headscale_domain": DOMAIN})
    monkeypatch.setattr(gateway, "_make_backend", lambda *a, **k: backend)
    monkeypatch.setattr(gateway, "show_ephemeral", lambda *a, **k: None)
    return backend


@pytest.mark.parametrize(("arg", "prefix", "key_id"), [
    ("AbCdEf123456", "AbCdEf123456", None),
    ("hskey-api-AbCdEf123456-***", "AbCdEf123456", None),
    ("hskey-api-place_hold-1-secret-part-here", "place_hold-1", None),
    ("abc1234***", "abc1234", None),
    ("3", "", 3),
])
def test_expire_reaches_the_backend(keys, arg, prefix, key_id):
    result = runner.invoke(app, ["gateway", "apikey", "expire", arg])
    assert result.exit_code == 0, result.output
    assert keys.calls == [("expire_api_key", prefix, key_id)]


@pytest.mark.parametrize("arg", ["x; id", "x$(id)", "a b", "x\nid", "0", "", "pre*fix"])
def test_expire_refuses_bad_values_before_the_backend(keys, arg):
    result = runner.invoke(app, ["gateway", "apikey", "expire", arg])
    assert result.exit_code == 1
    assert keys.calls == []


def test_expire_needs_a_target(keys):
    result = runner.invoke(app, ["gateway", "apikey", "expire"])
    assert result.exit_code == 1
    assert keys.calls == []


def test_list_shows_id_and_prefix(keys):
    result = runner.invoke(app, ["gateway", "apikey", "list"])
    assert result.exit_code == 0, result.output
    assert keys.calls == [("list_api_keys",)]
    assert "hskey-api-AbCdEf123456-***" in result.output


def test_plain_apikey_still_creates(keys):
    result = runner.invoke(app, ["gateway", "apikey", "--expiration", "90d"])
    assert result.exit_code == 0, result.output
    assert keys.calls == [("create_api_key", "90d")]


def test_unknown_action_is_refused(keys):
    result = runner.invoke(app, ["gateway", "apikey", "delete", "3"])
    assert result.exit_code == 1
    assert keys.calls == []


def _sent(monkeypatch: pytest.MonkeyPatch, call) -> str:
    sent: list[str] = []
    monkeypatch.setattr(LocalHeadscaleBackend, "_exec",
                        lambda self, cmd, message="", check=True: sent.append(cmd) or "[]")
    call(LocalHeadscaleBackend(bay_dir=Path("/tmp"), env="production"))
    return sent[0]


def test_backend_expire_by_prefix_runs_the_cli(monkeypatch):
    cmd = _sent(monkeypatch, lambda b: b.expire_api_key(prefix="AbCdEf123456"))
    assert cmd == "docker exec headscale headscale apikeys expire --prefix AbCdEf123456"


def test_backend_expire_by_id_runs_the_cli(monkeypatch):
    cmd = _sent(monkeypatch, lambda b: b.expire_api_key(key_id=3))
    assert cmd == "docker exec headscale headscale apikeys expire --id 3"


def test_backend_expire_takes_exactly_one(monkeypatch):
    for kwargs in ({}, {"prefix": "abc", "key_id": 1}):
        with pytest.raises(ValueError):
            _sent(monkeypatch, lambda b, kw=kwargs: b.expire_api_key(**kw))


def test_backend_list_runs_the_cli(monkeypatch):
    cmd = _sent(monkeypatch, lambda b: b.list_api_keys())
    assert cmd == "docker exec headscale headscale apikeys list -o json"


def test_null_backend_refuses_key_operations():
    from bay_cli.errors import BayError

    backend = NullGatewayBackend("wireguard")
    with pytest.raises(BayError):
        backend.list_api_keys()
    with pytest.raises(BayError):
        backend.expire_api_key(prefix="abc")


def test_no_cli_code_calls_the_admin_api():
    """Every `bay gateway` verb goes through docker exec on the control host."""
    for path in (REPO / "src" / "bay_cli").rglob("*.py"):
        text = path.read_text()
        assert "/api/v1" not in text, path


def test_headscale_defaults_header_states_the_limit():
    text = HEADSCALE_DEFAULTS.read_text()
    assert "headscale_admin_api_public" in text
    assert "cannot enrol" in text
