"""`command:`/`entrypoint:` must survive the spec builder (GH bay#26).

Both keys are accepted on services AND accessories by services.schema.json,
but neither reached a running container:

  - accessories never emitted `command` or `entrypoint` at all, so an
    accessory that declares `command: ['sleep', 'infinity']` to stay alive ran
    the image's default CMD, exited 0, and crash-looped under
    `restart: unless-stopped`;
  - `entrypoint` was emitted by *nothing* — not the spec builder, not
    `_service.j2` — so it was silently dropped for services too.

The issue proposed fixing `_macros.j2`. That file renders
`docker-compose.yml`, which deploy_stack deploys as **documentation only**
(see the banner in docker-compose.yml.j2) — the reconciler, fed by
`build_specs.yml`, is the sole path that creates containers as of v0.97.0.
So these tests exercise the real `build_specs.yml` expressions: they parse the
task file itself rather than re-implementing the Jinja, so a future edit to
the builder cannot drift away from the guard.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml
from jinja2 import Environment

_REPO_ROOT = Path(__file__).resolve().parent.parent
_BUILD_SPECS = _REPO_ROOT / "roles" / "container_lifecycle" / "tasks" / "build_specs.yml"

_filter_dir = str(_REPO_ROOT / "filter_plugins")
if _filter_dir not in sys.path:
    sys.path.insert(0, _filter_dir)

from bay_filters import bay_prefix_volumes  # noqa: E402


def _combine(base, *others, recursive=False, list_merge="replace"):
    """Stand-in for Ansible's `combine` filter (shallow merge is enough here)."""
    result = dict(base or {})
    for other in others:
        if other:
            result.update(other)
    return result


def _optional_expr(task_name: str) -> str:
    """Pull the live `_optional` Jinja expression out of build_specs.yml."""
    tasks = yaml.safe_load(_BUILD_SPECS.read_text())
    for task in tasks:
        if task.get("name") == task_name:
            return task["vars"]["_optional"]
    raise AssertionError(f"task {task_name!r} not found in {_BUILD_SPECS}")


def _env() -> Environment:
    env = Environment(trim_blocks=True, lstrip_blocks=False)
    env.filters["combine"] = _combine
    env.filters["bay_prefix_volumes"] = bay_prefix_volumes
    env.filters["bay_healthcheck"] = lambda hc, port: dict(hc)
    env.filters["bay_log_rotation_spec"] = lambda v, d: {}
    return env


def _render_optional(task_name: str, **context) -> dict:
    """Render the `_optional` expression and eval it back into a dict.

    Ansible templates this string and its `{{ ... }}` result is a Python
    literal, which Ansible then native-types back into a dict.
    """
    expr = _optional_expr(task_name)
    rendered = _env().from_string(expr).render(**context)
    return eval(rendered, {"__builtins__": {}}, {})  # noqa: S307 - test-local literal


_ACCESSORY_TASK = "Build accessory container specs"
_SERVICE_TASK = "Build service container specs"


def _accessory_ctx(acc: dict) -> dict:
    return {
        "_acc": acc,
        "_name": "demo-backup",
        "stack_dir": "/opt/test",
        "stack_name": "test",
        "traefik_docker_network": "services",
        "_acc_port_rendered": "",
        "_log_rotation": {},
    }


def _service_ctx(svc: dict) -> dict:
    return {
        "_svc": svc,
        "_name": "demo",
        "stack_dir": "/opt/test",
        "stack_name": "test",
        "_svc_expose": "",
        "_svc_port_rendered": "",
        "_log_rotation": {},
    }


# ── Accessories: the reported bug ────────────────────────────────────────


def test_accessory_command_reaches_the_spec():
    """The issue's exact repro: an idle sidecar kept alive by `command:`."""
    spec = _render_optional(
        _ACCESSORY_TASK,
        **_accessory_ctx(
            {"image": "alpine:3.20", "command": ["sleep", "infinity"]},
        ),
    )
    assert spec.get("command") == ["sleep", "infinity"], (
        "accessory `command:` was dropped — the container runs the image's "
        "default CMD and crash-loops under restart: unless-stopped"
    )


def test_accessory_entrypoint_reaches_the_spec():
    spec = _render_optional(
        _ACCESSORY_TASK,
        **_accessory_ctx({"image": "alpine:3.20", "entrypoint": "/bin/custom"}),
    )
    assert spec.get("entrypoint") == "/bin/custom"


def test_accessory_string_command_preserved_verbatim():
    """The schema allows a string as well as an array; neither may be coerced."""
    spec = _render_optional(
        _ACCESSORY_TASK,
        **_accessory_ctx({"image": "redis:7", "command": "redis-server --appendonly yes"}),
    )
    assert spec.get("command") == "redis-server --appendonly yes"


# ── Services: entrypoint was dropped here too ────────────────────────────


def test_service_entrypoint_reaches_the_spec():
    """`entrypoint` was accepted by the schema but emitted by nothing."""
    spec = _render_optional(
        _SERVICE_TASK,
        **_service_ctx({"image": "app:1", "entrypoint": ["/app/init.sh"]}),
    )
    assert spec.get("entrypoint") == ["/app/init.sh"]


def test_service_command_still_reaches_the_spec():
    spec = _render_optional(
        _SERVICE_TASK,
        **_service_ctx({"image": "app:1", "command": ["serve", "--port", "80"]}),
    )
    assert spec.get("command") == ["serve", "--port", "80"]


# ── The 95% case: absent keys must stay absent ───────────────────────────
#
# An emitted `command: None` would both change the config_hash (recreating
# every container on upgrade) and override the image CMD with nothing.


@pytest.mark.parametrize("task,ctx", [
    (_ACCESSORY_TASK, _accessory_ctx({"image": "postgres:16"})),
    (_SERVICE_TASK, _service_ctx({"image": "app:1"})),
])
def test_absent_command_and_entrypoint_emit_no_key(task, ctx):
    spec = _render_optional(task, **ctx)
    assert "command" not in spec, "an absent command must not be emitted as a key"
    assert "entrypoint" not in spec, "an absent entrypoint must not be emitted as a key"
