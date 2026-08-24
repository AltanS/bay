"""Render + schema + Python-validator tests for service `ports.expose:` (M87).

Covers the M87 fix for the post-M83 cross-region service-link gap (GH bay#7):

- Schema accepts/rejects `ports.expose` enum values
- Macro renders host-port binding when service has `ports.internal +
  ports.expose` (Traefik-fronted service that opts into a host bind, e.g. for
  cross-region links)
- Default (no `ports.expose`) → no host binding rendered (existing behaviour
  for Traefik-fronted services)
- String `port:` form takes precedence (backwards compat)
- Python validator `_validate_link_target_exposure` catches a cross-region
  link to a service that lacks `ports.expose: tailnet`

The render harness mirrors `test_log_rotation._render_service` — real
`_service.j2` + `_macros.j2` under an Ansible-shaped Jinja2 environment.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest
from helpers import make_ansible_env

from bay_cli.commands.validate import _validate_link_target_exposure


# ── Ansible filter shims ──────────────────────────────────────────────


def _hash_filter(value, algo="sha1"):
    return hashlib.new(algo, str(value).encode()).hexdigest()


def _combine_filter(base, *others, recursive=False, list_merge="replace"):
    result = dict(base or {})
    for other in others:
        if other is None:
            continue
        result.update(other)
    return result


def _to_json_filter(value, **_kwargs):
    return json.dumps(value)


def _quote_filter(value):
    import shlex

    return shlex.quote(str(value))


def _regex_replace_filter(value, pattern, replacement, ignorecase=False):
    flags = re.IGNORECASE if ignorecase else 0
    return re.sub(pattern, replacement, str(value), flags=flags)


def _dict2items_filter(d):
    return [{"key": k, "value": v} for k, v in (d or {}).items()]


def _register_ansible_filters(env):
    env.filters.setdefault("hash", _hash_filter)
    env.filters.setdefault("combine", _combine_filter)
    env.filters.setdefault("to_json", _to_json_filter)
    env.filters.setdefault("quote", _quote_filter)
    env.filters.setdefault("regex_replace", _regex_replace_filter)
    env.filters.setdefault("dict2items", _dict2items_filter)
    env.filters.setdefault("bool", lambda v: bool(v))
    env.globals.setdefault("lookup", lambda *_a, **_k: "")
    return env


TEMPLATE_DIR = (
    Path(__file__).resolve().parent.parent
    / "roles"
    / "deploy_stack"
    / "templates"
)

SCHEMA_PATH = (
    Path(__file__).resolve().parent.parent
    / "src"
    / "bay_cli"
    / "schemas"
    / "services.schema.json"
)


def _base_svc_ctx(**overrides):
    ctx = {
        "stack_name": "testapp",
        "stack_dir": "/opt/testapp",
        "traefik_docker_network": "services",
        "traefik_security_headers_enabled": False,
        "traefik_compress_enabled": False,
        "traefik_error_pages_enabled": False,
        "traefik_rate_limit_average": 100,
        "traefik_rate_limit_burst": 200,
        "traefik_rate_limit_period": "1s",
        "traefik_in_flight_req_amount": 100,
        "traefik_circuit_breaker_expression": "NetworkErrorRatio() > 0.5",
        "traefik_retry_attempts": 3,
        "link_targets": [],
        "log_rotation_defaults": False,
        "watchtower_enabled": False,
        "active_services": {},
    }
    ctx.update(overrides)
    return ctx


def _render_service(svc_name: str, svc: dict, **ctx_overrides) -> str:
    env = _register_ansible_filters(make_ansible_env(TEMPLATE_DIR))
    ctx = _base_svc_ctx(active_services={svc_name: svc}, **ctx_overrides)
    tmpl_src = (
        "{% import '_macros.j2' as macros with context %}"
        "{% for name, svc in active_services.items() %}"
        "{% include '_service.j2' %}"
        "{% endfor %}"
    )
    return env.from_string(tmpl_src).render(**ctx)


def _port_lines(rendered: str) -> list[str]:
    """Extract every `- "<binding>"` entry under a `ports:` key.

    Returns the binding strings in render order. Used to check that
    `expose:` produced exactly the expected one binding (and didn't leak
    a duplicate into the loadbalancer section of `_service.j2`)."""
    out: list[str] = []
    in_ports = False
    for line in rendered.splitlines():
        stripped = line.strip()
        if stripped.startswith("ports:"):
            in_ports = True
            continue
        if in_ports:
            m = re.match(r'-\s+"([^"]+)"\s*$', stripped)
            if m:
                out.append(m.group(1))
                continue
            # Any non-list-item line ends the ports block
            if stripped and not stripped.startswith("-"):
                in_ports = False
    return out


# ── Render: ports.expose modes ────────────────────────────────────────


class TestServicePortsExposeRender:
    def test_no_expose_no_host_binding(self):
        """Default Traefik-fronted service: ports.internal only → no
        host-port binding rendered (existing behaviour, unchanged)."""
        svc = {
            "access": "public",
            "image": "myapp:latest",
            "domains": ["app.example.com"],
            "ports": {"internal": 5100},
        }
        rendered = _render_service("myapp", svc)
        assert _port_lines(rendered) == []

    def test_ports_expose_tailnet_renders_tailnet_binding(self):
        """Cross-region link target opts in via `ports.expose: tailnet` —
        the host port equals ports.internal, bound to the tailnet IP."""
        svc = {
            "access": "vpn",
            "image": "myapp:latest",
            "domains": ["app.example.com"],
            "ports": {"internal": 5100, "expose": "tailnet"},
        }
        rendered = _render_service(
            "myapp", svc, gateway_bind_ip="100.64.0.1"
        )
        assert _port_lines(rendered) == ["100.64.0.1:5100:5100"]

    def test_ports_expose_loopback_renders_loopback_binding(self):
        svc = {
            "access": "public",
            "image": "myapp:latest",
            "domains": ["app.example.com"],
            "ports": {"internal": 5100, "expose": "loopback"},
        }
        rendered = _render_service("myapp", svc)
        assert _port_lines(rendered) == ["127.0.0.1:5100:5100"]

    def test_ports_expose_host_renders_public_binding(self):
        svc = {
            "access": "public",
            "image": "myapp:latest",
            "domains": ["app.example.com"],
            "ports": {"internal": 5100, "expose": "host"},
        }
        rendered = _render_service("myapp", svc)
        assert _port_lines(rendered) == ["0.0.0.0:5100:5100"]

    def test_string_port_takes_precedence_over_ports_expose(self):
        """Backwards-compat: when both `port:` (string) and `ports.expose`
        exist, the string `port:` form wins to keep the existing accessory-
        style render path stable. Operators should use one or the other."""
        svc = {
            "access": "public",
            "image": "myapp:latest",
            "domains": ["app.example.com"],
            "ports": {"internal": 5100, "expose": "tailnet"},
            "port": "127.0.0.1:9999:9999",
            "expose": "loopback",
        }
        rendered = _render_service(
            "myapp", svc, gateway_bind_ip="100.64.0.1"
        )
        # The string-port path renders 127.0.0.1:9999:9999, NOT the
        # ports.expose tailnet binding.
        assert _port_lines(rendered) == ["127.0.0.1:9999:9999"]


# ── Schema ────────────────────────────────────────────────────────────


class TestServicePortsExposeSchema:
    def _load(self) -> dict:
        return json.loads(SCHEMA_PATH.read_text())

    def test_ports_block_has_expose_property(self):
        schema = self._load()
        ports_block = schema["$defs"]["ports_block"]
        assert "expose" in ports_block["properties"]
        assert ports_block["properties"]["expose"]["enum"] == ["loopback", "gateway", "tailnet", "host"]

    def test_ports_block_still_rejects_unknown_keys(self):
        schema = self._load()
        ports_block = schema["$defs"]["ports_block"]
        assert ports_block.get("additionalProperties") is False

    def test_ports_block_internal_still_required(self):
        schema = self._load()
        ports_block = schema["$defs"]["ports_block"]
        assert ports_block.get("required") == ["internal"]

    def test_schema_accepts_ports_expose_tailnet(self):
        from jsonschema import Draft202012Validator

        schema = self._load()
        data = {
            "services": {
                "myapp": {
                    "access": "vpn",
                    "image": "myapp:latest",
                    "domains": ["app.example.com"],
                    "ports": {"internal": 5100, "expose": "tailnet"},
                }
            }
        }
        errors = list(Draft202012Validator(schema).iter_errors(data))
        assert errors == []

    def test_schema_rejects_invalid_ports_expose_value(self):
        from jsonschema import Draft202012Validator

        schema = self._load()
        data = {
            "services": {
                "myapp": {
                    "access": "vpn",
                    "image": "myapp:latest",
                    "domains": ["app.example.com"],
                    "ports": {"internal": 5100, "expose": "public"},
                }
            }
        }
        errors = list(Draft202012Validator(schema).iter_errors(data))
        assert errors, "schema should reject expose: public"
        assert any("'public' is not one of" in e.message for e in errors)

    def test_schema_accepts_ports_without_expose(self):
        """Backwards compat: services that don't declare ports.expose still
        validate (the field is optional)."""
        from jsonschema import Draft202012Validator

        schema = self._load()
        data = {
            "services": {
                "myapp": {
                    "access": "public",
                    "image": "myapp:latest",
                    "domains": ["app.example.com"],
                    "ports": {"internal": 5100},
                }
            }
        }
        errors = list(Draft202012Validator(schema).iter_errors(data))
        assert errors == []


# ── Python validator: _validate_link_target_exposure ─────────────────


class _FakeResult:
    def __init__(self) -> None:
        self.failures: list[str] = []

    def fail(self, msg: str) -> None:
        self.failures.append(msg)

    def ok(self, _msg: str) -> None:
        pass

    def warn(self, _msg: str) -> None:
        pass


def _run_link_validator(data: dict) -> _FakeResult:
    r = _FakeResult()
    _validate_link_target_exposure(data, "group_vars/all/services.yml", r)
    return r


class TestLinkTargetExposureValidator:
    def test_cross_region_link_target_without_expose_fails(self):
        """The user's bug from issue #7: NA service links to EU service
        but EU service has no ports.expose → validator fails before deploy."""
        data = {
            "services": {
                "consumer": {
                    "regions": ["na"],
                    "links": {"target": {"region": "eu"}},
                },
                "target": {
                    "regions": ["eu"],
                    "ports": {"internal": 5100},
                },
            },
            "accessories": {},
        }
        failures = _run_link_validator(data).failures
        assert len(failures) == 1
        assert "ports.expose" in failures[0]
        assert "consumer" in failures[0]
        assert "target" in failures[0]
        assert "LINKS_TARGET_HOST" in failures[0]

    def test_cross_region_link_target_with_ports_expose_tailnet_passes(self):
        data = {
            "services": {
                "consumer": {
                    "regions": ["na"],
                    "links": {"target": {"region": "eu"}},
                },
                "target": {
                    "regions": ["eu"],
                    "ports": {"internal": 5100, "expose": "tailnet"},
                },
            },
            "accessories": {},
        }
        assert _run_link_validator(data).failures == []

    def test_cross_region_link_target_with_ports_expose_host_passes(self):
        """`expose: host` is also a valid host binding for cross-region."""
        data = {
            "services": {
                "consumer": {
                    "regions": ["na"],
                    "links": {"target": {"region": "eu"}},
                },
                "target": {
                    "regions": ["eu"],
                    "ports": {"internal": 5100, "expose": "host"},
                },
            },
            "accessories": {},
        }
        assert _run_link_validator(data).failures == []

    def test_cross_region_link_to_accessory_with_expose_tailnet_passes(self):
        """Accessories use top-level `expose:` (M83 shape) — validator
        must look there, not at `ports.expose`."""
        data = {
            "services": {
                "consumer": {
                    "regions": ["na"],
                    "links": {"postgres": {"region": "eu"}},
                },
            },
            "accessories": {
                "postgres": {
                    "regions": ["eu"],
                    "port": "5432:5432",
                    "expose": "tailnet",
                },
            },
        }
        assert _run_link_validator(data).failures == []

    def test_cross_region_link_to_accessory_without_expose_fails(self):
        data = {
            "services": {
                "consumer": {
                    "regions": ["na"],
                    "links": {"postgres": {"region": "eu"}},
                },
            },
            "accessories": {
                "postgres": {
                    "regions": ["eu"],
                    "port": "5432:5432",
                },
            },
        }
        failures = _run_link_validator(data).failures
        assert len(failures) == 1
        assert "expose" in failures[0]
        assert "postgres" in failures[0]
        # Top-level expose path for accessories — NOT `ports.expose`
        assert "ports.expose" not in failures[0]

    def test_same_region_link_does_not_require_expose(self):
        """Same-region links use docker network DNS — no host binding needed,
        validator must not require ports.expose. (Note: same-region links
        are flagged by `validate_links` separately as a same-stack trap;
        this test just confirms the exposure validator doesn't pile on.)"""
        data = {
            "services": {
                "consumer": {
                    "regions": ["eu"],
                    "links": {"target": {"region": "eu"}},
                },
                "target": {
                    "regions": ["eu"],
                    "ports": {"internal": 5100},
                },
            },
            "accessories": {},
        }
        assert _run_link_validator(data).failures == []

    def test_link_to_missing_target_does_not_emit_exposure_error(self):
        """Missing-target errors are reported by validate_links — the
        exposure validator should stay silent (don't pile on errors that
        confuse the operator)."""
        data = {
            "services": {
                "consumer": {
                    "regions": ["na"],
                    "links": {"nonexistent": {"region": "eu"}},
                },
            },
            "accessories": {},
        }
        assert _run_link_validator(data).failures == []

    def test_no_links_no_failures(self):
        data = {
            "services": {
                "myapp": {
                    "regions": ["eu"],
                    "ports": {"internal": 5100},
                }
            },
            "accessories": {},
        }
        assert _run_link_validator(data).failures == []
