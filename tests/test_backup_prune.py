"""The backup role removes a target it installed that left the config (2.24.0).

Before 2.24.0 a volume set to `backup = false` kept its timer and its nightly
run (docs/backups.md said so). Now the role compares the manifest of its last
run (targets.json) with the targets of this run and tears down only names the
manifest recorded: a script someone else put there is never touched. The tasks
are lifted from roles/backup/tasks/main.yml and run in a real ansible-playbook
on localhost, with a fake systemctl on PATH.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
MAIN = ROOT / "roles" / "backup" / "tasks" / "main.yml"

LIFTED = (
    "Read the manifest of the last backup role run",
    "Find the recorded backup targets that left the config",
    "Remove the backup units and scripts of targets that left the config",
    "Report the backup targets that a deploy would remove",
    "Write the manifest of this host's backup targets",
)

FAKE_SYSTEMCTL = """#!/bin/sh
echo "$@" >> "$SYSTEMCTL_LOG"
case "$1" in show) echo "Id=$2"; echo "LoadState=loaded"; echo "ActiveState=active"; echo "UnitFileState=enabled";; esac
exit 0
"""


def _run(tmp_path: Path, *, old: list[str] | None, now: list[str], scripts: list[str],
         check: bool = False) -> tuple[subprocess.CompletedProcess[str], Path]:
    backup_dir = tmp_path / "opt" / "backup"
    (backup_dir / "status").mkdir(parents=True)
    for name in scripts:
        (backup_dir / f"backup-{name}.sh").write_text("#!/bin/sh\n")
        (backup_dir / "status" / f"{name}.json").write_text("{}")
        (backup_dir / "status" / f"{name}.restore-test.json").write_text("{}")
        (backup_dir / "cache" / name).mkdir(parents=True)
    if old is not None:
        (backup_dir / "targets.json").write_text(json.dumps([{"name": n} for n in old]))
    tasks = [t for t in yaml.safe_load(MAIN.read_text()) if t.get("name") in LIFTED]
    assert [t["name"] for t in tasks] == list(LIFTED)
    for t in tasks:
        if "ansible.builtin.include_tasks" in t:
            t["ansible.builtin.include_tasks"] = str(ROOT / "roles/backup/tasks/teardown.yml")
        if "ansible.builtin.copy" in t:
            t["ansible.builtin.copy"].pop("owner", None)
            t["ansible.builtin.copy"].pop("group", None)
    units = tmp_path / "units"
    units.mkdir()
    play = [{"hosts": "localhost", "connection": "local", "gather_facts": False, "tasks": tasks}]
    (tmp_path / "play.yml").write_text(yaml.safe_dump(play))
    (tmp_path / "vars.json").write_text(json.dumps({
        "backup_enabled": True,
        "ansible_become": False,
        "app_user": os.environ.get("USER", "nobody"),
        "backup_scripts_dir": str(backup_dir),
        "backup_status_dir": str(backup_dir / "status"),
        "backup_max_age_hours": 30,
        "_backup_accessories": [
            {"name": n, "method": "pg_dump", "schedule": "0 3 * * *", "format": "custom"}
            for n in now
        ],
    }))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "systemctl").write_text(FAKE_SYSTEMCTL)
    (bin_dir / "systemctl").chmod(0o755)
    (tmp_path / "ansible.cfg").write_text("[defaults]\n")
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "SYSTEMCTL_LOG": str(tmp_path / "systemctl.log"),
        "ANSIBLE_CONFIG": str(tmp_path / "ansible.cfg"),
        "ANSIBLE_NOCOLOR": "1",
        "ANSIBLE_LOCALHOST_WARNING": "0",
        "ANSIBLE_INVENTORY_UNPARSED_WARNING": "0",
        "ANSIBLE_PYTHON_INTERPRETER": sys.executable,
    }
    cmd = [sys.executable, "-m", "ansible.cli.playbook", "-i", "localhost,",
           "-e", f"@{tmp_path / 'vars.json'}", str(tmp_path / "play.yml")]
    if check:
        cmd.append("--check")
    proc = subprocess.run(cmd, cwd=tmp_path, env=env, capture_output=True, text=True, timeout=300)
    return proc, backup_dir


def test_a_recorded_target_that_left_the_config_is_removed(tmp_path):
    proc, d = _run(tmp_path, old=["postgres", "shop_data"], now=["postgres"],
                   scripts=["postgres", "shop_data", "hand-made"])
    assert proc.returncode == 0, proc.stdout[-3000:]
    assert not (d / "backup-shop_data.sh").exists()
    assert not (d / "status" / "shop_data.json").exists()
    assert not (d / "status" / "shop_data.restore-test.json").exists()
    assert not (d / "cache" / "shop_data").exists()
    assert (d / "backup-postgres.sh").exists()
    assert (d / "cache" / "postgres").is_dir()
    assert (d / "status" / "postgres.restore-test.json").exists()
    # Not in the old manifest: never touched.
    assert (d / "backup-hand-made.sh").exists()
    log = (tmp_path / "systemctl.log").read_text()
    assert "bay-backup@shop_data.timer" in log and "bay-backup@postgres.timer" not in log
    assert [t["name"] for t in json.loads((d / "targets.json").read_text())] == ["postgres"]


def test_no_old_manifest_removes_nothing(tmp_path):
    proc, d = _run(tmp_path, old=None, now=["postgres"], scripts=["postgres", "shop_data"])
    assert proc.returncode == 0, proc.stdout[-3000:]
    assert (d / "backup-shop_data.sh").exists()
    manifest = json.loads((d / "targets.json").read_text())
    assert manifest == [{"name": "postgres", "method": "pg_dump", "format": "custom",
                         "schedule": "0 3 * * *", "utc": False, "max_age_hours": 30}]


def test_check_mode_only_reports(tmp_path):
    proc, d = _run(tmp_path, old=["postgres", "shop_data"], now=["postgres"],
                   scripts=["postgres", "shop_data"], check=True)
    assert proc.returncode == 0, proc.stdout[-3000:]
    assert "would remove the backup units and scripts of shop_data" in proc.stdout
    assert (d / "backup-shop_data.sh").exists()


def test_a_hostile_name_in_the_manifest_is_ignored(tmp_path):
    proc, d = _run(tmp_path, old=["postgres", "../../etc/x", "a b"], now=["postgres"],
                   scripts=["postgres"])
    assert proc.returncode == 0, proc.stdout[-3000:]
    log = tmp_path / "systemctl.log"
    text = log.read_text() if log.exists() else ""
    assert "etc/x" not in text and "a b" not in text
