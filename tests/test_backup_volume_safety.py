"""A backup never stores a cut-off snapshot, and a deploy waits for a volume backup.

Every backup method streams a producer into restic: `pg_dump` and `mysqldump`
through `docker exec`, and `docker cp` for `redis` and `file` (every volume
backup, the Headscale state and any accessory with `method: file`). When the
producer ran as the left side of a pipe into `restic backup --stdin`, one that
died mid-stream still closed the pipe, restic read EOF and stored the partial
dump as the newest snapshot. The script now hands the producer to restic
(`backup --stdin-from-command`, restic 0.17.0 or newer), and restic stores no
snapshot when the command exits non-zero.

The script is rendered from the real template and run with bash, with a fake
`docker` on PATH that fails mid-stream. restic is either a fake that keeps one
file per snapshot (it follows restic's documented rules for `--stdin` and
`--stdin-from-command`) or a real binary on a repository in a temp dir:
`$BAY_TEST_RESTIC` when set (for example the 0.17.3 the role installs), else
`restic` on PATH, else the real cases skip. `$BAY_TEST_RESTIC_019` is a restic
0.19.x binary for the `real019` cases (local only, they skip when it is unset).
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

# The fake docker. `redis-cli LASTSAVE/BGSAVE` keep a counter, so the BGSAVE
# poll ends at once. Every other call is a producer: it is logged (argv, one
# JSON line), writes 64 KiB, then fails when $FAIL_ON is one of its arguments,
# else writes 64 KiB more. When $EMPTY_ON is one of its arguments it writes
# nothing and exits 0. When $TINY_ON is one of its arguments it writes 10 bytes
# and exits 0.
FAKE_DOCKER = r"""#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
state = os.environ["FAKE_DOCKER_STATE"]
if args[:1] == ["exec"] and "redis-cli" in args:
    path = os.path.join(state, "lastsave")
    n = int(open(path).read()) if os.path.exists(path) else 100
    if args[-1] == "BGSAVE":
        open(path, "w").write(str(n + 1))
        print("Background saving started")
    else:
        print(n)
    sys.exit(0)
with open(os.path.join(state, "producers.log"), "a") as fh:
    fh.write(json.dumps(args) + "\n")
out = sys.stdout.buffer
if os.environ.get("EMPTY_ON") and os.environ["EMPTY_ON"] in args:
    sys.exit(0)  # a dump that exits 0 and writes nothing
if os.environ.get("TINY_ON") and os.environ["TINY_ON"] in args:
    out.write(b"tiny dump\n")
    out.flush()
    sys.exit(0)
out.write(b"a" * 65536)
out.flush()
if os.environ.get("FAIL_ON") and os.environ["FAIL_ON"] in args:
    if os.environ.get("NOISE"):
        sys.stderr.write("helper: command failed: boom\n")  # not restic's line
    sys.stderr.write("Error response from daemon: connection reset\n")
    sys.exit(1)
out.write(b"b" * 65536)
"""

# A fake restic that follows the two input rules the script depends on:
# `--stdin` stores whatever arrived before EOF; `--stdin-from-command -- cmd`
# runs cmd and stores nothing when cmd exits non-zero, with restic's own error
# text. One data file and one metadata file per snapshot in $RESTIC_REPOSITORY.
# FAKE_RESTIC_FAIL=1 makes `backup` fail on its own, before the command runs.
# STATS_NO_JSON=1 makes `stats` print text and no JSON.
# PROGRESS_LINE=1 makes `stats` print restic 0.19.0's progress line before the JSON.
# Every `stats` call is logged to $RESTIC_CALLS.
FAKE_RESTIC = r"""#!/usr/bin/env python3
import json, os, subprocess, sys
PROGRESS = "[0:00] 100.00%  1 / 1 snapshots"
repo = os.environ["RESTIC_REPOSITORY"]
os.makedirs(repo, exist_ok=True)
args = sys.argv[1:]
cmd = args[0] if args else ""
snaps = sorted(p[:-5] for p in os.listdir(repo) if p.endswith(".json"))
def meta(s):
    return json.load(open(os.path.join(repo, s + ".json")))
if cmd == "backup":
    name = args[args.index("--stdin-filename") + 1] if "--stdin-filename" in args else "stdin"
    if os.environ.get("FAKE_RESTIC_FAIL"):
        sys.stderr.write("Fatal: unable to open config file: Stat: connection refused\n")
        sys.exit(1)
    if "--stdin-from-command" in args:
        proc = subprocess.run(args[args.index("--") + 1:], stdout=subprocess.PIPE)
        if proc.returncode != 0:
            sys.stderr.write("error: failed to save /%s: Fatal: command failed: exit status %d\n"
                             % (name, proc.returncode))
            sys.exit(1)
        data = proc.stdout
        if not data:
            # restic 0.17: an empty stream fails the file, still stores the snapshot.
            snap = "%06d" % len(snaps)
            open(os.path.join(repo, snap + ".data"), "wb").write(data)
            with open(os.path.join(repo, snap + ".json"), "w") as fh:
                json.dump({"short_id": snap, "paths": ["/" + name]}, fh)
            print("snapshot %s saved" % snap)
            sys.stderr.write("error: failed to save /%s: read /%s: no data read\n" % (name, name))
            sys.exit(3)
    elif "--stdin" in args:
        data = sys.stdin.buffer.read()
    else:
        sys.exit(2)
    snap = "%06d" % len(snaps)
    open(os.path.join(repo, snap + ".data"), "wb").write(data)
    with open(os.path.join(repo, snap + ".json"), "w") as fh:
        json.dump({"short_id": snap, "paths": ["/" + name]}, fh)
    print("snapshot %s saved" % snap)
    sys.exit(0)
if cmd == "snapshots":
    print(json.dumps([meta(s) for s in snaps]))
    sys.exit(0)
if cmd == "stats":
    # `stats <id> --json`, like the script runs it; the script never asks for `latest`.
    with open(os.environ["RESTIC_CALLS"], "a") as fh:
        fh.write(" ".join(args) + "\n")
    snap = args[1]
    if snap not in snaps:
        sys.stderr.write("Fatal: no matching ID found for prefix %r\n" % snap)
        sys.exit(1)
    if os.environ.get("STATS_NO_JSON"):
        print("repository contains 1 snapshots")
        sys.exit(0)
    if os.environ.get("PROGRESS_LINE"):
        print(PROGRESS)
    print(json.dumps({"total_size": os.path.getsize(os.path.join(repo, snap + ".data")),
                      "snapshots_count": 1}))
    sys.exit(0)
if cmd == "dump":
    snap = snaps[-1] if args[1] == "latest" else args[1]
    assert meta(snap)["paths"] == [args[2]], (meta(snap), args[2])
    sys.stdout.buffer.write(open(os.path.join(repo, snap + ".data"), "rb").read())
    sys.exit(0)
if cmd == "forget":
    for gone in [a for a in args[1:] if a in snaps]:
        os.remove(os.path.join(repo, gone + ".json"))
        os.remove(os.path.join(repo, gone + ".data"))
    sys.exit(0)
sys.exit(2)
"""

# A pass-through for the real binary that logs every `stats` call to
# $RESTIC_CALLS and, with PROGRESS_LINE=1, prints restic 0.19.0's progress line
# before the real output (0.17.3 never prints it, and 0.19.1 only on a tty).
REAL_RESTIC_WRAPPER = r"""#!/usr/bin/env bash
if [ "$1" = stats ]; then
  printf '%s\n' "$*" >> "$RESTIC_CALLS"
  if [ -n "${PROGRESS_LINE:-}" ]; then
    echo "[0:00] 100.00%  1 / 1 snapshots, 1 / 1 files"
  fi
fi
exec "$REAL_RESTIC" "$@"
"""

_RESTICS = ["fake", "real", "real019"]

# Per method: the render variables, the snapshot file name (what restore.yml
# dumps), the producer argv exactly as the script ran it before, and the
# argument that makes the fake docker fail.
METHODS: dict[str, dict[str, Any]] = {
    "pg_dump": {
        "accessory": "postgres",
        "config": {"env": {"clear": {"POSTGRES_USER": "app", "POSTGRES_DB": "shop"}}},
        "file": "postgres.sql",
        "producer": ["exec", "postgres", "pg_dump", "-U", "app", "shop"],
        "fail_on": "pg_dump",
    },
    "mysql": {
        "accessory": "mysql",
        "config": {"env": {"clear": {"MYSQL_ROOT_PASSWORD": "pw-placeholder",
                                     "MYSQL_DATABASE": "shop"}}},
        "file": "mysql.sql",
        "producer": ["exec", "-e", "MYSQL_PWD=pw-placeholder", "mysql",
                     "mysqldump", "-u", "root", "shop"],
        "fail_on": "mysqldump",
    },
    "redis": {
        "accessory": "redis",
        "config": {"env": {"clear": {}}},
        "file": "redis.rdb",
        "producer": ["cp", "redis:/data/dump.rdb", "-"],
        "fail_on": "cp",
    },
    "file": {
        "accessory": "bay_shop_data",
        "container": "shop",
        "config": {"backup": {"source_path": "/app/data"}},
        "file": "bay_shop_data.tar",
        "producer": ["cp", "shop:/app/data", "-"],
        "fail_on": "cp",
    },
}


def _exe(path: Path, text: str) -> Path:
    path.write_text(text)
    path.chmod(0o755)
    return path


def _render(method: str, **overrides: Any) -> str:
    m = METHODS[method]
    ctx: dict[str, Any] = dict(
        ansible_managed="managed", accessory_name=m["accessory"], method=method,
        retain=14, repo="r", accessory_config=m["config"], inventory_hostname="eu-1",
        backup_restic_password="pw-placeholder", backup_s3_access_key_id="id-placeholder",
        backup_s3_secret_access_key="key-placeholder", backup_scripts_dir="/b",
        backup_lock_dir="/b/l", backup_restic_bin="/r",
        docker_monitor_alert_header="", docker_monitor_alert_footer="",
    )
    if "container" in m:
        ctx["backup_container"] = m["container"]
    ctx.update(overrides)
    env = make_ansible_env(ROOT / "roles" / "backup" / "templates")
    return env.get_template("backup.sh.j2").render(**ctx)


class Box:
    """One rendered backup script with its fake docker and its restic repo."""

    def __init__(self, tmp_path: Path, which: str, method: str, **overrides: Any) -> None:
        self.tmp = tmp_path
        self.method = method
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        _exe(bin_dir / "docker", FAKE_DOCKER)
        state = tmp_path / "docker-state"
        state.mkdir()
        self.state = state
        self.repo = tmp_path / "repo"
        self.env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}",
                    "FAKE_DOCKER_STATE": str(state)}
        self.env.pop("RESTIC_REPOSITORY", None)
        self.env["RESTIC_CALLS"] = str(tmp_path / "restic-calls.log")
        if which == "fake":
            restic = _exe(tmp_path / "restic", FAKE_RESTIC)
        else:
            if which == "real019":
                real = os.environ.get("BAY_TEST_RESTIC_019")
                if not real:
                    pytest.skip("BAY_TEST_RESTIC_019 is not set (a restic 0.19.x binary)")
            else:
                real = os.environ.get("BAY_TEST_RESTIC") or shutil.which("restic")
            if real is None:
                pytest.skip("no restic on this machine; the fake restic covers the rule")
            restic = _exe(tmp_path / "restic", REAL_RESTIC_WRAPPER)
            self.env["REAL_RESTIC"] = real
            subprocess.run([real, "init", "-q"], env=self._restic_env(), check=True,
                           capture_output=True)
        self.restic = restic
        script = _render(
            method, repo=str(self.repo), backup_scripts_dir=str(tmp_path / "backup"),
            backup_lock_dir=str(tmp_path / "backup" / "locks"), backup_restic_bin=str(restic),
            alert_env_path=str(tmp_path / "no-alert.env"), **overrides,
        )
        self.script = _exe(tmp_path / "backup.sh", script)

    def _restic_env(self) -> dict[str, str]:
        return {**self.env, "RESTIC_REPOSITORY": str(self.repo),
                "RESTIC_PASSWORD": "pw-placeholder", "RESTIC_CACHE_DIR": str(self.tmp / "cache")}

    def run(self, fail_on: str = "", empty_on: str = "", **extra: str
            ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["bash", str(self.script)],
                              env={**self.env, "FAIL_ON": fail_on, "EMPTY_ON": empty_on, **extra},
                              capture_output=True, text=True, timeout=120)

    def snapshots(self) -> list[str]:
        """The file path of every snapshot, oldest first."""
        out = subprocess.run([str(self.restic), "snapshots", "--json"], env=self._restic_env(),
                             check=True, capture_output=True, text=True).stdout
        snaps = sorted(json.loads(out) or [], key=lambda s: s.get("time", s.get("short_id")))
        return [p for s in snaps for p in s["paths"]]

    def dump(self, path: str) -> bytes:
        return subprocess.run([str(self.restic), "dump", "latest", path],
                              env=self._restic_env(), check=True, capture_output=True).stdout

    def snapshot_ids(self) -> dict[str, str]:
        """Snapshot id by file path, for every snapshot in the repo."""
        out = subprocess.run([str(self.restic), "snapshots", "--json"], env=self._restic_env(),
                             check=True, capture_output=True, text=True).stdout
        return {p: s["short_id"] for s in json.loads(out) or [] for p in s["paths"]}

    def stats_calls(self) -> list[list[str]]:
        log = Path(self.env["RESTIC_CALLS"])
        return [line.split() for line in log.read_text().splitlines()] if log.exists() else []

    def producers(self) -> list[list[str]]:
        log = self.state / "producers.log"
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


@pytest.mark.parametrize("which", _RESTICS)
def test_volume_backup_failed_producer_leaves_no_snapshot(tmp_path: Path, which: str) -> None:
    box = Box(tmp_path, which, "file")

    # docker cp dies mid-stream: the script fails, and no snapshot is stored.
    failed = box.run(fail_on="cp")
    assert failed.returncode == 1, failed.stdout + failed.stderr
    assert "ERROR:" in failed.stdout
    assert "connection reset" in failed.stderr  # docker's own error reaches the journal
    assert box.snapshots() == []
    # The lock is released, so a deploy does not wait for a dead backup.
    assert not (tmp_path / "backup" / "locks" / "bay_shop_data.lock").exists()

    # A good docker cp makes exactly one snapshot.
    ok = box.run()
    assert ok.returncode == 0, ok.stdout + ok.stderr
    assert "Backup complete for bay_shop_data" in ok.stdout
    assert box.snapshots() == ["/bay_shop_data.tar"]

    # A second failure after a good run leaves the good snapshot as the newest.
    again = box.run(fail_on="cp")
    assert again.returncode == 1
    assert box.snapshots() == ["/bay_shop_data.tar"]


@pytest.mark.parametrize("which", _RESTICS)
@pytest.mark.parametrize("method", sorted(METHODS))
def test_backup_failed_dump_leaves_no_snapshot(tmp_path: Path, method: str, which: str) -> None:
    m = METHODS[method]
    box = Box(tmp_path, which, method)
    failed = box.run(fail_on=m["fail_on"])
    assert failed.returncode == 1, failed.stdout + failed.stderr
    # The alert still names the dump, with its exit code and the file.
    assert (f"ERROR: Dump command failed with exit code 1, no snapshot stored ({m['file']})"
            in failed.stdout), failed.stdout + failed.stderr
    assert "connection reset" in failed.stderr
    assert box.snapshots() == []
    assert box.producers() == [m["producer"]]
    assert not (tmp_path / "backup" / "locks" / f"{m['accessory']}.lock").exists()


@pytest.mark.parametrize("which", _RESTICS)
@pytest.mark.parametrize("method", sorted(METHODS))
def test_backup_dump_snapshot_keeps_its_file_name(tmp_path: Path, method: str, which: str) -> None:
    """One snapshot, under the file name restore.yml dumps, of the whole stream."""
    m = METHODS[method]
    box = Box(tmp_path, which, method)
    ok = box.run()
    assert ok.returncode == 0, ok.stdout + ok.stderr
    assert box.snapshots() == [f"/{m['file']}"]
    assert box.dump(f"/{m['file']}") == b"a" * 65536 + b"b" * 65536
    # The dump command is the one the script ran before, argument for argument.
    assert box.producers() == [m["producer"]]


@pytest.mark.parametrize("which", _RESTICS)
@pytest.mark.parametrize("method", sorted(METHODS))
def test_backup_empty_dump_stores_no_snapshot(tmp_path: Path, method: str, which: str) -> None:
    """A dump that exits 0 and writes nothing: restic fails with `no data read` but has
    stored a snapshot of the empty file. The script removes that one and alerts."""
    m = METHODS[method]
    box = Box(tmp_path, which, method)
    # A good snapshot first: it must survive, and stay the newest.
    assert box.run().returncode == 0
    assert box.snapshots() == [f"/{m['file']}"]
    failed = box.run(empty_on=m["fail_on"])
    assert failed.returncode == 1, failed.stdout + failed.stderr
    assert (f"ERROR: Dump produced no data, no snapshot stored ({m['file']})"
            in failed.stdout), failed.stdout + failed.stderr
    assert "no data read" in failed.stderr
    assert box.snapshots() == [f"/{m['file']}"]
    assert box.dump(f"/{m['file']}") == b"a" * 65536 + b"b" * 65536
    assert not (tmp_path / "backup" / "locks" / f"{m['accessory']}.lock").exists()


@pytest.mark.parametrize("which", _RESTICS)
def test_backup_empty_dump_on_an_empty_repo_leaves_nothing(tmp_path: Path, which: str) -> None:
    box = Box(tmp_path, which, "file")
    failed = box.run(empty_on="cp")
    assert failed.returncode == 1, failed.stdout + failed.stderr
    assert "Dump produced no data" in failed.stdout
    assert box.snapshots() == []


@pytest.mark.parametrize("which", _RESTICS)
def test_dump_stderr_with_command_failed_does_not_hide_the_exit_code(
    tmp_path: Path, which: str
) -> None:
    """The exit code comes from restic's `Fatal: command failed: ` line, not from the
    first `command failed: ` in stderr (the dump command's own text comes first)."""
    box = Box(tmp_path, which, "file")
    failed = box.run(fail_on="cp", NOISE="1")
    assert failed.returncode == 1
    assert "helper: command failed: boom" in failed.stderr
    assert ("ERROR: Dump command failed with exit code 1, no snapshot stored "
            "(bay_shop_data.tar)") in failed.stdout, failed.stdout


def test_pg_dump_reports_a_failed_database_when_a_later_one_succeeds(tmp_path: Path) -> None:
    """Each database is its own snapshot; a failure in the first is not masked by the next."""
    config = {"env": {"clear": {"POSTGRES_USER": "app"}},
              "backup": {"databases": ["shop", "blog"]}}
    box = Box(tmp_path, "fake", "pg_dump", accessory_config=config)
    failed = box.run(fail_on="shop")
    assert failed.returncode == 1, failed.stdout + failed.stderr
    assert ("Dump command failed with exit code 1, no snapshot stored (postgres-shop.sql)"
            in failed.stdout)
    assert box.snapshots() == ["/postgres-blog.sql"]
    assert [p[-1] for p in box.producers()] == ["shop", "blog"]


TWO_DBS = {"env": {"clear": {"POSTGRES_USER": "app"}},
           "backup": {"databases": ["shop", "blog"]}}


@pytest.mark.parametrize("which", _RESTICS)
@pytest.mark.parametrize("tiny", ["", "shop", "blog"])
def test_size_check_reads_every_snapshot(tmp_path: Path, which: str, tiny: str) -> None:
    """A run with two databases makes two snapshots and the size check reads both, by id.
    The old `stats latest` read only the last one, so a tiny first database passed."""
    box = Box(tmp_path, which, "pg_dump", accessory_config=TWO_DBS)
    run = box.run(TINY_ON=tiny)
    ids = box.snapshot_ids()
    assert sorted(ids) == ["/postgres-blog.sql", "/postgres-shop.sql"]
    # Every size read names a snapshot of this run by id; none asks for `latest`.
    asked = [c[1] for c in box.stats_calls()]
    assert all(c[0] == "stats" and "--json" in c for c in box.stats_calls())
    if not tiny:
        assert run.returncode == 0, run.stdout + run.stderr
        assert sorted(asked) == sorted(ids.values())
        # The summary adds the sizes of both snapshots (two dumps of 128 KiB).
        assert "Backup complete for postgres (262144 bytes)" in run.stdout
        return
    assert run.returncode == 1, run.stdout + run.stderr
    # The alert names the tiny snapshot's file, not the other one.
    assert f"ERROR: Snapshot suspiciously small (10 bytes, postgres-{tiny}.sql)" in run.stdout
    assert "Backup complete" not in run.stdout
    assert ids[f"/postgres-{tiny}.sql"] in asked
    assert not (tmp_path / "backup" / "locks" / "postgres.lock").exists()


@pytest.mark.parametrize("which", _RESTICS)
def test_size_check_parses_progress_line(tmp_path: Path, which: str) -> None:
    """restic 0.19.0 prints a progress line on stdout before the JSON of `stats --json`."""
    box = Box(tmp_path, which, "file")
    ok = box.run(PROGRESS_LINE="1")
    assert ok.returncode == 0, ok.stdout + ok.stderr
    assert "Backup complete for bay_shop_data (131072 bytes)" in ok.stdout
    assert len(box.stats_calls()) == 1
    # A tiny dump behind the progress line is still caught.
    tiny = box.run(PROGRESS_LINE="1", TINY_ON="cp")
    assert tiny.returncode == 1, tiny.stdout + tiny.stderr
    assert "Snapshot suspiciously small (10 bytes, bay_shop_data.tar)" in tiny.stdout


def test_size_check_fails_closed_when_stats_has_no_json(tmp_path: Path) -> None:
    """A stats call that prints no JSON document fails the run and names the file."""
    box = Box(tmp_path, "fake", "file")
    failed = box.run(STATS_NO_JSON="1")
    assert failed.returncode == 1, failed.stdout + failed.stderr
    assert "ERROR: Could not read the size of the snapshot (bay_shop_data.tar)" in failed.stdout
    assert "Backup complete" not in failed.stdout


@pytest.mark.parametrize("method", sorted(METHODS))
def test_size_check_never_asks_for_latest(method: str) -> None:
    """The rendered script reads sizes by snapshot id; no `stats latest` is left."""
    script = _render(method)
    assert "stats latest" not in script
    assert script.count("restic_json stats ") == 1
    assert "jq" in script and script.count('"$RESTIC" "$@" --json') == 1


def test_restic_failure_is_not_called_a_dump_failure(tmp_path: Path) -> None:
    box = Box(tmp_path, "fake", "pg_dump")
    failed = box.run(FAKE_RESTIC_FAIL="1")
    assert failed.returncode == 1
    assert "ERROR: Restic backup failed with exit code 1 (postgres.sql)" in failed.stdout
    assert "Dump command failed" not in failed.stdout
    assert "connection refused" in failed.stderr
    assert box.snapshots() == []


@pytest.mark.parametrize("method", sorted(METHODS))
def test_backup_hands_the_dump_to_restic(method: str) -> None:
    """The rendered script: restic runs every producer, nothing pipes into restic."""
    script = _render(method)
    assert script.count('"$RESTIC" backup') == 1  # in backup_from, nowhere else
    assert "--stdin-from-command -- \"$@\"" in script
    assert "--stdin \\" not in script and "PIPESTATUS" not in script
    body = script[script.index("# ── Backup ──"):script.index("# ── Error Checking")]
    assert body.count("backup_from ") == 1
    assert f'"${{ACCESSORY}}.{METHODS[method]["file"].rsplit(".", 1)[1]}"' in body


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
    ("restic 0.18.1 compiled with go1.24.1 on linux/amd64", False, True),
    # 0.19 prints a progress line before the JSON of `restic stats --json`; the
    # size check reads past it.
    ("restic 0.19.0 compiled with go1.26.4 on linux/amd64", False, True),
    ("restic 0.19.1 compiled with go1.26.4 on linux/amd64", False, True),
    ("restic 0.20.0 compiled with go1.27.0 on linux/amd64", False, False),
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
                 "Require restic 0.17 to 0.19 for the backup scripts")
    registered: dict[str, Any] = {"stdout": stdout, "rc": 0, "changed": False}
    if skipped:
        registered = {"skipped": True, "changed": False, "stdout": "", "rc": 0}
    proc = _play(tmp_path, [gate], {"_restic_version": registered,
                                    "backup_restic_bin": "/usr/local/bin/restic",
                                    "backup_restic_version": "0.17.3"})
    assert (proc.returncode == 0) is passes, proc.stdout[-2000:]
    if not passes:
        assert "0.17.0 or newer" in proc.stdout and "older than 0.20.0" in proc.stdout


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


def test_deploy_wait_names_volume_locks_without_stack_name(tmp_path: Path) -> None:
    """The wait task has `ignore_errors`, so a lock list that failed to render would be
    "wait for nothing". With no `stack_name` the list still names `bay_<volume>`."""
    wait = _task(ROOT / "roles" / "deploy_stack" / "tasks" / "main.yml",
                 "Wait for active backups to complete before deploy")
    wait = {**wait, "register": "_waited"}
    locks = tmp_path / "locks"
    locks.mkdir()
    out = tmp_path / "out.json"
    record = {"name": "Record what the wait covered", "ansible.builtin.copy": {
        "content": "{{ (_waited.results | default([]) | map(attribute='item') | list) | to_json }}",
        "dest": str(out), "mode": "0600"}}
    argv, env = _play_files(tmp_path, [wait, record], {
        "stack_dir": str(tmp_path),
        "backup_lock_dir": str(locks),
        "active_services": {"shop": {"image": "x"}},
        "volume_backups": {"shop_data": {"container": "shop", "path": "/app/data"}},
    })
    proc = subprocess.run(argv, cwd=tmp_path, env=env, capture_output=True, text=True, timeout=300)
    assert proc.returncode == 0, proc.stdout[-3000:]
    assert json.loads(out.read_text()) == ["bay_shop_data"]
