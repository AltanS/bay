"""Volume backups on the box: the backup role entries, the script and the restore.

`bay compile` writes every volume mount with `backup` true to the top-level
`volume_backups:` map (`{container, path, schedule?, retain?}`, keyed by the
volume name). The backup role adds one restic `file` entry per volume whose
container runs on the host, named like the Docker volume
(`<stack_name>_<volume>`). The script reads the mount path through the docker
daemon; `restore.yml` writes the archive back into the volume's Mountpoint
with the container stopped.

The entry and the restore variables are computed in a real ansible-playbook,
from tasks and vars lifted out of the role and the playbook, so nothing on the
machine running the tests is touched.
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
ROLE = ROOT / "roles" / "backup"
sys.path.insert(0, str(Path(__file__).parent))

from helpers import make_ansible_env  # noqa: E402

VOLUME_BACKUPS = {
    "shop_data": {"container": "shop", "path": "/app/data", "schedule": "0 22 * * *", "retain": 14},
    "blog-data": {"container": "blog", "path": "/srv"},
}
ROLE_VARS = {
    "backup_enabled": True,
    "stack_name": "bay",
    "inventory_hostname": "eu-1",
    "backup_s3_endpoint": "s3.example.com",
    "backup_s3_bucket": "bucket",
    "backup_s3_prefix": "backups",
    "backup_schedule": "0 3 * * *",
    "backup_retain": 30,
}


def _run(tmp_path: Path, tasks: list[dict[str, Any]], variables: dict[str, Any],
         expr: str, play_vars: dict[str, Any] | None = None) -> Any:
    out = tmp_path / "out.json"
    play = [{
        "hosts": "localhost", "connection": "local", "gather_facts": False,
        "vars": play_vars or {},
        "tasks": [*tasks, {
            "name": "Write the result",
            "ansible.builtin.copy": {"content": "{{ " + expr + " | to_json }}",
                                     "dest": str(out), "mode": "0600"},
        }],
    }]
    (tmp_path / "play.yml").write_text(yaml.safe_dump(play))
    (tmp_path / "vars.json").write_text(json.dumps(variables))
    (tmp_path / "ansible.cfg").write_text("[defaults]\n")
    env = {
        **os.environ,
        "ANSIBLE_CONFIG": str(tmp_path / "ansible.cfg"),
        "ANSIBLE_NOCOLOR": "1",
        "ANSIBLE_LOCALHOST_WARNING": "0",
        "ANSIBLE_INVENTORY_UNPARSED_WARNING": "0",
        "ANSIBLE_PYTHON_INTERPRETER": sys.executable,
    }
    proc = subprocess.run(
        [sys.executable, "-m", "ansible.cli.playbook", "-i", "localhost,",
         "-e", f"@{tmp_path / 'vars.json'}", str(tmp_path / "play.yml")],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=300,
    )
    assert proc.returncode == 0, proc.stdout[-3000:] + proc.stderr[-3000:]
    return json.loads(out.read_text())


def _role_task(name: str) -> dict[str, Any]:
    tasks = yaml.safe_load((ROLE / "tasks" / "main.yml").read_text())
    return next(t for t in tasks if t["name"] == name)


def _all_tasks(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for task in tasks:
        out.append(task)
        for key in ("block", "rescue", "always"):
            out.extend(_all_tasks(task.get(key) or []))
    return out


def test_backup_role_volume_entries(tmp_path: Path) -> None:
    # ── the role: one file entry per volume whose container runs here
    task = _role_task("Resolve backup configuration for each volume mount")
    entries = _run(
        tmp_path, [task],
        {**ROLE_VARS, "volume_backups": VOLUME_BACKUPS,
         "active_services": {"shop": {"image": "x"}}, "active_accessories": {}},
        "_backup_accessories | default([])",
    )
    assert entries == [{
        "name": "bay_shop_data",
        "method": "file",
        "schedule": "0 22 * * *",
        "retain": 14,
        "image": "",
        "repo": "s3:s3.example.com/bucket/backups/eu-1/bay_shop_data",
        "container": "shop",
        "volume": "bay_shop_data",
        "utc": True,
        "config": {"backup": {"source_path": "/app/data"}},
    }]
    # The role defaults fill a volume without a [backup] policy; its time is box time.
    blog = _run(
        tmp_path, [task],
        {**ROLE_VARS, "volume_backups": VOLUME_BACKUPS, "active_services": {"blog": {}}},
        "_backup_accessories",
    )
    assert [(e["name"], e["schedule"], e["retain"], e["utc"]) for e in blog] == [
        ("bay_blog-data", "0 3 * * *", 30, False)
    ]
    # No volume_backups: nothing.
    assert _run(tmp_path, [task], {**ROLE_VARS, "active_services": {}},
                "_backup_accessories | default([])") == []

    # ── the script reads the mount path out of the container, under the volume name
    env = make_ansible_env(ROLE / "templates")
    entry = entries[0]
    script = env.get_template("backup.sh.j2").render(
        ansible_managed="managed", accessory_name=entry["name"], method="file",
        retain=entry["retain"], repo=entry["repo"], accessory_config=entry["config"],
        backup_container=entry["container"], inventory_hostname="eu-1",
        backup_restic_password="pw-placeholder", backup_s3_access_key_id="id-placeholder",
        backup_s3_secret_access_key="key-placeholder", backup_scripts_dir="/opt/bay/backup",
        backup_lock_dir="/opt/bay/backup/locks", backup_restic_bin="/usr/local/bin/restic",
        docker_monitor_alert_header="", docker_monitor_alert_footer="",
    )
    path = tmp_path / "backup.sh"
    path.write_text(script)
    assert subprocess.run(["bash", "-n", str(path)]).returncode == 0
    assert "ACCESSORY=bay_shop_data" in script
    assert "CONTAINER=shop" in script
    assert "SOURCE_PATH=/app/data" in script
    assert 'docker cp "${CONTAINER}:${SOURCE_PATH}" -' in script
    assert 'backup_from "${ACCESSORY}.tar"' in script
    assert "--keep-within 14d" in script
    # An accessory file backup still reads its own container.
    acc = env.get_template("backup.sh.j2").render(
        ansible_managed="managed", accessory_name="headscale", method="file", retain=30,
        repo="r", accessory_config={"backup": {"source_path": "/var/lib/headscale"}},
        inventory_hostname="eu-1", backup_restic_password="p", backup_s3_access_key_id="i",
        backup_s3_secret_access_key="k", backup_scripts_dir="/b", backup_lock_dir="/b/l",
        backup_restic_bin="/r", docker_monitor_alert_header="", docker_monitor_alert_footer="",
    )
    assert "CONTAINER=headscale" in acc

    # ── the timer: a schedule from bay.toml is UTC, the role default is box time
    timer = env.get_template("bay-backup@.timer.j2")
    assert "OnCalendar=*-*-* 22:00:00 UTC" in timer.render(
        ansible_managed="m", accessory_name="bay_shop_data", timer_schedule="0 22 * * *",
        timer_utc=True)
    assert "OnCalendar=*-*-* 03:00:00\n" in timer.render(
        ansible_managed="m", accessory_name="postgres", timer_schedule="0 3 * * *")

    # ── the generate tasks hand the container and the UTC flag to the templates
    gen = _role_task("Generate backup scripts for accessories")
    assert gen["vars"]["backup_container"] == "{{ item.container | default(item.name) }}"
    timers = _role_task("Deploy backup timer units")
    assert timers["vars"]["timer_utc"] == "{{ item.utc | default(false) | bool }}"

    # ── restore.yml: a volume target resolves to a file restore of its path
    playbook = yaml.safe_load((ROOT / "restore.yml").read_text())[0]
    keys = ("_volume_key", "_volume_backup", "_is_volume", "_accessory_config",
            "_image", "_backup_raw", "_method", "_auto_method", "_restore_method")
    play_vars = {k: playbook["vars"][k] for k in keys}
    got = _run(
        tmp_path, [],
        {"stack_name": "bay", "accessory": "bay_shop_data", "volume_backups": VOLUME_BACKUPS,
         "accessories": {}},
        "{'is_volume': _is_volume | bool, 'config': _accessory_config, "
        "'method': _restore_method, 'container': _volume_backup.container}",
        play_vars=play_vars,
    )
    assert got == {"is_volume": True, "config": {"backup": {"method": "file", "source_path": "/app/data"}},
                   "method": "file", "container": "shop"}
    # An accessory target is unchanged.
    got = _run(
        tmp_path, [],
        {"stack_name": "bay", "accessory": "postgres", "volume_backups": VOLUME_BACKUPS,
         "accessories": {"postgres": {"image": "postgres:16", "backup": True}}},
        "{'is_volume': _is_volume | bool, 'method': _restore_method}",
        play_vars=play_vars,
    )
    assert got == {"is_volume": False, "method": "pg_dump"}

    # The volume restore: list and stop every container on the volume, read the
    # Mountpoint, extract as root, start the same set always.
    tasks = {t["name"]: t for t in _all_tasks(playbook["tasks"] + playbook["pre_tasks"])}
    block = tasks["Restore a volume backup"]
    assert [t["name"] for t in block["block"]] == [
        "List the running containers that mount the volume",
        "Stop every container that mounts the volume for restore",
        "Read the mountpoint of the volume",
        "Restore the volume into its mountpoint",
    ]
    assert [t["name"] for t in block["always"]] == ["Start the containers that ran on the volume before restore"]
    inspect = tasks["Read the mountpoint of the volume"]["ansible.builtin.command"]["cmd"]
    assert "docker volume inspect" in inspect and ".Mountpoint" in inspect
    extract = tasks["Restore the volume into its mountpoint"]
    assert extract["become_user"] == "root"
    cmd = extract["ansible.builtin.shell"]["cmd"]
    assert "/{{ accessory }}.tar" in cmd and "--strip-components=1" in cmd
    assert "_volume_mountpoint.stdout" in cmd
    assert "not (_is_volume | bool)" in tasks["Restore file backup"]["when"]
    assert tasks["Skip a host that does not run the container of the volume"]["ansible.builtin.meta"] == "end_host"
