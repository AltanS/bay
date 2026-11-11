"""A `--check` run must not fail on a timer whose unit file does not exist yet.

`bay provision <env> --tags outbound_monitor -- --check` died on a host that
had no `bay-reboot-alert.timer` yet: check mode does not write the unit file,
so the systemd module could not find the unit ("Could not find the requested
service"). Every timer that is enabled right after its unit file task now
skips in check mode when that file task reports changed.

The tests run the real tasks with ansible-playbook on localhost in check mode.
The unit file goes to a scratch folder, so it is always "changed" there, which
is the state of a new host. The unit names do not exist on this machine.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

from helpers import run_role_tasks

ROOT = Path(__file__).resolve().parent.parent
ROLES = ROOT / "roles"

OUTBOUND = (ROLES / "outbound_monitor", ROLES / "outbound_monitor" / "tasks" / "main.yml")

# (id, role, tasks file, unit task, enable/disable tasks, vars)
CASES = [
    ("outbound-check-on", OUTBOUND, "Deploy outbound check systemd timer unit",
     ["Enable and start outbound check timer"],
     {"outbound_check_enabled": True}),
    ("outbound-check-off", OUTBOUND, "Deploy outbound check systemd timer unit",
     ["Stop, disable and mask outbound check timer"],
     {"outbound_check_enabled": False}),
    ("disk-alert-on", OUTBOUND, "Deploy disk alert systemd timer unit",
     ["Enable and start disk alert timer"],
     {"disk_alert_enabled": True, "disk_alert_interval": "15min"}),
    ("disk-alert-off", OUTBOUND, "Deploy disk alert systemd timer unit",
     ["Stop, disable and mask disk alert timer"],
     {"disk_alert_enabled": False, "disk_alert_interval": "15min"}),
    ("reboot-alert-on", OUTBOUND, "Deploy reboot alert systemd timer unit",
     ["Enable and start reboot alert timer"],
     {"reboot_alert_enabled": True}),
    ("reboot-alert-off", OUTBOUND, "Deploy reboot alert systemd timer unit",
     ["Stop, disable and mask reboot alert timer"],
     {"reboot_alert_enabled": False}),
    ("backup-timers",
     (ROLES / "backup", ROLES / "backup" / "tasks" / "main.yml"),
     "Deploy backup timer units", ["Enable and start backup timers"],
     {"backup_enabled": True,
      "_backup_accessories": [{"name": "bay-nonexistent-one", "schedule": "0 3 * * *"}]}),
    ("backup-maintenance-timers",
     (ROLES / "backup", ROLES / "backup" / "tasks" / "main.yml"),
     "Deploy maintenance timer units", ["Enable and start maintenance timers"],
     {"backup_enabled": True,
      "_backup_accessories": [{"name": "bay-nonexistent-one", "schedule": "0 3 * * *"}]}),
    ("trigger-watchdog",
     (ROLES / "git_deploy", ROLES / "git_deploy" / "tasks" / "systemd.yml"),
     "Deploy trigger stall watchdog timer unit",
     ["Enable and start trigger stall watchdog timer"],
     {"git_deploy_stall_watchdog_enabled": True}),
    ("crowdsec-data-refresh",
     (ROLES / "crowdsec", ROLES / "crowdsec" / "tasks" / "main.yml"),
     "Deploy CrowdSec data refresh systemd timer unit",
     ["Enable and start CrowdSec data refresh timer"],
     {"crowdsec_enabled": True, "crowdsec_data_refresh_enabled": True}),
]


def _transform(role: Path, scratch: Path, drop_guard: bool):
    def go(task: dict) -> dict:
        module = next(k for k in task if k.startswith("ansible.builtin."))
        args = task[module]
        if module == "ansible.builtin.template":
            args = dict(args)
            args["src"] = str(role / "templates" / args["src"])
            args["dest"] = args["dest"].replace("/etc/systemd/system", str(scratch))
            args.pop("owner", None)
            args.pop("group", None)
            task[module] = args
        task.pop("become", None)
        task.pop("become_user", None)
        task.pop("notify", None)
        task["vars"] = {**task.get("vars", {}), "ansible_managed": "managed"}
        if "when" in task:
            when = task["when"] if isinstance(task["when"], list) else [task["when"]]
            if drop_guard:
                when = [w for w in when if "ansible_check_mode" not in str(w)]
            task["when"] = when
        return task
    return go


def _run(tmp_path, case, drop_guard=False):
    _id, (role, tasks), unit_task, follow, variables = case
    scratch = tmp_path / "units"
    scratch.mkdir(exist_ok=True)
    defaults = yaml.safe_load((role / "defaults" / "main.yml").read_text()) or {}
    return run_role_tasks(
        tmp_path, tasks, [unit_task, *follow], {"stack_dir": "/opt/bay-test", "app_user": "bay", **defaults, **variables},
        check=True, transform=_transform(role, scratch, drop_guard),
    )


@pytest.mark.parametrize("case", CASES, ids=[c[0] for c in CASES])
def test_check_mode_skips_the_enable_task_for_a_new_unit(tmp_path, case):
    proc = _run(tmp_path, case)
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out[-3000:]
    assert "Could not find the requested service" not in out
    assert "failed=0" in proc.stdout
    assert "skipped=1" in proc.stdout, out[-2000:]


@pytest.mark.skipif(shutil.which("systemctl") is None, reason="needs systemctl")
@pytest.mark.parametrize("case", CASES, ids=[c[0] for c in CASES])
def test_control_without_the_guard_the_check_run_fails(tmp_path, case):
    """Pin the bug: with the guard removed the same run fails."""
    proc = _run(tmp_path, case, drop_guard=True)
    assert proc.returncode != 0
    assert "Could not find the requested service" in proc.stdout + proc.stderr


@pytest.mark.parametrize("case", CASES, ids=[c[0] for c in CASES])
def test_every_enable_task_names_the_check_mode_guard(case):
    _id, (_role, tasks), _unit, follow, _vars = case
    loaded = {t["name"]: t for t in yaml.safe_load(tasks.read_text())}
    for name in follow:
        assert "ansible_check_mode" in str(loaded[name]["when"]), name
