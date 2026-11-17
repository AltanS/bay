"""The pg_dump restore pipes the dump into psql with `docker exec -i`.

Before 2.23.2 the task ran `restic dump ... | docker exec <accessory> psql`.
Without `-i` Docker attaches no stdin: restic got a broken pipe (exit 141,
seen on a test box), or psql read an empty script, exited 0, and the check
after it ("has tables") passed on the untouched database. Either way
`bay backup restore <env> <pg_dump target>` restored nothing. psql now also runs with ON_ERROR_STOP and --single-transaction, so a
statement that fails stops the restore and rolls it back.

The task is lifted out of restore.yml and run in a real ansible-playbook on
localhost. In the first tests a fake docker does what docker does with stdin:
it forwards it to the command only with `-i`. The last test runs the task
against a real Postgres container (skipped when no container runtime answers).
"""

from __future__ import annotations

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

SQL = (
    "CREATE TABLE notes (id integer PRIMARY KEY, body text);\n"
    "INSERT INTO notes VALUES (1, 'one'), (2, 'two'), (3, 'three');\n"
)

FAKE_RESTIC = r"""#!/usr/bin/env python3
import os, sys
args = sys.argv[1:]
if args[:1] != ["dump"] or args[2] != "/" + os.environ["DUMP_FILE"]:
    sys.stderr.write("fake restic: unexpected call: %r\n" % (args,))
    sys.exit(3)
sys.stdout.buffer.write(open(os.environ["SNAPSHOT_FILE"], "rb").read())
"""

# `docker exec [-i] <container> <cmd...>`. Docker forwards stdin to the command
# only with -i; without it the command reads nothing. The fake records the
# flags, the command and what the command read.
FAKE_DOCKER = r"""#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
if args[:1] == ["ps"]:
    print("\n".join(os.environ.get("RUNNING", "").split()))
    sys.exit(0)
if args[:1] in (["stop"], ["start"]):
    with open(os.environ["DOCKER_LOG"], "a") as fh:
        fh.write(json.dumps({"lifecycle": args[0], "names": args[1:]}) + "\n")
    sys.exit(0)
if args[:1] != ["exec"]:
    sys.exit(97)
rest = args[1:]
flags = []
while rest and rest[0].startswith("-"):
    flag = rest.pop(0)
    flags.append(flag)
    if flag == "-e":
        flags.append(rest.pop(0))
container, cmd = rest[0], rest[1:]
got = sys.stdin.read() if "-i" in flags else ""
lock = os.environ.get("LOCK_FILE", "")
with open(os.environ["DOCKER_LOG"], "a") as fh:
    fh.write(json.dumps({"flags": flags, "container": container, "cmd": cmd, "stdin": got,
                         "locked": bool(lock) and os.path.exists(lock)}) + "\n")
if "pg_stat_activity" in got:
    # The session check: other sessions of the database, until terminated.
    # Background workers (autovacuum, extension workers) are sessions of the
    # database too; only a query that keeps to client backends skips them.
    workers = "" if "backend_type = 'client backend'" in got else os.environ.get("WORKERS", "")
    if "pg_terminate_backend" in got:
        open(os.environ["DOCKER_LOG"] + ".terminated", "w").close()
        sys.exit(0)
    rows = workers
    if not os.path.exists(os.environ["DOCKER_LOG"] + ".terminated"):
        rows = ";".join(x for x in (os.environ.get("CONNECTIONS", ""), workers) if x)
    sys.stdout.write(rows.replace(";", "\n"))
    sys.exit(0)
sys.exit(int(os.environ.get("FAKE_PSQL_EXIT", "0")))
"""


def _all_tasks(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for task in tasks:
        out.append(task)
        for key in ("block", "rescue", "always"):
            out.extend(_all_tasks(task.get(key) or []))
    return out


def _no_become(node: Any) -> Any:
    """The test runs as the current user: drop become from every task."""
    if isinstance(node, dict):
        return {k: _no_become(v) for k, v in node.items() if k not in ("become", "become_user")}
    if isinstance(node, list):
        return [_no_become(v) for v in node]
    return node


def _restore_task() -> dict[str, Any]:
    """The pg restore block: list and stop the clients, restore, start them again."""
    play = yaml.safe_load((ROOT / "restore.yml").read_text())[0]
    return next(
        t for t in _all_tasks(play["tasks"])
        if t.get("name") == "Restore PostgreSQL database with its clients stopped"
    )


def _run(
    tmp_path: Path,
    *,
    accessory: str,
    docker: str | None,
    extra_vars: dict[str, Any] | None = None,
    **env_extra: str,
) -> subprocess.CompletedProcess[str]:
    """Run the lifted task. ``docker`` is the fake's source, or None for the real CLI."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    scripts = tmp_path / "scripts"
    scripts.mkdir(exist_ok=True)
    # The pre-restore safety backup: it records when it ran.
    (scripts / f"backup-{accessory}.sh").write_text(
        "#!/bin/sh\n"
        'printf \'{"prebackup": "%s"}\\n\' "$RESTIC_TAG_EXTRA" >> "$DOCKER_LOG"\n')
    (scripts / f"backup-{accessory}.sh").chmod(0o755)
    (bin_dir / "restic").write_text(FAKE_RESTIC)
    (bin_dir / "restic").chmod(0o755)
    if docker is not None:
        (bin_dir / "docker").write_text(docker)
        (bin_dir / "docker").chmod(0o755)
    play = [{"hosts": "localhost", "connection": "local", "gather_facts": False,
             "tasks": _no_become([_restore_task()])}]
    (tmp_path / "play.yml").write_text(yaml.safe_dump(play))
    (tmp_path / "vars.json").write_text(json.dumps({
        "accessory": accessory,
        "_restore_method": "pg_dump",
        "_snap": {"short_id": "ab12cd34"},
        "_backup_restic_bin": str(bin_dir / "restic"),
        "_pg_user": "app",
        "_pg_database": "app",
        "_pg_file": "/" + env_extra.get("DUMP_FILE", f"{accessory}.sql"),
        "_pg_custom": env_extra.get("DUMP_FILE", "").endswith(".dump"),
        "_restore_stop_clients": [],
        "_into": "live",
        "_restore_lock": str(tmp_path / "locks" / f"restore-{accessory}.lock"),
        "_restore_terminate": False,
        "_restore_lock_timeout": "30s",
        "_backup_scripts_dir": str(scripts),
        "_skip_pre_backup": False,
        "_restic_env": {"RESTIC_REPOSITORY": "r", "RESTIC_PASSWORD": "pw-placeholder"},
        **(extra_vars or {}),
    }))
    (tmp_path / "ansible.cfg").write_text("[defaults]\n")
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "DUMP_FILE": f"{accessory}.sql",
        "SNAPSHOT_FILE": str(tmp_path / "snapshot.sql"),
        "DOCKER_LOG": str(tmp_path / "docker.log"),
        "LOCK_FILE": str(tmp_path / "locks" / f"restore-{accessory}.lock"),
        "ANSIBLE_CONFIG": str(tmp_path / "ansible.cfg"),
        "ANSIBLE_NOCOLOR": "1",
        "ANSIBLE_LOCALHOST_WARNING": "0",
        "ANSIBLE_INVENTORY_UNPARSED_WARNING": "0",
        "ANSIBLE_PYTHON_INTERPRETER": sys.executable,
        **env_extra,
    }
    return subprocess.run(
        [sys.executable, "-m", "ansible.cli.playbook", "-i", "localhost,",
         "-e", f"@{tmp_path / 'vars.json'}", str(tmp_path / "play.yml")],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=300,
    )


def _log(tmp_path: Path) -> list[dict[str, Any]]:
    log = tmp_path / "docker.log"
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def _calls(tmp_path: Path) -> list[dict[str, Any]]:
    """The restore calls (not the session check)."""
    return [c for c in _log(tmp_path) if "cmd" in c and "pg_stat_activity" not in c["stdin"]]


def _order(tmp_path: Path) -> list[str]:
    """What happened, in order: stop, sessions, prebackup, restore, start."""
    out = []
    for c in _log(tmp_path):
        if "lifecycle" in c:
            out.append(c["lifecycle"])
        elif "prebackup" in c:
            out.append("prebackup")
        elif "pg_stat_activity" in c.get("stdin", ""):
            out.append("terminate" if "pg_terminate_backend" in c["stdin"] else "sessions")
        elif "cmd" in c:
            out.append("restore")
    return out


def _session_checks(tmp_path: Path) -> list[dict[str, Any]]:
    return [c for c in _log(tmp_path) if "cmd" in c and "pg_stat_activity" in c["stdin"]]


def _lifecycle(tmp_path: Path) -> list[tuple[str, list[str]]]:
    return [(c["lifecycle"], c["names"]) for c in _log(tmp_path) if "lifecycle" in c]


def test_pg_restore_feeds_the_dump_to_psql(tmp_path: Path) -> None:
    (tmp_path / "snapshot.sql").write_text(SQL)
    proc = _run(tmp_path, accessory="postgres", docker=FAKE_DOCKER)
    assert proc.returncode == 0, proc.stdout[-3000:] + proc.stderr[-3000:]
    [call] = _calls(tmp_path)
    assert "-i" in call["flags"]
    assert call["container"] == "postgres"
    # psql read the whole dump. Without -i it read nothing (the old bug).
    assert call["stdin"] == SQL
    cmd = call["cmd"]
    assert cmd[0] == "psql"
    assert "ON_ERROR_STOP=1" in cmd and "--single-transaction" in cmd
    assert cmd[cmd.index("-U") + 1] == "app" and cmd[cmd.index("-d") + 1] == "app"


def test_pg_restore_reads_the_file_of_the_named_database(tmp_path: Path) -> None:
    (tmp_path / "snapshot.sql").write_text(SQL)
    proc = _run(
        tmp_path, accessory="postgres", docker=FAKE_DOCKER,
        extra_vars={"database": "shop", "_pg_database": "shop"},
        DUMP_FILE="postgres-shop.sql",
    )
    assert proc.returncode == 0, proc.stdout[-3000:] + proc.stderr[-3000:]
    [call] = _calls(tmp_path)
    assert call["stdin"] == SQL
    assert call["cmd"][call["cmd"].index("-d") + 1] == "shop"


def test_pg_restore_fails_when_psql_stops(tmp_path: Path) -> None:
    """ON_ERROR_STOP makes psql exit 3 on the first error; the task must fail."""
    (tmp_path / "snapshot.sql").write_text(SQL)
    proc = _run(tmp_path, accessory="postgres", docker=FAKE_DOCKER, FAKE_PSQL_EXIT="3")
    assert proc.returncode != 0
    assert "Restore PostgreSQL database" in proc.stdout


def test_a_custom_format_dump_is_restored_with_pg_restore_clean(tmp_path: Path) -> None:
    """2.24.0: a `.dump` file (pg_dump -Fc) goes to pg_restore, which drops each
    object before it creates it, in one transaction, and stops at an error."""
    (tmp_path / "snapshot.sql").write_bytes(b"PGDMP-binary")
    proc = _run(tmp_path, accessory="postgres", docker=FAKE_DOCKER, DUMP_FILE="postgres.dump")
    assert proc.returncode == 0, proc.stdout[-3000:] + proc.stderr[-3000:]
    [call] = _calls(tmp_path)
    cmd = call["cmd"]
    assert cmd[0] == "pg_restore" and "-i" in call["flags"]
    for flag in ("--clean", "--if-exists", "--single-transaction", "--exit-on-error"):
        assert flag in cmd
    assert cmd[cmd.index("-d") + 1] == "app"
    assert call["stdin"] == "PGDMP-binary"


def test_live_restore_stops_only_the_named_running_clients_and_starts_them(tmp_path: Path) -> None:
    (tmp_path / "snapshot.sql").write_text(SQL)
    proc = _run(
        tmp_path, accessory="postgres", docker=FAKE_DOCKER,
        extra_vars={"_restore_stop_clients": ["shop", "shop-worker", "shop-cron"]},
        RUNNING="postgres shop shop-worker other-app",
    )
    assert proc.returncode == 0, proc.stdout[-3000:] + proc.stderr[-3000:]
    # shop-cron does not run: not stopped, not started. other-app is not named.
    assert _lifecycle(tmp_path) == [("stop", ["shop", "shop-worker"]),
                                    ("start", ["shop", "shop-worker"])]


def test_the_clients_are_started_again_when_the_restore_fails(tmp_path: Path) -> None:
    (tmp_path / "snapshot.sql").write_text(SQL)
    proc = _run(
        tmp_path, accessory="postgres", docker=FAKE_DOCKER,
        extra_vars={"_restore_stop_clients": ["shop"]},
        RUNNING="postgres shop", FAKE_PSQL_EXIT="3",
    )
    assert proc.returncode != 0
    assert _lifecycle(tmp_path) == [("stop", ["shop"]), ("start", ["shop"])]


def test_no_clients_named_stops_nothing(tmp_path: Path) -> None:
    (tmp_path / "snapshot.sql").write_text(SQL)
    proc = _run(tmp_path, accessory="postgres", docker=FAKE_DOCKER, RUNNING="postgres shop")
    assert proc.returncode == 0, proc.stdout[-3000:]
    assert _lifecycle(tmp_path) == []


def test_live_restore_stops_only_the_clients_of_the_restored_database(tmp_path: Path) -> None:
    """Review fix: a client bound to another database of the server keeps running."""
    (tmp_path / "snapshot.sql").write_text(SQL)
    proc = _run(
        tmp_path, accessory="postgres", docker=FAKE_DOCKER,
        extra_vars={"_restore_stop_clients": [{"name": "shop", "database": ""},
                                              {"name": "blog", "database": "blog"},
                                              {"name": "app-web", "database": "app"}]},
        RUNNING="postgres shop blog app-web",
    )
    assert proc.returncode == 0, proc.stdout[-3000:] + proc.stderr[-3000:]
    assert _lifecycle(tmp_path) == [("stop", ["shop", "app-web"]), ("start", ["shop", "app-web"])]


def test_live_restore_refuses_while_other_sessions_use_the_database(tmp_path: Path) -> None:
    """Review fix: a session that is still open after the clients stopped
    (another box, a cron, a psql) would block or race the restore. It is named
    and the restore does not start; the stopped clients start again."""
    (tmp_path / "snapshot.sql").write_text(SQL)
    proc = _run(
        tmp_path, accessory="postgres", docker=FAKE_DOCKER,
        extra_vars={"_restore_stop_clients": ["shop"]},
        RUNNING="postgres shop", CONNECTIONS="4242|report-cron|10.0.0.5|app",
    )
    assert proc.returncode != 0
    assert "report-cron" in proc.stdout and "--terminate-connections" in proc.stdout
    assert _calls(tmp_path) == []
    assert len(_session_checks(tmp_path)) == 1
    assert _lifecycle(tmp_path) == [("stop", ["shop"]), ("start", ["shop"])]


def test_terminate_connections_ends_the_sessions_then_restores(tmp_path: Path) -> None:
    (tmp_path / "snapshot.sql").write_text(SQL)
    proc = _run(
        tmp_path, accessory="postgres", docker=FAKE_DOCKER,
        extra_vars={"_restore_terminate": True},
        CONNECTIONS="4242|report-cron|10.0.0.5|app",
    )
    assert proc.returncode == 0, proc.stdout[-3000:] + proc.stderr[-3000:]
    assert any("pg_terminate_backend" in c["stdin"] for c in _session_checks(tmp_path))
    [call] = _calls(tmp_path)
    assert call["stdin"] == SQL


def test_the_restore_waits_on_no_lock_silently(tmp_path: Path) -> None:
    (tmp_path / "snapshot.sql").write_text(SQL)
    proc = _run(tmp_path, accessory="postgres", docker=FAKE_DOCKER)
    assert proc.returncode == 0, proc.stdout[-3000:]
    [call] = _calls(tmp_path)
    assert "PGOPTIONS=-c lock_timeout=30s" in call["flags"]


@pytest.mark.parametrize("psql_exit", ["0", "3"])
def test_the_restore_lock_is_held_during_the_restore_and_removed_after(
        tmp_path: Path, psql_exit: str) -> None:
    """Review fix: the nightly prune (docker system prune -af removes stopped
    containers) and a deploy must not act while the clients are stopped."""
    (tmp_path / "snapshot.sql").write_text(SQL)
    proc = _run(tmp_path, accessory="postgres", docker=FAKE_DOCKER,
                extra_vars={"_restore_stop_clients": ["shop"]},
                RUNNING="postgres shop", FAKE_PSQL_EXIT=psql_exit)
    assert (proc.returncode == 0) == (psql_exit == "0"), proc.stdout[-3000:]
    [call] = _calls(tmp_path)
    assert call["locked"] is True
    assert not (tmp_path / "locks" / "restore-postgres.lock").exists()
    assert _lifecycle(tmp_path) == [("stop", ["shop"]), ("start", ["shop"])]


def test_the_safety_backup_runs_after_the_stop_and_the_session_check(tmp_path: Path) -> None:
    """Second review: before, the pre-restore backup ran before the clients
    stopped, so writes made in between were lost on undo."""
    (tmp_path / "snapshot.sql").write_text(SQL)
    proc = _run(tmp_path, accessory="postgres", docker=FAKE_DOCKER,
                extra_vars={"_restore_stop_clients": ["shop"]}, RUNNING="postgres shop")
    assert proc.returncode == 0, proc.stdout[-3000:] + proc.stderr[-3000:]
    assert _order(tmp_path) == ["stop", "sessions", "prebackup", "restore", "start"]
    assert {"prebackup": "pre-restore"} in _log(tmp_path)


def test_a_refused_restore_pays_for_no_safety_backup(tmp_path: Path) -> None:
    (tmp_path / "snapshot.sql").write_text(SQL)
    proc = _run(tmp_path, accessory="postgres", docker=FAKE_DOCKER,
                extra_vars={"_restore_stop_clients": ["shop"]}, RUNNING="postgres shop",
                CONNECTIONS="4242|report-cron|10.0.0.5|app")
    assert proc.returncode != 0
    assert _order(tmp_path) == ["stop", "sessions", "start"]


def test_background_workers_do_not_count_as_sessions(tmp_path: Path) -> None:
    """Second review: autovacuum and extension workers are in pg_stat_activity
    too; only client backends block the restore."""
    (tmp_path / "snapshot.sql").write_text(SQL)
    proc = _run(tmp_path, accessory="postgres", docker=FAKE_DOCKER,
                WORKERS="77|pg_cron scheduler|local|app;78|-|local|-")
    assert proc.returncode == 0, proc.stdout[-3000:]
    assert _order(tmp_path) == ["sessions", "prebackup", "restore"]


# ── real Postgres ────────────────────────────────────────────────────────


def _runtime() -> str | None:
    for name in ("docker", "podman"):
        if shutil.which(name) is None:
            continue
        try:
            r = subprocess.run([name, "info"], capture_output=True, timeout=10, check=False)
        except (subprocess.TimeoutExpired, OSError):
            continue
        if r.returncode == 0:
            return name
    return None


_RUNTIME = _runtime()
_IMAGE = os.environ.get("BAY_TEST_PG_IMAGES", "postgres:17-alpine").split(",")[0].strip()


@pytest.fixture
def pg():
    if _RUNTIME is None:
        pytest.skip("no container runtime (docker or podman) on this host")
    name = f"bay-restore-pg-{uuid.uuid4().hex[:10]}"
    run = subprocess.run(
        [_RUNTIME, "run", "-d", "--name", name, "--network", "none",
         "-e", "POSTGRES_USER=app", "-e", "POSTGRES_DB=app",
         "-e", "POSTGRES_HOST_AUTH_METHOD=trust", _IMAGE],
        capture_output=True, text=True, timeout=600, check=False,
    )
    try:
        if run.returncode != 0:
            pytest.skip(f"cannot start {_IMAGE}: {run.stderr.strip()[:200]}")
        deadline = time.time() + 90
        while time.time() < deadline:
            ready = subprocess.run(
                [_RUNTIME, "exec", name, "pg_isready", "-h", "127.0.0.1", "-U", "app"],
                capture_output=True, timeout=10, check=False,
            )
            if ready.returncode == 0:
                break
            time.sleep(0.5)
        else:
            pytest.fail(f"{_IMAGE} did not become ready")
        yield name
    finally:
        subprocess.run([_RUNTIME, "rm", "-f", "-v", name], capture_output=True, timeout=60,
                       check=False)


def _psql(name: str, sql: str) -> str:
    assert _RUNTIME is not None
    r = subprocess.run(
        [_RUNTIME, "exec", "-i", name, "psql", "-X", "-qAt", "-v", "ON_ERROR_STOP=1",
         "-U", "app", "-d", "app"],
        input=sql, capture_output=True, text=True, timeout=60, check=False,
    )
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def _docker_shim(tmp_path: Path) -> str | None:
    """The task calls `docker`; on a podman-only host a shim forwards to podman."""
    if _RUNTIME == "docker":
        return None
    return f"#!/bin/sh\nexec {shutil.which('podman')} \"$@\"\n"


def test_pg_restore_restores_rows_into_real_postgres(tmp_path: Path, pg: str) -> None:
    assert _RUNTIME is not None
    _psql(pg, "CREATE TABLE notes (id integer PRIMARY KEY, body text);"
              "INSERT INTO notes SELECT g, 'row ' || g FROM generate_series(1, 500) g;")
    dump = subprocess.run(
        [_RUNTIME, "exec", pg, "pg_dump", "-U", "app", "app"],
        capture_output=True, text=True, timeout=60, check=True,
    ).stdout
    (tmp_path / "snapshot.sql").write_text(dump)

    # The data is lost: the table is gone.
    _psql(pg, "DROP TABLE notes;")
    proc = _run(tmp_path, accessory=pg, docker=_docker_shim(tmp_path))
    assert proc.returncode == 0, proc.stdout[-3000:] + proc.stderr[-3000:]
    assert _psql(pg, "SELECT count(*) FROM notes;") == "500"

    # A restore over a database that still holds the table stops at the first
    # error and changes nothing (one transaction).
    _psql(pg, "DELETE FROM notes WHERE id > 100;")
    proc = _run(tmp_path, accessory=pg, docker=_docker_shim(tmp_path))
    assert proc.returncode != 0
    assert "already exists" in proc.stdout + proc.stderr
    assert _psql(pg, "SELECT count(*) FROM notes;") == "100"


def test_custom_format_restore_replaces_existing_rows_in_real_postgres(tmp_path: Path, pg: str) -> None:
    """pg_restore --clean --if-exists restores over the live objects (2.24.0)."""
    assert _RUNTIME is not None
    _psql(pg, "CREATE TABLE notes (id integer PRIMARY KEY, body text);"
              "INSERT INTO notes SELECT g, 'row ' || g FROM generate_series(1, 500) g;")
    dump = subprocess.run(
        [_RUNTIME, "exec", pg, "pg_dump", "-Fc", "-Z", "0", "-U", "app", "app"],
        capture_output=True, timeout=60, check=True,
    ).stdout
    (tmp_path / "snapshot.sql").write_bytes(dump)
    _psql(pg, "DELETE FROM notes WHERE id > 100;")
    proc = _run(tmp_path, accessory=pg, docker=_docker_shim(tmp_path), DUMP_FILE=f"{pg}.dump")
    assert proc.returncode == 0, proc.stdout[-3000:] + proc.stderr[-3000:]
    assert _psql(pg, "SELECT count(*) FROM notes;") == "500"
