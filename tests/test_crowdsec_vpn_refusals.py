"""Guards the whitelist that keeps Bay's own VPN 403s out of CrowdSec.

Bay's `vpn-only` IPAllowList middleware answers 403 to a client outside the
VPN before any backend sees the request. The Hub HTTP scenarios cannot tell
that from an app's 403: crowdsecurity/http-admin-interface-probing banned an
operator for 24h after three off-VPN requests to /admin, /Admin and /ADMIN/x.

The role renders an s02-enrich whitelist parser that drops exactly those
events. These tests pin three things:

1. The rendered parser is valid YAML with the expected expression.
2. The expression whitelists a VPN refusal and NOTHING else: an app's own 403,
   a 404, a public router and a health router must all still reach the
   scenarios. The field values below are copied from `cscli explain -v` output
   for real Traefik CLF lines on a Bay host (CrowdSec 1.7.7,
   crowdsecurity/traefik-logs 1.5), with documentation IPs.
3. The opt-out (`crowdsec_ignore_vpn_refusals: false`) removes the file.
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from jinja2 import Environment, FileSystemLoader

_ROLE = Path(__file__).resolve().parent.parent / "roles" / "crowdsec"
_TEMPLATE_DIR = _ROLE / "templates" / "parsers"
_TEMPLATE_NAME = "vpn-refusals-whitelist.yaml.j2"
_DEST = "/etc/crowdsec/parsers/s02-enrich/bay-vpn-allowlist-refusals.yaml"
_VAR = "crowdsec_ignore_vpn_refusals"

DEPLOY_TASK = "Deploy VPN allowlist refusal whitelist"
REMOVE_TASK = "Remove VPN allowlist refusal whitelist when disabled"

EXPECTED_EXPRESSION = (
    "evt.Meta.http_status == '403'"
    " && evt.Parsed.traefik_server_url == '-'"
    " && (evt.Parsed.traefik_router_name endsWith '-vpn@docker'"
    " || evt.Parsed.traefik_router_name endsWith '-tailnet@file')"
)


@pytest.fixture(scope="module")
def parser() -> dict[str, Any]:
    env = Environment(
        loader=FileSystemLoader(str(_TEMPLATE_DIR)),
        trim_blocks=True,
        lstrip_blocks=False,
        keep_trailing_newline=True,
    )
    rendered = env.get_template(_TEMPLATE_NAME).render()
    loaded = yaml.safe_load(rendered)
    assert isinstance(loaded, dict)
    return loaded


@pytest.fixture(scope="module")
def tasks() -> list[dict[str, Any]]:
    loaded = yaml.safe_load((_ROLE / "tasks" / "main.yml").read_text())
    assert isinstance(loaded, list)
    return loaded


def _task(tasks: list[dict[str, Any]], name: str) -> dict[str, Any]:
    for task in tasks:
        if task.get("name") == name:
            return task
    raise AssertionError(f"task {name!r} not found in the crowdsec role")


# ── 1. the rendered parser ─────────────────────────────────────────────


def test_parser_is_a_whitelist_with_the_expected_expression(parser: dict[str, Any]) -> None:
    assert parser["name"] == "bay/vpn-allowlist-refusals"
    assert parser["whitelist"]["expression"] == [EXPECTED_EXPRESSION]
    # An `ip:`/`cidr:` key here would whitelist a client outright, which is
    # the opposite of this parser's job.
    assert set(parser["whitelist"]) == {"reason", "expression"}


def test_parser_only_looks_at_traefik_access_logs(parser: dict[str, Any]) -> None:
    assert parser["filter"] == (
        "evt.Meta.log_type == 'http_access-log' && evt.Parsed.program == 'traefik'"
    )


# ── 2. the expression logic ────────────────────────────────────────────
#
# CrowdSec evaluates these with expr-lang. The translation below covers only
# the operators the parser uses and refuses anything else, so adding an
# operator to the template fails this test instead of being misread.

_EXPR_OPERATOR = re.compile(r"\b(startsWith|contains|matches|in|not)\b|!")


def _to_python(expr: str) -> str:
    out = re.sub(r"(\S+) endsWith ('[^']*')", r"\1.endswith(\2)", expr)
    out = out.replace("&&", " and ").replace("||", " or ")
    leftover = _EXPR_OPERATOR.search(out)
    assert leftover is None, f"untranslated expr operator {leftover.group(0)!r} in {expr!r}"
    return out


class _Fields(dict[str, str]):
    # A missing key in evt.Meta / evt.Parsed is the empty string in CrowdSec
    # (map[string]string zero value), never an error.
    def __getattr__(self, name: str) -> str:
        return self.get(name, "")


def _evaluate(expr: str, meta: dict[str, str], parsed: dict[str, str]) -> bool:
    evt = SimpleNamespace(Meta=_Fields(meta), Parsed=_Fields(parsed))
    return bool(eval(_to_python(expr), {"__builtins__": {}}, {"evt": evt}))  # noqa: S307


def _traefik_event(status: str, router: str, server_url: str) -> tuple[dict[str, str], dict[str, str]]:
    meta = {
        "log_type": "http_access-log",
        "service": "http",
        "http_status": status,
        "http_path": "/Admin",
        "traefik_router_name": router,
        "source_ip": "203.0.113.7",
    }
    parsed = {
        "program": "traefik",
        "status": status,
        "traefik_router_name": router,
        "traefik_server_url": server_url,
    }
    return meta, parsed


# (status, router, server URL, whitelisted?)
_CASES = [
    # The alert-24144 line: vpn-only answered, no backend.
    ("403", "gatus-vpn@docker", "-", True),
    # tailnet_proxies routers carry vpn-only too.
    ("403", "notes-tailnet@file", "-", True),
    # The app itself answered 403 behind the VPN router: still detected.
    ("403", "gatus-vpn@docker", "http://172.18.0.4:8080", False),
    # A public router never carries the allowlist.
    ("403", "gatus@docker", "-", False),
    # Only 403 is the allowlist's answer.
    ("404", "gatus-vpn@docker", "-", False),
    # Health routers are out of scope (one exact path, see docs/crowdsec.md).
    ("403", "gatus-health@docker", "-", False),
    # No router matched at all.
    ("403", "-", "-", False),
    # The suffix must be the END of the name.
    ("403", "gatus-vpn@docker-x", "-", False),
]


@pytest.mark.parametrize(("status", "router", "server_url", "whitelisted"), _CASES)
def test_expression_whitelists_only_vpn_refusals(
    parser: dict[str, Any], status: str, router: str, server_url: str, whitelisted: bool
) -> None:
    meta, parsed = _traefik_event(status, router, server_url)
    assert _evaluate(parser["filter"], meta, parsed)
    (expr,) = parser["whitelist"]["expression"]
    assert _evaluate(expr, meta, parsed) is whitelisted


def test_expression_ignores_events_without_traefik_fields(parser: dict[str, Any]) -> None:
    """An nginx access line has no traefik_* fields. The filter keeps it out,
    and the expression would not match it either."""
    meta = {"log_type": "http_access-log", "http_status": "403"}
    parsed = {"program": "nginx", "status": "403"}
    assert not _evaluate(parser["filter"], meta, parsed)
    (expr,) = parser["whitelist"]["expression"]
    assert not _evaluate(expr, meta, parsed)


# ── 3. on by default, and the opt-out removes it ───────────────────────


def test_on_by_default() -> None:
    defaults = yaml.safe_load((_ROLE / "defaults" / "main.yml").read_text())
    assert defaults[_VAR] is True


def _runs(task: dict[str, Any], value: bool) -> bool:
    env = Environment()
    env.filters["bool"] = bool
    when = task.get("when", [])
    conditions = [when] if isinstance(when, str) else list(when)
    context = {"crowdsec_enabled": True, _VAR: value}
    return all(
        env.from_string("{{ (" + cond + ") }}").render(context) == "True"
        for cond in conditions
    )


@pytest.mark.parametrize("enabled", [True, False])
def test_opt_out_swaps_deploy_for_removal(tasks: list[dict[str, Any]], enabled: bool) -> None:
    deploy = _task(tasks, DEPLOY_TASK)
    remove = _task(tasks, REMOVE_TASK)

    assert deploy["ansible.builtin.template"]["src"] == f"parsers/{_TEMPLATE_NAME}"
    assert deploy["ansible.builtin.template"]["dest"] == _DEST
    assert remove["ansible.builtin.file"] == {"path": _DEST, "state": "absent"}
    assert deploy["notify"] == remove["notify"] == "Reload crowdsec"

    assert _runs(deploy, enabled) is enabled
    assert _runs(remove, enabled) is not enabled
