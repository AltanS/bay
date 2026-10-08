"""The Redis restore writes the RDB bytes to /data/dump.rdb, not the tar around them.

The backup stores the output of `docker cp <accessory>:/data/dump.rdb -` as the
`<accessory>.rdb` snapshot. That output is a tar archive with one entry,
`dump.rdb`. The restore used to copy it as a plain file to /data/dump.rdb, so
Redis found a tar where it expects its RDB file. It now unpacks the archive
with `docker cp - <accessory>:/data`.

The block is lifted out of restore.yml and run in a real ansible-playbook on
localhost. A fake restic serves the snapshot; a fake docker keeps the
container's /data in a temp dir and does what docker does for each `cp` form:
`cp - <c>:<dir>` extracts a tar from stdin into <dir>, `cp <file> <c>:<path>`
copies the file as is.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tarfile
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent

RDB = b"REDIS0011\xfa\tredis-ver\x057.2.4" + bytes(range(256)) * 64 + b"\xff\x00\x01"

FAKE_RESTIC = r"""#!/usr/bin/env python3
import os, sys
args = sys.argv[1:]
if args[:1] != ["dump"] or args[2] != "/" + os.environ["ACCESSORY"] + ".rdb":
    sys.stderr.write("fake restic: unexpected call: %r\n" % (args,))
    sys.exit(3)
if os.environ.get("FAKE_RESTIC_FAIL"):
    sys.stderr.write("Fatal: unable to open config file: Stat: connection refused\n")
    sys.exit(1)
sys.stdout.buffer.write(open(os.environ["SNAPSHOT_FILE"], "rb").read())
"""

FAKE_DOCKER = r"""#!/usr/bin/env python3
import os, shutil, sys, tarfile
args = sys.argv[1:]
root = os.environ["CONTAINER_ROOT"]
with open(os.environ["DOCKER_LOG"], "a") as fh:
    fh.write(" ".join(args) + "\n")
if args[0] in ("stop", "start"):
    sys.exit(0)
if args[0] == "cp":
    src, dest = args[1], args[2]
    path = os.path.join(root, dest.split(":", 1)[1].lstrip("/"))
    if src == "-":
        if not os.path.isdir(path):
            sys.stderr.write("Error: destination must be a directory\n")
            sys.exit(1)
        with tarfile.open(fileobj=sys.stdin.buffer, mode="r|*") as tar:
            tar.extractall(path, filter="data")
    else:
        shutil.copyfile(src, path)
    sys.exit(0)
sys.exit(97)
"""


def _snapshot_tar() -> bytes:
    """What `docker cp redis:/data/dump.rdb -` writes: a tar with one entry."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        info = tarfile.TarInfo("dump.rdb")
        info.size = len(RDB)
        info.uid = info.gid = 999
        tar.addfile(info, io.BytesIO(RDB))
    return buf.getvalue()


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


def _redis_block() -> dict[str, Any]:
    play = yaml.safe_load((ROOT / "restore.yml").read_text())[0]
    return next(t for t in _all_tasks(play["tasks"]) if t.get("name") == "Restore a Redis backup")


def _run(tmp_path: Path, tasks: list[dict[str, Any]], **env_extra: str
         ) -> tuple[subprocess.CompletedProcess[str], Path, list[str]]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, text in (("restic", FAKE_RESTIC), ("docker", FAKE_DOCKER)):
        (bin_dir / name).write_text(text)
        (bin_dir / name).chmod(0o755)
    data = tmp_path / "container" / "data"
    data.mkdir(parents=True)
    (data / "dump.rdb").write_bytes(b"live data that the restore replaces")
    snapshot = tmp_path / "snapshot.rdb"
    snapshot.write_bytes(_snapshot_tar())
    tmp_dir = tmp_path / "restore-tmp"
    tmp_dir.mkdir()
    log = tmp_path / "docker.log"

    play = [{"hosts": "localhost", "connection": "local", "gather_facts": False,
             "tasks": _no_become(tasks)}]
    (tmp_path / "play.yml").write_text(yaml.safe_dump(play))
    (tmp_path / "vars.json").write_text(json.dumps({
        "accessory": "redis",
        "_restore_method": "redis",
        "_snap": {"short_id": "ab12cd34"},
        "_backup_restic_bin": str(bin_dir / "restic"),
        "_restore_tmp_dir": str(tmp_dir),
        "_restic_env": {"RESTIC_REPOSITORY": "r", "RESTIC_PASSWORD": "pw-placeholder"},
    }))
    (tmp_path / "ansible.cfg").write_text("[defaults]\n")
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "ACCESSORY": "redis",
        "SNAPSHOT_FILE": str(snapshot),
        "CONTAINER_ROOT": str(tmp_path / "container"),
        "DOCKER_LOG": str(log),
        "ANSIBLE_CONFIG": str(tmp_path / "ansible.cfg"),
        "ANSIBLE_NOCOLOR": "1",
        "ANSIBLE_LOCALHOST_WARNING": "0",
        "ANSIBLE_INVENTORY_UNPARSED_WARNING": "0",
        "ANSIBLE_PYTHON_INTERPRETER": sys.executable,
        **env_extra,
    }
    proc = subprocess.run(
        [sys.executable, "-m", "ansible.cli.playbook", "-i", "localhost,",
         "-e", f"@{tmp_path / 'vars.json'}", str(tmp_path / "play.yml")],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=300,
    )
    calls = log.read_text().splitlines() if log.exists() else []
    return proc, data / "dump.rdb", calls


def test_redis_restore_writes_the_rdb_bytes(tmp_path: Path) -> None:
    proc, dump, calls = _run(tmp_path, [_redis_block()])
    assert proc.returncode == 0, proc.stdout[-3000:] + proc.stderr[-3000:]
    assert dump.read_bytes() == RDB  # the RDB itself, not the tar around it
    assert not dump.read_bytes().startswith(_snapshot_tar()[:100])
    # Redis is stopped while dump.rdb is replaced, and started after.
    assert calls == ["stop redis", "cp - redis:/data", "start redis"]


def test_redis_restore_starts_redis_again_when_the_dump_fails(tmp_path: Path) -> None:
    proc, dump, calls = _run(tmp_path, [_redis_block()], FAKE_RESTIC_FAIL="1")
    assert proc.returncode != 0
    assert "connection refused" in proc.stdout + proc.stderr
    # Nothing was copied in, and the stopped container runs again.
    assert dump.read_bytes() == b"live data that the restore replaces"
    assert calls == ["stop redis", "start redis"]
