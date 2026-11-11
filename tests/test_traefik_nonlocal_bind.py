"""`traefik_nonlocal_bind` lets Traefik bind the tailnet IP before tailscaled has it.

On a cold boot tailscaled has no address until it reaches Headscale. Traefik
(host network) then fails on `websecure_tailnet` and exits, which takes the
public 80/443 down too. The sysctl must be set before the config task.
"""

from __future__ import annotations

from pathlib import Path

import yaml

_ROLE = Path(__file__).parent.parent / "roles" / "traefik"
_FILE = "/etc/sysctl.d/99-bay-traefik-nonlocal-bind.conf"
_SET = "Allow Traefik to bind the tailnet address before tailscaled has it"
_REMOVE = "Remove the Traefik nonlocal-bind sysctl file when it is off"
_CONFIG = "Deploy Traefik static config"


def _tasks() -> list[dict]:
    return yaml.safe_load((_ROLE / "tasks" / "main.yml").read_text())


def _task(name: str) -> dict:
    return next(t for t in _tasks() if t["name"] == name)


def test_sysctl_task_persists_and_applies_now() -> None:
    sysctl = _task(_SET)["ansible.posix.sysctl"]
    assert sysctl["name"] == "net.ipv4.ip_nonlocal_bind"
    assert str(sysctl["value"]) == "1"
    assert sysctl["sysctl_file"] == _FILE
    assert sysctl["sysctl_set"] is True
    assert sysctl["reload"] is True


def test_tasks_run_as_root_explicitly() -> None:
    for name in (_SET, _REMOVE):
        task = _task(name)
        assert task["become"] is True
        assert task["become_user"] == "root"


def test_set_task_is_conditional_on_the_variable() -> None:
    assert _task(_SET)["when"] == "traefik_nonlocal_bind | bool"


def test_off_removes_the_file_and_does_not_touch_the_live_value() -> None:
    task = _task(_REMOVE)
    assert task["ansible.builtin.file"] == {"path": _FILE, "state": "absent"}
    assert task["when"] == "not (traefik_nonlocal_bind | bool)"
    assert "ansible.posix.sysctl" not in task


def test_sysctl_comes_before_the_traefik_config_task() -> None:
    names = [t["name"] for t in _tasks()]
    assert names.index(_SET) < names.index(_CONFIG)
    assert names.index(_REMOVE) < names.index(_CONFIG)


def test_default_follows_split_entrypoints() -> None:
    defaults = yaml.safe_load((_ROLE / "defaults" / "main.yml").read_text())
    value = defaults["traefik_nonlocal_bind"]
    assert "traefik_split_entrypoints" in value
    assert "bool" in value
    assert defaults["traefik_split_entrypoints"] is False


def test_default_renders_true_only_when_split_is_on() -> None:
    from jinja2 import Environment

    defaults = yaml.safe_load((_ROLE / "defaults" / "main.yml").read_text())
    env = Environment()
    env.filters["bool"] = bool  # Ansible's filter; plain bool is enough here
    template = env.from_string(defaults["traefik_nonlocal_bind"])
    assert template.render(traefik_split_entrypoints=True) == "True"
    assert template.render(traefik_split_entrypoints=False) == "False"
    assert template.render() == "False"
