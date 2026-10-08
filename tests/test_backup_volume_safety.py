"""A volume backup never stores a cut-off snapshot, and a deploy waits for it.

The `file` method (every volume backup, the Headscale state and any accessory
with `method: file`) reads the data with `docker cp <container>:<path> -`. When
that ran as the left side of a pipe into `restic backup --stdin`, a `docker cp`
that died mid-stream still closed the pipe, restic read EOF and stored the
partial tar as the newest snapshot. The script now hands the command to restic
(`backup --stdin-from-command`, restic 0.17.0 or newer), and restic stores no
snapshot when the command exits non-zero.

The script is rendered from the real template and run with bash, with a fake
`docker` on PATH that fails mid-stream. restic is either a fake that keeps one
file per snapshot (it follows restic's documented rules for `--stdin` and
`--stdin-from-command`) or, when this machine has one, the real binary on a
repository in a temp dir.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).parent))

from helpers import make_ansible_env  # noqa: E402

# The producer: `docker cp` writes part of the tar, then fails (FAKE_DOCKER=fail)
# or writes all of it (FAKE_DOCKER=ok). Any other docker call is an error.
FAKE_DOCKER = r"""#!/usr/bin/env bash
[ "$1" = cp ] || { echo "fake docker: unexpected call: $*" >&2; exit 97; }
head -c 65536 /dev/zero | tr '\0' 'a'
if [ "${FAKE_DOCKER:-ok}" = fail ]; then
  echo "Error response from daemon: connection reset" >&2
  exit 1
fi
head -c 65536 /dev/zero | tr '\0' 'b'
"""

# A fake restic that follows the two input rules the script depends on:
# `--stdin` stores whatever arrived before EOF; `--stdin-from-command -- cmd`
# runs cmd and stores nothing when cmd exits non-zero. One file per snapshot
# in $RESTIC_REPOSITORY.
FAKE_RESTIC = r"""#!/usr/bin/env python3
import json, os, subprocess, sys, time
repo = os.environ["RESTIC_REPOSITORY"]
os.makedirs(repo, exist_ok=True)
args = sys.argv[1:]
cmd = args[0] if args else ""
snaps = sorted(p for p in os.listdir(repo) if p.endswith(".snap"))
if cmd == "backup":
    if "--stdin-from-command" in args:
        argv = args[args.index("--") + 1:]
        proc = subprocess.run(argv, stdout=subprocess.PIPE)
        if proc.returncode != 0:
            sys.stderr.write("Fatal: command failed: exit status %d\n" % proc.returncode)
            sys.exit(1)
        data = proc.stdout
    elif "--stdin" in args:
        data = sys.stdin.buffer.read()
    else:
        sys.exit(2)
    name = "%d-%06d.snap" % (time.time_ns(), len(snaps))
    with open(os.path.join(repo, name), "wb") as fh:
        fh.write(data)
    sys.exit(0)
if cmd == "snapshots":
    print(json.dumps([{"short_id": s} for s in snaps]))
    sys.exit(0)
if cmd == "stats":
    print(json.dumps({"total_size": os.path.getsize(os.path.join(repo, snaps[-1]))}))
    sys.exit(0)
if cmd == "forget":
    sys.exit(0)
sys.exit(2)
"""

# restic 0.19 prints a progress line on stdout before the JSON of `stats --json`
# (0.17.3, which the role installs, does not). Only for the real binary in this
# test, drop that line so the script's `jq` reads the JSON.
REAL_RESTIC_WRAPPER = r"""#!/usr/bin/env bash
if [ "$1" = stats ]; then
  "$REAL_RESTIC" "$@" | grep '^{'
  exit "${PIPESTATUS[0]}"
fi
exec "$REAL_RESTIC" "$@"
"""

_RESTICS = ["fake", "real"]


def _exe(path: Path, text: str) -> Path:
    path.write_text(text)
    path.chmod(0o755)
    return path


def _setup(tmp_path: Path, which: str) -> tuple[Path, dict[str, str], Path]:
    """Render the volume backup script; return (script, env, repo)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _exe(bin_dir / "docker", FAKE_DOCKER)
    repo = tmp_path / "repo"
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}
    env.pop("RESTIC_REPOSITORY", None)
    if which == "fake":
        restic = _exe(tmp_path / "restic", FAKE_RESTIC)
    else:
        real = shutil.which("restic")
        if real is None:
            pytest.skip("no restic on this machine; the fake restic covers the rule")
        restic = _exe(tmp_path / "restic", REAL_RESTIC_WRAPPER)
        env["REAL_RESTIC"] = real
        init_env = {**env, "RESTIC_REPOSITORY": str(repo), "RESTIC_PASSWORD": "pw-placeholder",
                    "RESTIC_CACHE_DIR": str(tmp_path / "cache")}
        subprocess.run([real, "init", "-q"], env=init_env, check=True, capture_output=True)

    script = make_ansible_env(ROOT / "roles" / "backup" / "templates").get_template(
        "backup.sh.j2").render(
        ansible_managed="managed", accessory_name="bay_shop_data", method="file",
        retain=14, repo=str(repo),
        accessory_config={"backup": {"source_path": "/app/data"}},
        backup_container="shop", inventory_hostname="eu-1",
        backup_restic_password="pw-placeholder", backup_s3_access_key_id="id-placeholder",
        backup_s3_secret_access_key="key-placeholder",
        backup_scripts_dir=str(tmp_path / "backup"),
        backup_lock_dir=str(tmp_path / "backup" / "locks"),
        backup_restic_bin=str(restic),
        alert_env_path=str(tmp_path / "no-alert.env"),
        docker_monitor_alert_header="", docker_monitor_alert_footer="",
    )
    path = _exe(tmp_path / "backup.sh", script)
    return path, env, repo


def _snapshots(tmp_path: Path, env: dict[str, str], repo: Path) -> int:
    restic = tmp_path / "restic"
    out = subprocess.run(
        [str(restic), "snapshots", "--json"],
        env={**env, "RESTIC_REPOSITORY": str(repo), "RESTIC_PASSWORD": "pw-placeholder",
             "RESTIC_CACHE_DIR": str(tmp_path / "cache")},
        check=True, capture_output=True, text=True,
    ).stdout
    return len(json.loads(out) or [])


def _backup(script: Path, env: dict[str, str], docker: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["bash", str(script)], env={**env, "FAKE_DOCKER": docker},
                          capture_output=True, text=True, timeout=120)


@pytest.mark.parametrize("which", _RESTICS)
def test_volume_backup_failed_producer_leaves_no_snapshot(tmp_path: Path, which: str) -> None:
    script, env, repo = _setup(tmp_path, which)

    # docker cp dies mid-stream: the script fails, and no snapshot is stored.
    failed = _backup(script, env, "fail")
    assert failed.returncode == 1, failed.stdout + failed.stderr
    assert "ERROR:" in failed.stdout
    assert "connection reset" in failed.stderr  # docker's own error reaches the journal
    assert _snapshots(tmp_path, env, repo) == 0
    # The lock is released, so a deploy does not wait for a dead backup.
    assert not (tmp_path / "backup" / "locks" / "bay_shop_data.lock").exists()

    # A good docker cp makes exactly one snapshot.
    ok = _backup(script, env, "ok")
    assert ok.returncode == 0, ok.stdout + ok.stderr
    assert "Backup complete for bay_shop_data" in ok.stdout
    assert _snapshots(tmp_path, env, repo) == 1

    # A second failure after a good run leaves the good snapshot as the newest.
    again = _backup(script, env, "fail")
    assert again.returncode == 1
    assert _snapshots(tmp_path, env, repo) == 1


def test_file_backup_hands_docker_cp_to_restic() -> None:
    """The rendered command: restic runs docker cp, nothing pipes into restic."""
    script = make_ansible_env(ROOT / "roles" / "backup" / "templates").get_template(
        "backup.sh.j2").render(
        ansible_managed="m", accessory_name="headscale", method="file", retain=30, repo="r",
        accessory_config={"backup": {"source_path": "/var/lib/headscale"}},
        inventory_hostname="eu-1", backup_restic_password="p", backup_s3_access_key_id="i",
        backup_s3_secret_access_key="k", backup_scripts_dir="/b", backup_lock_dir="/b/l",
        backup_restic_bin="/r", docker_monitor_alert_header="", docker_monitor_alert_footer="",
    )
    file_branch = script[script.index("Starting file backup"):script.index("Error Checking")]
    assert "--stdin-from-command -- docker cp \"${CONTAINER}:${SOURCE_PATH}\" -" in file_branch
    assert "| \\" not in file_branch and "--stdin \\" not in file_branch
    assert '--stdin-filename "${ACCESSORY}.tar"' in file_branch


# ── The restic the role leaves on the box ───────────────────────────────────


def _play_files(tmp_path: Path, tasks: list[dict[str, Any]], variables: dict[str, Any]
                ) -> tuple[list[str], dict[str, str]]:
    play = [{"hosts": "localhost", "connection": "local", "gather_facts": False,
             "tasks": tasks}]
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
        "PYTHONUNBUFFERED": "1",
    }
    argv = [sys.executable, "-m", "ansible.cli.playbook", "-i", "localhost,",
            "-e", f"@{tmp_path / 'vars.json'}", str(tmp_path / "play.yml")]
    return argv, env


def _play(tmp_path: Path, tasks: list[dict[str, Any]], variables: dict[str, Any]
          ) -> subprocess.CompletedProcess[str]:
    argv, env = _play_files(tmp_path, tasks, variables)
    return subprocess.run(argv, cwd=tmp_path, env=env, capture_output=True, text=True,
                          timeout=300)


def _all_tasks(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for task in tasks:
        out.append(task)
        for key in ("block", "rescue", "always"):
            out.extend(_all_tasks(task.get(key) or []))
    return out


def _task(path: Path, name: str) -> dict[str, Any]:
    return next(t for t in _all_tasks(yaml.safe_load(path.read_text())) if t.get("name") == name)


@pytest.mark.parametrize(("stdout", "skipped", "passes"), [
    ("restic 0.17.3 compiled with go1.23.3 on linux/amd64", False, True),
    ("restic 0.19.0 compiled with go1.26.4 on linux/amd64", False, True),
    ("restic 0.16.4 compiled with go1.21.6 on linux/amd64", False, False),
    ("restic 0.9.6 compiled with go1.13.8 on linux/amd64", False, False),
    ("not restic", False, False),
    # --check skips the version command; the gate must not read its empty stdout.
    ("", True, True),
])
def test_role_requires_a_restic_that_runs_the_producer(
    tmp_path: Path, stdout: str, skipped: bool, passes: bool
) -> None:
    gate = _task(ROOT / "roles" / "backup" / "tasks" / "install.yml",
                 "Require restic 0.17.0 or newer for the backup scripts")
    registered: dict[str, Any] = {"stdout": stdout, "rc": 0, "changed": False}
    if skipped:
        registered = {"skipped": True, "changed": False, "stdout": "", "rc": 0}
    proc = _play(tmp_path, [gate], {"_restic_version": registered,
                                    "backup_restic_bin": "/usr/local/bin/restic",
                                    "backup_restic_version": "0.17.3"})
    assert (proc.returncode == 0) is passes, proc.stdout[-2000:]
    if not passes:
        assert "0.17.0 or newer" in proc.stdout


# ── The deploy waits for a running volume backup ────────────────────────────


def test_deploy_waits_for_volume_backup_lock(tmp_path: Path) -> None:
    wait = _task(ROOT / "roles" / "deploy_stack" / "tasks" / "main.yml",
                 "Wait for active backups to complete before deploy")
    # The same timeout for every lock: one task, one timeout.
    assert wait["ansible.builtin.wait_for"]["timeout"] == 300
    wait = {**wait, "register": "_waited"}
    locks = tmp_path / "locks"
    locks.mkdir()
    lock = locks / "bay_shop_data.lock"
    lock.write_text("4242\n")
    out = tmp_path / "out.json"
    record = {"name": "Record what the wait covered", "ansible.builtin.copy": {
        "content": "{{ {'done': lookup('pipe', 'date +%s.%N') | float, "
                   "'waited': _waited.results | items2dict(key_name='item', value_name='elapsed')"
                   "} | to_json }}",
        "dest": str(out), "mode": "0600"}}
    argv, env = _play_files(tmp_path, [wait, record], {
        "stack_name": "bay",
        "stack_dir": str(tmp_path),
        "backup_lock_dir": str(locks),
        "active_accessories": {"postgres": {"image": "postgres:16", "backup": True},
                               "cache": {"image": "redis:7"}},
        "active_services": {"shop": {"image": "x"}},
        # blog runs on another host: its lock is not this host's business.
        "volume_backups": {"shop_data": {"container": "shop", "path": "/app/data"},
                           "blog-data": {"container": "blog", "path": "/srv"}},
    })

    # The volume backup finishes 3 seconds after the deploy reached the wait.
    removed_at: list[float] = []

    def _finish_backup() -> None:
        removed_at.append(time.time())
        lock.unlink()

    timer = threading.Timer(3, _finish_backup)
    lines: list[str] = []
    reached = False
    proc = subprocess.Popen(argv, cwd=tmp_path, env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True)
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            lines.append(line)
            if line.startswith("TASK [Wait for active backups") and not reached:
                reached = True
                timer.start()
        proc.wait(timeout=300)
    finally:
        timer.cancel()
        if proc.poll() is None:
            proc.kill()
    output = "".join(lines)
    assert proc.returncode == 0, output[-3000:]
    assert reached, "the play never reached the wait task:\n" + output[-3000:]
    # The play ended before the backup released its lock: the deploy did not wait.
    assert removed_at, "the deploy went on while the volume backup held its lock"
    got = json.loads(out.read_text())
    # Accessory and volume locks; not the volume of a container on another host.
    assert list(got["waited"]) == ["postgres", "bay_shop_data"]
    # The deploy went on only after the volume backup released its lock. (Not
    # the `elapsed` of the item: under load the module may start late, and
    # that number shrinks; the order of the two clocks does not.)
    assert got["done"] >= removed_at[0]
