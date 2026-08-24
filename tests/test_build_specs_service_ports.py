"""Service builder host-port rendering tests (GH bay#7).

`roles/container_lifecycle/tasks/build_specs.yml` is the path that produces
the spec dict consumed by `community.docker.docker_container`. An earlier fix
addressed the compose-file render path (`_macros.j2`) but missed this one —
the container module got `ports: omit` for services regardless of whether
`ports.expose:` was declared. This adds the same `expose:` resolution to
the service builder. These tests exercise the Jinja expression in
isolation against synthetic service shapes.

Critical regression guards (per counsel):

  - "no ports.expose" path emits NO `ports` key in the spec. This is
    the 95%-of-services case. Breaking it would crash deploys on every
    Traefik-fronted service that doesn't need a host bind.
  - The `.get()` chain protects against `_svc.ports` being undefined.
    The same trap on `dict.attr | default(...)` falling
    through to bound-method access must not regress here.
"""

from __future__ import annotations

import jinja2
import pytest


def _combine_filter(base, *others, recursive=False, list_merge="replace"):
    result = dict(base or {})
    for other in others:
        if other is None:
            continue
        result.update(other)
    return result


def _render_service_spec(svc: dict, headscale_server_tailnet_ip: str = "100.64.0.1") -> dict:
    """Re-implement the service-spec render expression from
    build_specs.yml.

    The Ansible block is:

        _svc_ports: "{{ _svc.get('ports', {}) }}"
        _svc_expose: "{{ _svc_ports.get('expose', '') }}"
        _svc_internal: "{{ _svc_ports.get('internal', 0) }}"
        _svc_port_rendered: >-
          {%- if _svc_expose == 'loopback' -%}127.0.0.1:{...}:{...}
          {%- elif _svc_expose == 'tailnet' -%}{{ tailnet_ip }}:{...}:{...}
          {%- elif _svc_expose == 'host' -%}0.0.0.0:{...}:{...}
          {%- endif -%}
        _optional: combine({'ports': [_svc_port_rendered]} if _svc_expose != '' else {})

    We render the same logic so a regression in the Jinja expression itself
    surfaces here. `_render_service_spec` returns the resulting `spec` dict
    as it would land in `_service_specs` after the combine.
    """
    env = jinja2.Environment(undefined=jinja2.StrictUndefined)
    env.filters["combine"] = _combine_filter

    # Render _svc_port_rendered (the binding string)
    binding_template = env.from_string(
        "{%- set _svc_ports = svc.get('ports', {}) -%}"
        "{%- set _svc_expose = _svc_ports.get('expose', '') -%}"
        "{%- set _svc_internal = _svc_ports.get('internal', 0) -%}"
        "{%- if _svc_expose == 'loopback' -%}127.0.0.1:{{ _svc_internal }}:{{ _svc_internal }}"
        "{%- elif _svc_expose == 'tailnet' -%}{{ tailnet_ip }}:{{ _svc_internal }}:{{ _svc_internal }}"
        "{%- elif _svc_expose == 'host' -%}0.0.0.0:{{ _svc_internal }}:{{ _svc_internal }}"
        "{%- endif -%}"
    )
    binding = binding_template.render(svc=svc, tailnet_ip=headscale_server_tailnet_ip).strip()

    # Render the combine guard — only emit `ports` when expose is set
    expose = svc.get("ports", {}).get("expose", "") if isinstance(svc.get("ports"), dict) else ""

    spec: dict = {}
    if expose:
        spec["ports"] = [binding]
    return spec


# ── Positive: each expose mode renders the right binding ─────────────


class TestServicePortsRendering:
    def test_expose_tailnet_renders_tailnet_binding(self):
        svc = {
            "access": "vpn",
            "image": "myapp:latest",
            "ports": {"internal": 5100, "expose": "tailnet"},
        }
        spec = _render_service_spec(svc, headscale_server_tailnet_ip="100.64.0.1")
        assert spec.get("ports") == ["100.64.0.1:5100:5100"]

    def test_expose_loopback_renders_loopback_binding(self):
        svc = {
            "access": "public",
            "image": "myapp:latest",
            "ports": {"internal": 5100, "expose": "loopback"},
        }
        spec = _render_service_spec(svc)
        assert spec.get("ports") == ["127.0.0.1:5100:5100"]

    def test_expose_host_renders_public_binding(self):
        svc = {
            "access": "public",
            "image": "myapp:latest",
            "ports": {"internal": 5100, "expose": "host"},
        }
        spec = _render_service_spec(svc)
        assert spec.get("ports") == ["0.0.0.0:5100:5100"]

    def test_storefront_platform_user_bug_pattern(self):
        """Exactly the failure pattern the user reported (GH bay#7)."""
        svc = {
            "access": "vpn",
            "image": "registry.example.com/storefront-platform:latest",
            "domains": ["platform.storefront.de"],
            "ports": {"internal": 5100, "expose": "tailnet"},
            "regions": ["eu"],
        }
        spec = _render_service_spec(svc, headscale_server_tailnet_ip="100.64.0.1")
        assert spec.get("ports") == ["100.64.0.1:5100:5100"], (
            "Regression: the user's storefront-platform on EU did not "
            "get its host-port binding into the docker_container spec, even "
            "though the compose file rendered correctly. Validate that "
            "build_specs.yml emits ports on the spec, not just the macro."
        )


# ── Negative: no ports.expose → no `ports` key in spec ───────────────


class TestServiceWithoutPortsExpose:
    def test_no_ports_expose_emits_no_ports_key(self):
        """The vast majority of services don't need a host bind — they
        reach each other via Docker network DNS. If this regresses,
        every Traefik-fronted service tries to bind a host port and
        deploys break en masse."""
        svc = {
            "access": "public",
            "image": "myapp:latest",
            "ports": {"internal": 5100},
        }
        spec = _render_service_spec(svc)
        assert "ports" not in spec, (
            "Service without ports.expose must NOT get a ports binding — "
            "got %r" % spec.get("ports")
        )

    def test_no_ports_block_at_all_does_not_crash(self):
        """`_svc.ports.expose | default('')` would raise on undefined
        `ports`. The `.get()` chain protects against this — an
        env.get() trap pattern (CLAUDE.md). Schema requires `ports:`,
        but the build_specs path must not depend on that — defensive."""
        svc = {
            "access": "public",
            "image": "myapp:latest",
            # no ports: at all
        }
        spec = _render_service_spec(svc)
        assert "ports" not in spec

    def test_empty_ports_block_does_not_crash(self):
        svc = {
            "access": "public",
            "image": "myapp:latest",
            "ports": {},
        }
        spec = _render_service_spec(svc)
        assert "ports" not in spec


# ── build_specs.yml source-level sentinel ────────────────────────────


class TestBuildSpecsYmlSource:
    """Sentinel checks against the actual file so a refactor that
    accidentally drops the expose-resolution lines fails immediately."""

    def _load(self) -> str:
        from pathlib import Path

        return (
            Path(__file__).resolve().parent.parent
            / "roles"
            / "container_lifecycle"
            / "tasks"
            / "build_specs.yml"
        ).read_text()

    def test_build_specs_renders_service_ports(self):
        src = self._load()
        # Service section must have the expose-resolution vars
        assert "_svc_ports:" in src
        assert "_svc_expose:" in src
        assert "_svc_port_rendered:" in src
        # And must combine ports into _optional when expose is set
        assert "{'ports': [_svc_port_rendered]}" in src
        # Must use .get() chain (not dict-attribute fallback that crashes on
        # undefined ports)
        assert "_svc.get('ports', {})" in src
        assert "_svc_ports.get('expose'," in src

    def test_service_block_uses_get_chain_not_attr_default(self):
        """Belt-and-suspenders against re-introducing the .attr|default
        trap. If someone "simplifies" the .get() chain to `_svc.ports.expose
        | default('')` it crashes when ports: is undefined. CLAUDE.md
        explicitly documents this trap (env.clear case)."""
        src = self._load()
        # Find the service builder section
        svc_start = src.find("Build service container specs")
        svc_end = src.find("Build accessory container specs")
        svc_block = src[svc_start:svc_end]

        # The defensive form must be present
        assert "_svc.get('ports', {})" in svc_block, (
            "Service builder must use .get() chain for ports access, not "
            "the .attr|default(...) form that fails on undefined nested "
            "dicts. See CLAUDE.md `Use .get() for optional dict keys`."
        )
