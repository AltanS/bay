"""reconcile.yml must render the reconciler tunables from the role defaults.

`container_lifecycle_stop_timeout` and the two healthcheck variables sat in the
role defaults for a long time while the bundle carried none of them, so the
reconciler ran on its built-in defaults and a consumer override did nothing.
This renders the real bundle expression from the task file, with the role's own
defaults file as variables, and loads the result through the real `load_bundle`.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import jinja2
import yaml

from bay_reconcile.bundle import load_bundle
from bay_reconcile.models import ReconcilerConfig

_ROLE = Path(__file__).parent.parent / "roles" / "container_lifecycle"


def _bundle_expression() -> str:
    def walk(items: list[dict]):
        for item in items:
            yield item
            for key in ("block", "rescue", "always"):
                yield from walk(item.get(key, []))

    tasks = yaml.safe_load((_ROLE / "tasks" / "reconcile.yml").read_text())
    task = next(
        t for t in walk(tasks) if str(t.get("name", "")).startswith("Write reconcile bundle")
    )
    return str(task["ansible.builtin.copy"]["content"])


def _render(overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    defaults = yaml.safe_load((_ROLE / "defaults" / "main.yml").read_text())
    variables = {k: v for k, v in defaults.items() if k.startswith("container_lifecycle_")}
    variables.update({"stack_name": "bay", "_reconcile_entries": []})
    variables.update(overrides or {})
    env = jinja2.Environment()
    # Ansible's `bool` and `to_nice_json` filters, reduced to what this uses.
    env.filters["bool"] = lambda v: str(v).lower() in ("1", "true", "yes")
    env.filters["to_nice_json"] = lambda v: json.dumps(v, indent=4)
    return json.loads(env.from_string(_bundle_expression()).render(**variables))


def test_role_defaults_render_the_reconciler_defaults() -> None:
    # The role defaults must equal the reconciler's built-ins, or this release
    # changes how deploys behave.
    assert load_bundle(_render()).config == ReconcilerConfig()


def test_each_tunable_comes_from_its_role_variable() -> None:
    rendered = _render(
        {
            "container_lifecycle_stop_timeout": "45",
            "container_lifecycle_healthcheck_timeout": "90",
            "container_lifecycle_healthcheck_poll": "0.5",
        }
    )
    assert rendered["config"] == {
        "stop_timeout": 45,
        "healthcheck_timeout": 90.0,
        "healthcheck_poll": 0.5,
    }
    assert load_bundle(rendered).config == ReconcilerConfig(
        stop_timeout=45, healthcheck_timeout=90.0, healthcheck_poll=0.5
    )
