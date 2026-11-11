"""The nightly docker prune must not delete volumes unless asked to.

`docker system prune -af --volumes` deletes every volume that no container
uses at that moment. After a failed deploy the data volume of a service has
no container, so the next prune removed its data. `docker_prune_volumes`
(default false) now gates the flag.
"""

from __future__ import annotations

from pathlib import Path

import jinja2
import yaml

_REPO_ROOT = Path(__file__).parent.parent
_CRONJOBS = _REPO_ROOT / "roles" / "cronjobs"


def _defaults() -> dict:
    return yaml.safe_load((_CRONJOBS / "defaults" / "main.yml").read_text())


def _job(**variables) -> str:
    tasks = yaml.safe_load((_CRONJOBS / "tasks" / "main.yml").read_text())
    task = next(t for t in tasks if t.get("name") == "Schedule Docker system prune")
    env = jinja2.Environment()
    env.filters["bool"] = lambda v: str(v).lower() in ("true", "1", "yes")
    ctx = {"docker_prune_volumes": _defaults()["docker_prune_volumes"], **variables}
    return " ".join(env.from_string(task["ansible.builtin.cron"]["job"]).render(ctx).split())


def test_volumes_are_not_pruned_by_default() -> None:
    assert _defaults()["docker_prune_volumes"] is False
    assert _job() == "docker system prune -af >> /var/log/docker-prune.log 2>&1"
    assert "--volumes" not in _job()


def test_volumes_are_pruned_when_the_operator_asks() -> None:
    assert _job(docker_prune_volumes=True) == (
        "docker system prune -af --volumes >> /var/log/docker-prune.log 2>&1"
    )


def test_the_job_still_logs_and_runs_as_root() -> None:
    tasks = yaml.safe_load((_CRONJOBS / "tasks" / "main.yml").read_text())
    cron = next(t for t in tasks if t.get("name") == "Schedule Docker system prune")
    assert cron["ansible.builtin.cron"]["user"] == "root"
    assert "docker-prune.log" in cron["ansible.builtin.cron"]["job"]


def test_the_defaults_explain_the_volume_risk_and_the_leak() -> None:
    text = (_CRONJOBS / "defaults" / "main.yml").read_text()
    assert "anonymous volume" in text
    assert "failed" in text
