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
if args[:1] != ["exec"]:
    sys.exit(97)
rest = args[1:]
flags = []
while rest and rest[0].startswith("-"):
    flags.append(rest.pop(0))
container, cmd = rest[0], rest[1:]
got = sys.stdin.read() if "-i" in flags else ""
with open(os.environ["DOCKER_LOG"], "a") as fh:
    fh.write(json.dumps({"flags": flags, "container": container, "cmd": cmd, "stdin": got}) + "\n")
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
    play = yaml.safe_load((ROOT / "restore.yml").read_text())[0]
    return next(
        t for t in _all_tasks(play["tasks"]) if t.get("name") == "Restore PostgreSQL database"
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


def _calls(tmp_path: Path) -> list[dict[str, Any]]:
    log = tmp_path / "docker.log"
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


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
