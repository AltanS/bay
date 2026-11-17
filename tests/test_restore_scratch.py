"""restore.yml with restore_into=scratch (2.24.0): a throwaway Postgres, rows counted.

Structure guards run everywhere. The end-to-end case runs the whole restore.yml
on localhost with a real Docker and a real Postgres image, and a fake restic
that serves a real pg_dump: it is skipped without Docker. It checks that the
live container is never written, the scratch container has no network and is
removed (also when the restore fails), and the report counts the rows.
"""
from __future__ import annotations

import getpass
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
SCRATCH = ROOT / "roles" / "backup" / "tasks" / "restore_scratch.yml"


def _tasks() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []

    def walk(items):
        for t in items:
            out.append(t)
            for key in ("block", "rescue", "always"):
                walk(t.get(key) or [])

    walk(yaml.safe_load(SCRATCH.read_text()))
    return out


def _task(name: str) -> dict[str, Any]:
    return next(t for t in _tasks() if t.get("name") == name)


def test_the_scratch_container_has_no_network_and_no_port():
    task = _task("Start the scratch container")
    assert task["ansible.builtin.command"]["argv"].startswith("{{ _scratch_run + ")
    argv = task["vars"]["_scratch_run"]
    assert argv[argv.index("--network") + 1] == "none"
    assert not any(a in ("-p", "--publish", "-P", "--publish-all") for a in argv)
    assert argv[argv.index("--name") + 1] == "{{ _scratch_name }}"


def test_every_scratch_container_carries_its_expiry():
    """Second review: the 6-hourly freshness run removes an expired one, also
    after an interrupted test or a reboot that lost the transient timer."""
    run = _task("Start the scratch container")["vars"]["_scratch_run"]
    assert "bay.restore_test.expires={{ _scratch_expires }}" in run


def test_every_exec_and_restore_targets_the_scratch_container():
    for t in _tasks():
        body = yaml.safe_dump(t)
        if t.get("name") == "Count the rows of every leaf table of the live database":
            # The one read of the live container's tables: read-only, see
            # tests/test_restore_live_counts.py.
            assert "BEGIN READ ONLY ISOLATION LEVEL REPEATABLE READ" in body
            continue
        if t.get("name") == "Read the size of the live database":
            assert "SELECT pg_database_size(current_database())" in body
            continue
        if t.get("name") == "Read the roles of the live server":
            assert "SELECT rolname FROM pg_roles" in body
            continue
        argv = (t.get("ansible.builtin.command") or {}).get("argv") or []
        cmd = str((t.get("ansible.builtin.shell") or {}).get("cmd", ""))
        if isinstance(argv, str) and "'docker', 'exec'" in argv:
            assert argv.startswith("{{ ['docker', 'exec', _scratch_name,"), t["name"]
        if argv[:2] == ["docker", "exec"]:
            assert argv[2] == "{{ _scratch_name }}", t["name"]
        if "docker exec" in cmd:
            assert "docker exec -i {{ _scratch_name | quote }}" in cmd, t["name"]


def test_the_name_guard_and_the_removal_always_run():
    guard = _task("Never let the scratch container be the live one")["ansible.builtin.assert"]["that"]
    assert "_scratch_name.startswith('bay-restore-test-')" in guard
    assert "_scratch_name != accessory" in guard
    block = next(t for t in yaml.safe_load(SCRATCH.read_text()) if "block" in t)
    assert [t["name"] for t in block["always"]] == ["Remove the scratch container"]


# ── end to end, real Docker ─────────────────────────────────────────────────


def _docker_ok() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        r = subprocess.run(["docker", "info", "-f", "{{.DockerRootDir}}"],
                           capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0 and r.stdout.strip().startswith("/")


IMAGE = os.environ.get("BAY_TEST_PG_IMAGES", "postgres:17-alpine").split(",")[0].strip()

FAKE_RESTIC = r"""#!/usr/bin/env python3
import json, os, sys
args = [a for a in sys.argv[1:]]
dump = os.environ["DUMP_PATH"]
if args[0] == "snapshots":
    print(json.dumps([{"short_id": "ab12cd34", "id": "ab12cd34" + "0" * 56,
                       "paths": [os.environ["DUMP_FILE"]], "time": "2026-11-17T03:00:00Z",
                       "tags": ["pg_dump", "db:app"]}]))
elif args[0] == "stats":
    print(json.dumps({"total_size": os.path.getsize(dump), "total_file_count": 1}))
elif args[0] == "dump":
    assert args[2] == os.environ["DUMP_FILE"], args
    sys.stdout.buffer.write(open(dump, "rb").read())
else:
    sys.exit(3)
"""


def _psql(name: str, sql: str) -> str:
    r = subprocess.run(["docker", "exec", "-i", name, "psql", "-X", "-qAt", "-v", "ON_ERROR_STOP=1",
                        "-U", "app", "-d", "app"], input=sql, capture_output=True, text=True,
                       timeout=60)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


@pytest.fixture
def live():
    if not _docker_ok():
        pytest.skip("no Docker on this host")
    name = f"bay-e2e-live-{uuid.uuid4().hex[:8]}"
    r = subprocess.run(["docker", "run", "-d", "--name", name, "--network", "none",
                        "-e", "POSTGRES_USER=app", "-e", "POSTGRES_DB=app",
                        "-e", "POSTGRES_HOST_AUTH_METHOD=trust", IMAGE],
                       capture_output=True, text=True, timeout=600)
    if r.returncode != 0:
        pytest.skip(f"cannot start {IMAGE}: {r.stderr.strip()[:200]}")
    try:
        for _ in range(180):
            if subprocess.run(["docker", "exec", name, "pg_isready", "-h", "127.0.0.1", "-U", "app"],
                              capture_output=True).returncode == 0:
                break
            time.sleep(0.5)
        yield name
    finally:
        subprocess.run(["docker", "rm", "-f", "-v", name], capture_output=True, timeout=60)


def _scratch_left(target: str) -> list[str]:
    out = subprocess.run(["docker", "ps", "-a", "--filter", "label=bay.restore_test=1",
                          "--filter", f"label=bay.restore_test.target={target}",
                          "--format", "{{.Names}}"], capture_output=True, text=True).stdout
    return out.split()


FAKE_SYSTEMD_RUN = """#!/bin/sh
printf '%s\\n' "$*" >> "$SYSTEMD_RUN_LOG"
"""


def _play(tmp_path: Path, live: str, dump: bytes, file: str, keep: bool = False,
          fmt: str = "custom", extra: dict[str, Any] | None = None,
          play_vars: dict[str, Any] | None = None):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "restic").write_text(FAKE_RESTIC)
    (bin_dir / "restic").chmod(0o755)
    (bin_dir / "systemd-run").write_text(FAKE_SYSTEMD_RUN)
    (bin_dir / "systemd-run").chmod(0o755)
    (tmp_path / "dump.bin").write_bytes(dump)
    (tmp_path / "stack" / "backup" / "status").mkdir(parents=True)
    (tmp_path / "hosts").write_text("[testing]\nlocalhost ansible_connection=local\n")
    (tmp_path / "vars.json").write_text(json.dumps({
        "accessory": live,
        "stack_name": "bay",
        "stack_dir": str(tmp_path / "stack"),
        "app_user": getpass.getuser(),
        "ansible_become": False,
        "secrets": {},
        "accessories": {live: {"image": IMAGE, "mem_limit": "512m",
                               "backup": {"method": "pg_dump", "format": fmt},
                               "env": {"clear": {"POSTGRES_USER": "app", "POSTGRES_DB": "app"}},
                               **(extra or {})}},
        "backup_restic_bin": str(bin_dir / "restic"),
        "restore_into": "scratch",
        "restore_keep": keep,
        "restore_test_out": str(tmp_path / "report.json"),
        "skip_pre_backup": True,
        **(play_vars or {}),
    }))
    (tmp_path / "ansible.cfg").write_text("[defaults]\n")
    env = {**os.environ, "DUMP_PATH": str(tmp_path / "dump.bin"), "DUMP_FILE": file,
           "PATH": f"{bin_dir}:{os.environ['PATH']}",
           "SYSTEMD_RUN_LOG": str(tmp_path / "systemd-run.log"),
           "ANSIBLE_CONFIG": str(tmp_path / "ansible.cfg"),
           "ANSIBLE_ROLES_PATH": str(ROOT / "roles"), "ANSIBLE_NOCOLOR": "1",
           "ANSIBLE_LOCALHOST_WARNING": "0", "ANSIBLE_PYTHON_INTERPRETER": sys.executable}
    return subprocess.run(
        [sys.executable, "-m", "ansible.cli.playbook", "-i", str(tmp_path / "hosts"),
         "-e", "target_host=testing", "-e", f"@{tmp_path / 'vars.json'}",
         str(ROOT / "restore.yml")],
        capture_output=True, text=True, env=env, cwd=tmp_path, timeout=900,
    )


def test_scratch_restore_counts_rows_and_never_touches_the_live_database(tmp_path, live):
    _psql(live, "CREATE TABLE notes (id integer PRIMARY KEY, body text);"
                "INSERT INTO notes SELECT g, 'row ' || g FROM generate_series(1, 300) g;"
                "CREATE TABLE tags (id integer); INSERT INTO tags VALUES (1), (2);"
                "ANALYZE;")
    dump = subprocess.run(["docker", "exec", live, "pg_dump", "-Fc", "-Z", "0", "-U", "app", "app"],
                          capture_output=True, timeout=60, check=True).stdout
    # The live database moves on after the snapshot: the scratch must show the snapshot.
    _psql(live, "DELETE FROM notes WHERE id > 10;")
    proc = _play(tmp_path, live, dump, f"/{live}.dump")
    assert proc.returncode == 0, proc.stdout[-4000:]
    report = json.loads((tmp_path / "report.json").read_text())
    assert report["counts"] == {"public.notes": 300, "public.tags": 2}
    assert report["tables"] == 2 and report["rows"] == 302
    # 2.24.1: the live side is counted exactly (10 rows left), not estimated.
    assert report["live_counts"] == {"public.notes": 10, "public.tags": 2}
    assert report["estimated"] == [] and report["verdict"] == "ok"
    assert report["snapshot_time"] == "2026-11-17T03:00:00Z" and report["live_counted_at"]
    assert report["snapshot"] == "ab12cd34"
    assert _psql(live, "SELECT count(*) FROM notes;") == "10"
    assert _scratch_left(live) == []
    kept = json.loads((tmp_path / "stack" / "backup" / "status" / f"{live}.restore-test.json").read_text())
    assert kept["rows"] == 302


def test_a_failed_scratch_restore_still_removes_the_container(tmp_path, live):
    proc = _play(tmp_path, live, b"PGDMP-this-is-not-a-dump", f"/{live}.dump")
    assert proc.returncode != 0
    assert "Restore the snapshot into the scratch container" in proc.stdout
    assert _scratch_left(live) == []


# ── review fixes ────────────────────────────────────────────────────────────


def test_a_plain_dump_owned_by_another_role_restores_into_scratch(tmp_path, live):
    """Review fix: a plain dump holds `ALTER TABLE ... OWNER TO <role>` and
    GRANTs. The scratch server had no such role, so psql stopped at the first
    one (ON_ERROR_STOP, exit 3) and a plain dump never passed a restore test."""
    _psql(live, "CREATE ROLE shop_owner NOLOGIN; CREATE ROLE shop_reader NOLOGIN;"
                "CREATE TABLE notes (id integer PRIMARY KEY, body text);"
                "INSERT INTO notes SELECT g, 'row ' || g FROM generate_series(1, 40) g;"
                "ALTER TABLE notes OWNER TO shop_owner;"
                "GRANT SELECT ON notes TO shop_reader;")
    dump = subprocess.run(["docker", "exec", live, "pg_dump", "-U", "app", "app"],
                          capture_output=True, timeout=60, check=True).stdout
    assert b"OWNER TO shop_owner" in dump
    proc = _play(tmp_path, live, dump, f"/{live}.sql", fmt="plain")
    assert proc.returncode == 0, proc.stdout[-4000:]
    report = json.loads((tmp_path / "report.json").read_text())
    assert report["counts"] == {"public.notes": 40}
    assert _scratch_left(live) == []


def test_a_kept_scratch_container_runs_like_the_live_one_and_goes_away_on_a_timer(tmp_path, live):
    """Review fixes: the scratch container takes the compiled memory key
    (mem_limit) and the accessory command; a kept one gets a systemd timer
    that removes it after restore_keep_hours (24)."""
    _psql(live, "CREATE TABLE notes (id integer); INSERT INTO notes VALUES (1);")
    dump = subprocess.run(["docker", "exec", live, "pg_dump", "-Fc", "-Z", "0", "-U", "app", "app"],
                          capture_output=True, timeout=60, check=True).stdout
    proc = _play(tmp_path, live, dump, f"/{live}.dump", keep=True,
                 extra={"mem_limit": "300m", "command": "postgres -c max_connections=37"})
    kept = _scratch_left(live)
    try:
        assert proc.returncode == 0, proc.stdout[-4000:]
        assert len(kept) == 1
        name = kept[0]
        mem = subprocess.run(["docker", "inspect", "-f", "{{.HostConfig.Memory}}", name],
                             capture_output=True, text=True, check=True).stdout.strip()
        assert mem == str(300 * 1024 * 1024)
        conns = subprocess.run(["docker", "exec", name, "psql", "-qAt", "-U", "app", "-d", "app",
                                "-c", "SHOW max_connections"],
                               capture_output=True, text=True, check=True).stdout.strip()
        assert conns == "37"
        expires = subprocess.run(
            ["docker", "inspect", "-f", '{{index .Config.Labels "bay.restore_test.expires"}}', name],
            capture_output=True, text=True, check=True).stdout.strip()
        assert abs(int(expires) - (int(time.time()) + 24 * 3600)) < 900
        timer = (tmp_path / "systemd-run.log").read_text()
        assert "--on-active=24h" in timer
        assert f"docker rm -f -v {name}" in timer
        assert "removed automatically" in proc.stdout
    finally:
        for name in kept:
            subprocess.run(["docker", "rm", "-f", "-v", name], capture_output=True, timeout=60)


# ── exact counts against exact counts (2.24.1) ──────────────────────────────

PARTITIONED = """
CREATE TABLE events (id integer, day date) PARTITION BY RANGE (day);
CREATE TABLE events_2026_10 PARTITION OF events FOR VALUES FROM ('2026-10-01') TO ('2026-11-01');
CREATE TABLE events_2026_11 PARTITION OF events FOR VALUES FROM ('2026-11-01') TO ('2026-12-01');
INSERT INTO events SELECT g, '2026-10-15' FROM generate_series(1, 120) g;
INSERT INTO events SELECT g, '2026-11-15' FROM generate_series(1, 80) g;
CREATE TABLE notes (id integer PRIMARY KEY);
INSERT INTO notes SELECT g FROM generate_series(1, 50) g;
"""


def _dump(live: str) -> bytes:
    return subprocess.run(["docker", "exec", live, "pg_dump", "-Fc", "-Z", "0", "-U", "app", "app"],
                          capture_output=True, timeout=60, check=True).stdout


def test_a_partitioned_parent_is_summed_and_rows_written_after_the_snapshot_pass(tmp_path, live):
    _psql(live, PARTITIONED)
    dump = _dump(live)
    # Written after the snapshot: a small gap, explained.
    _psql(live, "INSERT INTO events SELECT g, '2026-11-20' FROM generate_series(1, 30) g;"
                "INSERT INTO notes SELECT g FROM generate_series(51, 60) g;")
    proc = _play(tmp_path, live, dump, f"/{live}.dump", play_vars={"restore_test": True})
    assert proc.returncode == 0, proc.stdout[-4000:]
    report = json.loads((tmp_path / "report.json").read_text())
    assert report["counts"] == {"public.events_2026_10": 120, "public.events_2026_11": 80,
                                "public.notes": 50}
    assert report["tables"] == 3 and report["rows"] == 250, "the parent is not counted twice"
    assert report["parents"]["public.events"]["restored"] == 200
    assert report["parents"]["public.events"]["live"] == 230
    assert report["live_rows"] == 290 and report["verdict"] == "ok" and report["failures"] == []
    assert {r["table"] for r in report["non_ok"]} == set(), report["non_ok"]
    assert _scratch_left(live) == []


def test_a_restore_test_fails_on_rows_the_restore_lacks_and_still_cleans_up(tmp_path, live):
    _psql(live, PARTITIONED)
    dump = _dump(live)
    # 5,000 rows exist live that the snapshot does not hold: over both limits.
    _psql(live, "INSERT INTO notes SELECT g FROM generate_series(100, 5099) g;")
    proc = _play(tmp_path, live, dump, f"/{live}.dump", play_vars={"restore_test": True})
    assert proc.returncode != 0
    assert "public.notes" in proc.stdout and "lost rows" in proc.stdout
    report = json.loads((tmp_path / "report.json").read_text())
    assert report["failures"] == ["public.notes"] and report["verdict"] == "fail"
    row = report["non_ok"][0]
    assert (row["restored"], row["live"], row["delta"], row["method"]) == (50, 5050, -5000, "exact")
    assert _scratch_left(live) == []


def test_the_limits_come_from_fleet_variables(tmp_path, live):
    _psql(live, PARTITIONED)
    dump = _dump(live)
    _psql(live, "INSERT INTO notes SELECT g FROM generate_series(100, 5099) g;")
    proc = _play(tmp_path, live, dump, f"/{live}.dump",
                 play_vars={"restore_test": True, "restore_test_max_loss_pct": 99.5,
                            "restore_test_max_loss_rows": 10000})
    assert proc.returncode == 0, proc.stdout[-4000:]
    bad = tmp_path / "bad"
    bad.mkdir()
    proc = _play(bad, live, dump, f"/{live}.dump",
                 play_vars={"restore_test": True, "restore_test_max_loss_pct": "lots"})
    assert proc.returncode != 0 and "Nothing was started" in proc.stdout
    assert _scratch_left(live) == []


def test_a_scratch_restore_that_is_not_a_test_only_reports_a_loss(tmp_path, live):
    _psql(live, PARTITIONED)
    dump = _dump(live)
    _psql(live, "INSERT INTO notes SELECT g FROM generate_series(100, 5099) g;")
    proc = _play(tmp_path, live, dump, f"/{live}.dump")
    assert proc.returncode == 0, proc.stdout[-4000:]
    assert json.loads((tmp_path / "report.json").read_text())["verdict"] == "fail"


def test_a_count_that_times_out_falls_back_to_the_estimate(tmp_path, live):
    _psql(live, PARTITIONED + "ANALYZE;")
    dump = _dump(live)
    # Another session holds an exclusive lock on one live table: its count
    # waits for the lock and hits statement_timeout. The test goes on.
    holder = subprocess.Popen(
        ["docker", "exec", "-i", live, "psql", "-X", "-qAt", "-U", "app", "-d", "app"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        holder.stdin.write("BEGIN; LOCK TABLE notes IN ACCESS EXCLUSIVE MODE; SELECT 'locked';"
                           "SELECT pg_sleep(60);\n")
        holder.stdin.flush()
        assert holder.stdout.readline().strip() == "locked"
        started = time.time()
        proc = _play(tmp_path, live, dump, f"/{live}.dump",
                     play_vars={"restore_test": True, "restore_test_count_timeout": 1})
        assert time.time() - started < 300
    finally:
        holder.kill()
    assert proc.returncode == 0, proc.stdout[-4000:]
    report = json.loads((tmp_path / "report.json").read_text())
    assert report["estimated"] == ["public.notes"]
    row = next(r for r in report["non_ok"] if r["table"] == "public.notes")
    assert row["method"] == "estimate" and row["restored"] == 50
    assert report["live_counts"]["public.events_2026_10"] == 120
    assert report["verdict"] == "ok" and _scratch_left(live) == []


def test_an_unreadable_live_database_gives_an_unverified_report_and_still_cleans_up(tmp_path, live):
    """2.24.1: the live read fails (container down): nothing is compared. The
    play does not fail, the report says unverified, the scratch is removed."""
    _psql(live, PARTITIONED)
    dump = _dump(live)
    subprocess.run(["docker", "stop", live], capture_output=True, timeout=120, check=True)
    proc = _play(tmp_path, live, dump, f"/{live}.dump", play_vars={"restore_test": True})
    assert proc.returncode == 0, proc.stdout[-4000:]
    report = json.loads((tmp_path / "report.json").read_text())
    assert report["verdict"] == "unverified" and report["compared"] is False
    assert report["tables"] == 3 and report["failures"] == []
    assert "nothing was compared" in proc.stdout
    assert "Compose restore test unverified alert" in proc.stdout
    assert _scratch_left(live) == []
