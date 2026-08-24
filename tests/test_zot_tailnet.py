"""Unit tests for the Zot tailnet self-reach fix (GitHub issue #27).

The control host running Zot must be able to push/pull its own registry
over the tailnet instead of hairpinning its own public IP — the hairpin
times out on large layer uploads. The fix has two independent halves:

  1. Traefik entrypoints: the zot router previously bound only
     `websecure`, so infra-originated pushes that resolve `zot_domain` to
     the tailnet IP hit a 404 (nothing listens for them on
     `websecure_tailnet`). Under `traefik_split_entrypoints` the router
     now binds `websecure,websecure_tailnet` (single `websecure`
     otherwise) in BOTH places that build this label:
       - `roles/deploy_stack/templates/_zot.j2` (legacy compose path)
       - `roles/container_lifecycle/tasks/build_specs.yml` (reconciler
         spec-builder path — see bay#14 in test_zot_storage.py for why
         both paths must stay in lockstep)
     A consumer can still force a specific set via group_vars
     `zot_entrypoints`, which always wins over the computed default.

  2. `/etc/hosts` self-pin: `roles/zot/tasks/main.yml` pins `zot_domain`
     to `zot_tailnet_pin_ip` (defaults to the adapter's `gateway_bind_ip`)
     on the control host only, healing stale/manual entries via a
     domain-anchored regexp, and removing the managed line again if the
     pin IP is ever unset.

Harness notes (a whitespace regression was already caught this way):

  - `_zot.j2` is rendered with `jinja2.Environment(trim_blocks=True,
    lstrip_blocks=False)` (ansible template-module defaults) via
    `helpers.make_ansible_env`. An earlier version of the fix had an
    INDENTED `{# #}` comment block which, under `trim_blocks=True`,
    leaked leading whitespace into the following line and produced
    YAML-invalid compose output. Every render test here round-trips the
    output through `yaml.safe_load` so a whitespace regression fails
    loudly instead of silently producing a broken compose file.
  - Only `ansible.plugins.filter.core.to_bool` is registered as the
    `bool` filter — never the full `FilterModule().filters()` dict,
    because ansible-core's `default` filter shadows Jinja's builtin and
    returns empty outside a real Templar (see test_zot_storage.py /
    test_build_specs_service_ports.py for the same convention).
  - `_zot.j2` references `{{ macros.logging_block({}) }}`, which only
    exists in the parent `docker-compose.yml.j2` include context. It is
    stripped out before standalone rendering here (mirrors
    `test_zot_storage.py::_render_compose_zot`).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml
from ansible.plugins.filter.core import to_bool

from helpers import make_ansible_env

_REPO_ROOT = Path(__file__).parent.parent
_ZOT_DEFAULTS = _REPO_ROOT / "roles" / "zot" / "defaults" / "main.yml"
_ZOT_TASKS = _REPO_ROOT / "roles" / "zot" / "tasks" / "main.yml"
_DEPLOY_STACK_TPL = _REPO_ROOT / "roles" / "deploy_stack" / "templates"
_BUILD_SPECS = (
    _REPO_ROOT / "roles" / "container_lifecycle" / "tasks" / "build_specs.yml"
)


def _defaults() -> dict:
    return yaml.safe_load(_ZOT_DEFAULTS.read_text())


def _tasks() -> list[dict]:
    return yaml.safe_load(_ZOT_TASKS.read_text())


def _task(name: str) -> dict:
    for task in _tasks():
        if task.get("name") == name:
            return task
    raise AssertionError(f"task {name!r} not found in {_ZOT_TASKS}")


def _render_compose_zot(**overrides) -> str:
    """Render the _zot.j2 snippet with a stub macros context.

    Mirrors `test_zot_storage.py::_render_compose_zot` — `_zot.j2`
    references `{{ macros.logging_block({}) }}` from the parent
    docker-compose template, so that line is stripped before rendering
    standalone.
    """
    env = make_ansible_env(_DEPLOY_STACK_TPL)
    env.filters["bool"] = to_bool
    src = (_DEPLOY_STACK_TPL / "_zot.j2").read_text()
    src = src.replace("{{ macros.logging_block({}) }}\n", "")
    template = env.from_string(src)
    base = {
        "zot_enabled": True,
        "zot_image": "ghcr.io/project-zot/zot:v2.1.15",
        "zot_config_dir": "/opt/zot/config",
        "zot_data_dir": "/opt/zot/data",
        "traefik_docker_network": "services",
        "zot_domain": "registry.example.com",
        "zot_port": 5000,
        "stack_dir": "/opt/teststack",
        "zot_storage_driver": "filesystem",
    }
    base.update(overrides)
    return template.render(**base)


def _entrypoints_label_line(rendered: str) -> str:
    for line in rendered.splitlines():
        if "traefik.http.routers.zot.entrypoints=" in line:
            return line
    raise AssertionError(
        f"entrypoints label not found in rendered output:\n{rendered}"
    )


# ── A. _zot.j2 entrypoints render ───────────────────────────────────


class TestZotComposeEntrypoints:
    def test_split_unset_defaults_to_websecure_only(self) -> None:
        rendered = _render_compose_zot()
        line = _entrypoints_label_line(rendered)
        assert line == '      - "traefik.http.routers.zot.entrypoints=websecure"', (
            "default (no traefik_split_entrypoints) must bind websecure "
            "only, at 6-space indent, byte-exact — got %r" % line
        )

    def test_split_true_bool_binds_both_entrypoints(self) -> None:
        rendered = _render_compose_zot(traefik_split_entrypoints=True)
        line = _entrypoints_label_line(rendered)
        assert line == (
            '      - "traefik.http.routers.zot.entrypoints='
            'websecure,websecure_tailnet"'
        )

    def test_split_true_string_binds_both_entrypoints(self) -> None:
        """group_vars YAML can hand this filter a string ('true'), not a
        real bool — the `| bool` cast must normalize it."""
        rendered = _render_compose_zot(traefik_split_entrypoints="true")
        line = _entrypoints_label_line(rendered)
        assert line == (
            '      - "traefik.http.routers.zot.entrypoints='
            'websecure,websecure_tailnet"'
        )

    def test_zot_entrypoints_override_wins_over_computed_default(self) -> None:
        rendered = _render_compose_zot(
            traefik_split_entrypoints=True,
            zot_entrypoints="websecure_tailnet",
        )
        line = _entrypoints_label_line(rendered)
        assert line == (
            '      - "traefik.http.routers.zot.entrypoints=websecure_tailnet"'
        )

    @pytest.mark.parametrize(
        "overrides",
        [
            {},
            {"traefik_split_entrypoints": True},
            {"traefik_split_entrypoints": "true"},
            {"zot_entrypoints": "websecure_tailnet"},
        ],
    )
    def test_rendered_snippet_is_valid_yaml(self, overrides: dict) -> None:
        """Regression guard: an earlier fix had an indented `{# #}` comment
        that, under trim_blocks=True, leaked whitespace into the following
        line and broke YAML parsing of the compose file. Round-trip every
        variant through yaml.safe_load so this can't silently regress."""
        rendered = _render_compose_zot(**overrides)
        parsed = yaml.safe_load("services:\n" + rendered)
        assert "zot" in parsed["services"]
        assert parsed["services"]["zot"]["labels"], (
            "labels list must survive the YAML round-trip"
        )

    def test_non_split_render_is_byte_identical_explicit_vs_default(self) -> None:
        """Backward-compat guarantee: omitting traefik_split_entrypoints
        entirely must render byte-for-byte the same as explicitly passing
        False — no consumer relying on the old unconditional `websecure`
        output should see any diff."""
        implicit = _render_compose_zot()
        explicit = _render_compose_zot(traefik_split_entrypoints=False)
        assert implicit == explicit


# ── B. build_specs.yml zot label contract ───────────────────────────


class TestBuildSpecsZotLabel:
    def _zot_task(self) -> dict:
        tasks = yaml.safe_load(_BUILD_SPECS.read_text())
        for task in tasks:
            if task.get("name") == "Build zot registry container spec":
                return task
        raise AssertionError(
            "'Build zot registry container spec' task not found in "
            f"{_BUILD_SPECS}"
        )

    def test_zot_entrypoints_default_var_present_and_correct_shape(self) -> None:
        task = self._zot_task()
        default_tpl = task["vars"]["_zot_entrypoints_default"]
        assert "websecure,websecure_tailnet" in default_tpl
        assert "traefik_split_entrypoints | default(false) | bool" in default_tpl

    def test_entrypoints_label_references_the_default_var(self) -> None:
        task = self._zot_task()
        label = task["vars"]["_zot_spec"]["labels"][
            "traefik.http.routers.zot.entrypoints"
        ]
        assert label == "{{ zot_entrypoints | default(_zot_entrypoints_default) }}"

    @pytest.mark.parametrize(
        "split_value,expected",
        [
            (False, "websecure"),
            (True, "websecure,websecure_tailnet"),
            ("true", "websecure,websecure_tailnet"),
        ],
    )
    def test_default_var_renders_expected_value(
        self, split_value, expected: str
    ) -> None:
        task = self._zot_task()
        default_tpl = task["vars"]["_zot_entrypoints_default"]
        env = make_ansible_env(_DEPLOY_STACK_TPL)
        env.filters["bool"] = to_bool
        rendered = env.from_string(default_tpl).render(
            traefik_split_entrypoints=split_value
        )
        assert rendered == expected

    def test_label_template_renders_with_and_without_override(self) -> None:
        """`zot_entrypoints` must be OMITTED (Jinja Undefined), not passed
        as None, to exercise the `default()` filter — `default` only
        substitutes for Undefined (or falsy with boolean=True), so an
        explicit `None` would render the literal string 'None'. This
        mirrors the real Ansible case: an unset group_vars key is
        Undefined in the template context, never a Python None."""
        task = self._zot_task()
        default_tpl = task["vars"]["_zot_entrypoints_default"]
        label_tpl = task["vars"]["_zot_spec"]["labels"][
            "traefik.http.routers.zot.entrypoints"
        ]
        env = make_ansible_env(_DEPLOY_STACK_TPL)
        env.filters["bool"] = to_bool

        # No override, split off -> computed default (websecure)
        default_off = env.from_string(default_tpl).render(
            traefik_split_entrypoints=False
        )
        label_off = env.from_string(label_tpl).render(
            traefik_split_entrypoints=False,
            _zot_entrypoints_default=default_off,
        )
        assert label_off == "websecure"

        # No override, split on -> computed default (both)
        default_on = env.from_string(default_tpl).render(
            traefik_split_entrypoints=True
        )
        label_on = env.from_string(label_tpl).render(
            traefik_split_entrypoints=True,
            _zot_entrypoints_default=default_on,
        )
        assert label_on == "websecure,websecure_tailnet"

        # Explicit override always wins regardless of split
        label_override = env.from_string(label_tpl).render(
            traefik_split_entrypoints=True,
            zot_entrypoints="websecure_tailnet",
            _zot_entrypoints_default=default_on,
        )
        assert label_override == "websecure_tailnet"


# ── C. /etc/hosts pin task contract ─────────────────────────────────


class TestZotHostsPinTaskShape:
    def test_pin_task_module_and_state(self) -> None:
        task = _task("Pin zot registry domain to tailnet IP in /etc/hosts")
        assert task["ansible.builtin.lineinfile"]["path"] == "/etc/hosts"
        assert task["ansible.builtin.lineinfile"]["state"] == "present"
        assert (
            task["ansible.builtin.lineinfile"]["line"]
            == "{{ zot_tailnet_pin_ip }} {{ zot_domain }}  "
            "# bay-managed: zot registry tailnet pin (see roles/zot)"
        )
        assert (
            task["ansible.builtin.lineinfile"]["regexp"]
            == r"^\S+\s+{{ zot_domain | regex_escape }}(\s.*)?$"
        )
        assert task["become"] is True
        assert task["become_user"] == "root"
        assert task["when"] == "zot_tailnet_pin_ip | length > 0"
        assert task["tags"] == ["zot"]

    def test_removal_task_module_and_state(self) -> None:
        task = _task("Remove bay-managed zot registry tailnet pin from /etc/hosts")
        assert task["ansible.builtin.lineinfile"]["path"] == "/etc/hosts"
        assert task["ansible.builtin.lineinfile"]["state"] == "absent"
        # Dual-read: a host pinned before the pre-1.0 rename still carries the
        # pre-1.0 marker, and its stale line must still be removable.
        assert (
            task["ansible.builtin.lineinfile"]["regexp"]
            == r"^.*# (argo|bay)-managed: zot registry tailnet pin.*$"  # legacy-argo: dual-read, remove in a future major release
        )
        assert task["become"] is True
        assert task["become_user"] == "root"
        assert task["when"] == "zot_tailnet_pin_ip | length == 0"
        assert task["tags"] == ["zot"]


class TestZotHostsPinRegexBehavior:
    """Simulate the lineinfile regexp/line semantics with Python `re`.

    `regex_escape` (ansible default type='python') is `re.escape`, so
    substituting `re.escape(zot_domain)` into the pattern reproduces
    exactly what Ansible evaluates on the target host.
    """

    _DOMAIN = "registry.example.com"

    def _pin_pattern(self) -> re.Pattern:
        task = _task("Pin zot registry domain to tailnet IP in /etc/hosts")
        regexp = task["ansible.builtin.lineinfile"]["regexp"]
        rendered = regexp.replace(
            "{{ zot_domain | regex_escape }}", re.escape(self._DOMAIN)
        )
        return re.compile(rendered)

    def _removal_pattern(self) -> re.Pattern:
        task = _task("Remove bay-managed zot registry tailnet pin from /etc/hosts")
        return re.compile(task["ansible.builtin.lineinfile"]["regexp"])

    def test_pin_matches_manual_existing_line(self) -> None:
        assert self._pin_pattern().match("100.64.0.5 registry.example.com")

    def test_pin_matches_its_own_managed_line_idempotent(self) -> None:
        managed = (
            "100.64.0.5 registry.example.com  "
            "# bay-managed: zot registry tailnet pin (see roles/zot)"
        )
        assert self._pin_pattern().match(managed)

    def test_pin_does_not_match_unrelated_domain(self) -> None:
        assert not self._pin_pattern().match("1.2.3.4 other.example.com")

    def test_pin_does_not_match_domain_as_second_alias(self) -> None:
        assert not self._pin_pattern().match("1.2.3.4 alias registry.example.com")

    def test_pin_does_not_match_domain_as_suffix_of_longer_host(self) -> None:
        """The escaped `.` must reject a superstring like
        `registry.example.com.evil.io` — `(\\s.*)?$` requires either end
        of string or whitespace immediately after the domain."""
        assert not self._pin_pattern().match(
            "100.64.0.5 registry.example.com.evil.io"
        )

    def test_removal_matches_managed_marker_line(self) -> None:
        managed = (
            "100.64.0.5 registry.example.com  "
            "# bay-managed: zot registry tailnet pin (see roles/zot)"
        )
        assert self._removal_pattern().match(managed)

    def test_removal_matches_the_legacy_marker_line(self) -> None:
        legacy = (
            "100.64.0.5 registry.example.com  "
            "# argo-managed: zot registry tailnet pin (see roles/zot)"  # legacy-argo: pre-1.0 marker
        )
        assert self._removal_pattern().match(legacy)

    def test_removal_does_not_match_bare_manual_line(self) -> None:
        assert not self._removal_pattern().match("100.64.0.5 registry.example.com")


# ── D. defaults contract ────────────────────────────────────────────


class TestZotTailnetPinDefault:
    def test_zot_tailnet_pin_ip_defaults_from_gateway_bind_ip(
        self,
    ) -> None:
        d = _defaults()
        assert "zot_tailnet_pin_ip" in d
        tpl = d["zot_tailnet_pin_ip"]
        assert "gateway_bind_ip" in tpl
        # Two-arg default: `default('')` alone only replaces an UNDEFINED
        # value, so an explicit None would render the string "None" straight
        # into the pin. The falsy form is what this guard actually means.
        assert "default('', true)" in tpl
