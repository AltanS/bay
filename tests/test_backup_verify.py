"""Every configured backup target must have its script and a running timer.

A first `bay up` that failed used to leave a box with backup targets in
services.yml but no script and no timer, and nothing said so: the plan's
`backup` step appeared once, and only that step made `bay up` run the backup
role. Since 2.23.2 `bay up` runs the backup role on every run, and
roles/backup/tasks/verify.yml (run by deploy.yml after the roles, tags
deploy_stack and backup) fails the run when a target of the host has no
script or no enabled, active timer.

The check runs in a real ansible-playbook on localhost (include_role backup,
tasks_from verify). A fake `systemctl` answers the systemd module's `show`.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent

# The systemd module reads a unit with `systemctl show <unit>`. FAKE_TIMERS maps
# a unit to [UnitFileState, ActiveState]; any other unit is not found.
FAKE_SYSTEMCTL = r"""#!/usr/bin/env python3
import json, os, sys
args = [a for a in sys.argv[1:] if not a.startswith("--")]
timers = json.loads(os.environ.get("FAKE_TIMERS", "{}"))
if args[:1] == ["show"]:
    unit = args[1]
    if unit in timers:
        ufs, active = timers[unit]
        print(f"Id={unit}\nNames={unit}\nLoadState=loaded\nUnitFileState={ufs}\nActiveState={active}\nSubState=waiting")
    else:
        print(f"Id={unit}\nNames={unit}\nLoadState=not-found\nUnitFileState=\nActiveState=inactive\nSubState=dead")
    sys.exit(0)
if args[:1] in (["is-active"], ["is-enabled"], ["list-unit-files"], ["list-units"]):
    sys.exit(4)
sys.exit(0)
"""

VARS: dict[str, Any] = {
    "backup_enabled": True,
    "stack_name": "bay",
    "secrets": {},
    "access_gateway": "none",
    "active_services": {"shop": {"image": "example/shop"}},
    "active_accessories": {
        "postgres": {"image": "postgres:17", "backup": {"method": "pg_dump"}},
        "cache": {"image": "redis:7"},
    },
    "volume_backups": {
        "shop-data": {"container": "shop", "path": "/data"},
        # Its container runs on another host: not a target here.
        "blog-data": {"container": "blog", "path": "/data"},
    },
}


def _run(
    tmp_path: Path,
    *,
    scripts: list[str] = (),
    timers: dict[str, list[str]] | None = None,
    check: bool = False,
    extra: dict[str, Any] | None = None,
) -> subprocess.CompletedProcess[str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    (bin_dir / "systemctl").write_text(FAKE_SYSTEMCTL)
    (bin_dir / "systemctl").chmod(0o755)
    backup_dir = tmp_path / "opt" / "backup"
    backup_dir.mkdir(parents=True, exist_ok=True)
    for name in scripts:
        (backup_dir / f"backup-{name}.sh").write_text("#!/bin/sh\n")
    play = [{
        "hosts": "localhost", "connection": "local", "gather_facts": False,
        "tasks": [{
            "name": "Check that every backup target has a script and a running timer",
            "ansible.builtin.include_role": {"name": "backup", "tasks_from": "verify"},
        }],
    }]
    (tmp_path / "play.yml").write_text(yaml.safe_dump(play))
    (tmp_path / "vars.json").write_text(json.dumps({
        **VARS,
        "stack_dir": str(tmp_path / "opt"),
        "backup_scripts_dir": str(backup_dir),
        **(extra or {}),
    }))
    (tmp_path / "ansible.cfg").write_text("[defaults]\n")
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "FAKE_TIMERS": json.dumps(timers or {}),
        "ANSIBLE_CONFIG": str(tmp_path / "ansible.cfg"),
        # resolve.yml uses the playbook-level filters (bay_backup_max_age).
        "ANSIBLE_FILTER_PLUGINS": str(ROOT / "filter_plugins"),
        "ANSIBLE_ROLES_PATH": str(ROOT / "roles"),
        "ANSIBLE_NOCOLOR": "1",
        "ANSIBLE_LOCALHOST_WARNING": "0",
        "ANSIBLE_INVENTORY_UNPARSED_WARNING": "0",
        "ANSIBLE_PYTHON_INTERPRETER": sys.executable,
    }
    cmd = [sys.executable, "-m", "ansible.cli.playbook", "-i", "localhost,",
           "-e", f"@{tmp_path / 'vars.json'}", str(tmp_path / "play.yml")]
    if check:
        cmd.append("--check")
    return subprocess.run(cmd, cwd=tmp_path, env=env, capture_output=True, text=True,
                          timeout=300)


ALL_UP = {
    "bay-backup@postgres.timer": ["enabled", "active"],
    "bay-backup@bay_shop-data.timer": ["enabled", "active"],
}


def test_a_configured_target_with_no_script_and_no_timer_fails_the_run(tmp_path: Path) -> None:
    proc = _run(tmp_path)
    out = proc.stdout + proc.stderr
    assert proc.returncode != 0, out[-3000:]
    assert "are configured but not installed" in out
    assert "postgres (no script, timer not enabled)" in out
    assert "bay_shop-data (no script, timer not enabled)" in out
    # Not a target of this host: its container runs elsewhere. Not backed up: cache.
    assert "bay_blog-data" not in out and "cache (" not in out


def test_every_target_installed_passes(tmp_path: Path) -> None:
    proc = _run(tmp_path, scripts=["postgres", "bay_shop-data"], timers=ALL_UP)
    assert proc.returncode == 0, (proc.stdout + proc.stderr)[-3000:]
    assert "not installed" not in proc.stdout


def test_an_inactive_or_disabled_timer_fails(tmp_path: Path) -> None:
    timers = {
        "bay-backup@postgres.timer": ["enabled", "inactive"],
        "bay-backup@bay_shop-data.timer": ["disabled", "inactive"],
    }
    proc = _run(tmp_path, scripts=["postgres", "bay_shop-data"], timers=timers)
    out = proc.stdout + proc.stderr
    assert proc.returncode != 0
    assert "postgres (timer not active)" in out
    assert "bay_shop-data (timer not enabled)" in out


def test_a_missing_script_alone_fails(tmp_path: Path) -> None:
    proc = _run(tmp_path, scripts=["postgres"], timers=ALL_UP)
    out = proc.stdout + proc.stderr
    assert proc.returncode != 0
    assert "bay_shop-data (no script)" in out
    assert "postgres (" not in out


def test_check_mode_warns_and_does_not_fail(tmp_path: Path) -> None:
    proc = _run(tmp_path, check=True)
    assert proc.returncode == 0, (proc.stdout + proc.stderr)[-3000:]
    assert "Check mode: backup targets of localhost with no script" in proc.stdout


def test_backups_off_checks_nothing(tmp_path: Path) -> None:
    proc = _run(tmp_path, extra={"backup_enabled": False})
    assert proc.returncode == 0, (proc.stdout + proc.stderr)[-3000:]
    assert "not installed" not in proc.stdout


def _app_play() -> dict[str, Any]:
    plays = yaml.safe_load((ROOT / "deploy.yml").read_text())
    return next(p for p in plays if any(
        isinstance(r, dict) and r.get("role") == "backup" for r in p.get("roles", [])
    ))


def test_deploy_runs_the_check_after_the_roles_on_deploy_stack_and_backup() -> None:
    play = _app_play()
    task = next(
        t for t in play["post_tasks"]
        if t.get("name") == "Check that every backup target has a script and a running timer"
    )
    assert task["ansible.builtin.include_role"]["name"] == "backup"
    assert task["ansible.builtin.include_role"]["tasks_from"] == "verify"
    assert set(task["tags"]) == {"deploy_stack", "backup"}
    assert set(task["ansible.builtin.include_role"]["apply"]["tags"]) == {"deploy_stack", "backup"}
    # The first post task: the backup role (the last role) installed first.
    assert play["post_tasks"][0] is task
    assert [r["role"] for r in play["roles"]][-1] == "backup"


def test_bay_up_always_runs_the_backup_role() -> None:
    from bay_cli import apply, routes

    assert apply.UP_DEPLOY_TAGS.split(",") == ["deploy_stack", "backup"]
    # No step at all, as on every bay up after a failed first one.
    assert "backup" in routes.deploy_tags([], apply.UP_DEPLOY_TAGS).split(",")
    assert routes.deploy_tags([{"kind": "backup"}], apply.UP_DEPLOY_TAGS) == apply.UP_DEPLOY_TAGS
