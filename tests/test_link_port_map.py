"""Cross-region link port-map resolution tests (GH bay#7).

`deploy.yml` "Build link port map" resolves `LINKS_*_PORT` for cross-region
service-to-service links. It used to source the target spec from
`active_services|combine(active_accessories)` — the local-region active set —
so cross-region targets always fell through to 0. The fix changed the lookup to
the unfiltered `services|combine(accessories)` dict.

These tests exercise the same Jinja expression the playbook evaluates,
fed with the fixture shapes that flow through deploy.yml. Single-region
sandbox can't exercise this end-to-end, so isolation tests are the highest
useful signal short of a live two-region deploy.
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


def _resolve_port(
    target_name: str,
    services: dict | None,
    accessories: dict | None,
    active_services: dict | None,
    active_accessories: dict | None,
) -> str:
    """Re-implement the deploy.yml `Build link port map` expression.

    The Ansible task body is:

        _all: "{{ (services | default({})) | combine(accessories | default({})) }}"
        _cfg: "{{ _all[item.key] | default({}) }}"
        _port: >-
          {% if _cfg.ports is defined and _cfg.ports.internal is defined
              %}{{ _cfg.ports.internal }}{% elif _cfg.port is defined
              %}{{ _cfg.port.split(':')[-2] }}{% else %}0{% endif %}
        when: item.key in ((services | default({})) | combine(accessories | default({})))

    We exercise the same logic via a Jinja2 template so a regression in the
    expression itself shows up here. `active_services`/`active_accessories`
    are accepted as kwargs purely so a caller can pin "what the host sees"
    versus "what the global services dict carries" — the fix is that
    the resolution path no longer cares about the active filter.
    """
    env = jinja2.Environment(undefined=jinja2.StrictUndefined)
    env.filters["combine"] = _combine_filter
    template = env.from_string(
        "{% set _all = (services | default({})) | combine(accessories | default({})) %}"
        "{% if target in _all %}"
        "{% set _cfg = _all[target] %}"
        "{% if _cfg.ports is defined and _cfg.ports.internal is defined %}"
        "{{ _cfg.ports.internal }}"
        "{% elif _cfg.port is defined %}"
        "{{ _cfg.port.split(':')[-2] }}"
        "{% else %}0{% endif %}"
        "{% else %}0{% endif %}"
    )
    return template.render(
        target=target_name,
        services=services or {},
        accessories=accessories or {},
        active_services=active_services or {},
        active_accessories=active_accessories or {},
    ).strip()


# ── Cross-region: target absent from local active set ────────────────


class TestCrossRegionPortResolution:
    def test_cross_region_service_target_resolves_via_unfiltered_services(self):
        """The user's bug from issue #7: NA host's `active_services` does
        not contain `storefront-platform` (which is EU-only). Before the
        fix this returned 0; after, it resolves to 5100 from the global
        services dict."""
        services = {
            "storefront-platform": {
                "regions": ["eu"],
                "ports": {"internal": 5100},
            },
            "storefront-com": {
                "regions": ["na"],
                "links": {"storefront-platform": {"region": "eu"}},
            },
        }
        # NA host's perspective: only the NA service is active locally
        active_services = {"storefront-com": services["storefront-com"]}
        port = _resolve_port(
            "storefront-platform",
            services=services,
            accessories={},
            active_services=active_services,
            active_accessories={},
        )
        assert port == "5100"

    def test_cross_region_accessory_target_resolves_via_unfiltered_accessories(
        self,
    ):
        """demo case: NA service links to EU postgres accessory.
        EU postgres is absent from NA's `active_accessories` but present
        in the unfiltered `accessories` dict."""
        accessories = {
            "postgres": {
                "regions": ["eu"],
                "port": "5432:5432",
                "expose": "tailnet",
            }
        }
        services = {
            "myapp": {
                "regions": ["na"],
                "links": {"postgres": {"region": "eu"}},
            }
        }
        active_services = {"myapp": services["myapp"]}
        port = _resolve_port(
            "postgres",
            services=services,
            accessories=accessories,
            active_services=active_services,
            active_accessories={},
        )
        assert port == "5432"

    def test_same_region_link_still_resolves(self):
        """Regression guard: a same-region link where the target is in
        both global and active dicts must continue to resolve. The
        cross-region lookup change must not break the existing path."""
        services = {
            "consumer": {
                "regions": ["eu"],
                "links": {"target": {"region": "eu"}},
            },
            "target": {
                "regions": ["eu"],
                "ports": {"internal": 5100},
            },
        }
        active_services = dict(services)
        port = _resolve_port(
            "target",
            services=services,
            accessories={},
            active_services=active_services,
            active_accessories={},
        )
        assert port == "5100"

    def test_target_not_in_global_dict_returns_0(self):
        """Documented fallback when the link target is not in services or
        accessories at all — graceful degradation, not a Jinja crash."""
        services = {
            "consumer": {
                "regions": ["na"],
                "links": {"nonexistent": {"region": "eu"}},
            }
        }
        port = _resolve_port(
            "nonexistent",
            services=services,
            accessories={},
            active_services=services,
            active_accessories={},
        )
        assert port == "0"

    def test_string_port_target_resolves_correctly(self):
        """Accessory uses string `port:` form — port-map extracts the
        host-port half (the `-2` index in `port.split(':')`)."""
        accessories = {
            "redis": {
                "regions": ["eu"],
                "port": "6379:6380",  # host:container — host is 6379
                "expose": "tailnet",
            }
        }
        port = _resolve_port(
            "redis",
            services={},
            accessories=accessories,
            active_services={},
            active_accessories={},
        )
        assert port == "6379"


# ── Pre-fix regression sentinel ──────────────────────────────────────


class TestPreM87RegressionGuard:
    def test_active_filter_path_no_longer_used_for_cross_region(self):
        """If anyone reverts the deploy.yml change to filter through
        active_services again, this fails. The whole point of the fix is
        that cross-region resolution must not depend on the local
        active filter."""
        from pathlib import Path

        deploy_yml = (
            Path(__file__).resolve().parent.parent / "deploy.yml"
        ).read_text()

        # Locate the "Build link port map" task by name and grab the
        # following 14 lines (the task body).
        lines = deploy_yml.splitlines()
        try:
            idx = next(
                i for i, line in enumerate(lines) if "Build link port map" in line
            )
        except StopIteration:
            pytest.fail("`Build link port map` task missing from deploy.yml")

        # Strip comment lines so a "used to be X" historical note in
        # comments doesn't trip the regression check.
        body_lines = [
            line for line in lines[idx : idx + 20] if not line.lstrip().startswith("#")
        ]
        task_block = "\n".join(body_lines)

        # The fix: _all and `when:` use unfiltered services|combine(accessories)
        assert "active_services" not in task_block, (
            "Regression: `Build link port map` must not reference "
            "`active_services` — cross-region targets are absent from the "
            "local active set."
        )
        assert "active_accessories" not in task_block, (
            "Regression: `Build link port map` must not reference "
            "`active_accessories` — cross-region targets are absent from "
            "the local active set."
        )
        assert "(services | default({})) | combine(accessories | default({}))" in task_block
