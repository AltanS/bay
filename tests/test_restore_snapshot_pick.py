"""restore.yml picks the snapshot of the right file (2.24.0).

A pg_dump target with several databases stores one snapshot per database per
run. Before 2.24.0 `latest` took the newest snapshot of the repo, which could
hold another database. Now: by id, else the newest; `database` keeps only the
files of that database; several files and no `database` is refused. The pick
tasks run with restore.yml's own play vars in a real ansible-playbook.
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
PICK = ("Parse snapshot details", "Fail when no snapshot matches",
        "Refuse a snapshot choice that names no single database",
        "Warn that the database of an old snapshot cannot be checked",
        "Refuse a database that the snapshot does not hold")


def _snap(sid: str, path: str, time: str, tags: list[str] | None = None) -> dict[str, Any]:
    out = {"short_id": sid, "id": sid + "0" * 56, "paths": [path], "time": time}
    if tags is not None:
        out["tags"] = tags
    return out


SNAPS = [
    _snap("aaaa0001", "/postgres-shop.dump", "2026-11-16T03:00:00Z"),
    _snap("bbbb0002", "/postgres-blog.dump", "2026-11-16T03:01:00Z"),
    _snap("cccc0003", "/postgres-shop.dump", "2026-11-17T03:00:00Z"),
    _snap("dddd0004", "/postgres-blog.dump", "2026-11-17T03:01:00Z"),
]


def _run(tmp_path: Path, snaps: list[dict[str, Any]], **extra: Any):
    play_src = yaml.safe_load((ROOT / "restore.yml").read_text())[0]
    block = next(t for t in play_src["tasks"] if "block" in t)["block"]
    tasks = [t for t in block if t.get("name") in PICK]
    assert [t["name"] for t in tasks] == list(PICK)
    tasks.append({"name": "Show", "ansible.builtin.debug": {
        "msg": "PICK {{ {'id': _snap.short_id, 'file': _pg_file | trim, "
               "'custom': _pg_custom | bool, 'db': _pg_database | trim} | to_json }}"}})
    play = [{
        "hosts": "localhost", "connection": "local", "gather_facts": False,
        "vars": play_src["vars"], "tasks": tasks,
    }]
    (tmp_path / "play.yml").write_text(yaml.safe_dump(play))
    (tmp_path / "vars.json").write_text(json.dumps({
        "accessory": "postgres",
        "stack_name": "bay",
        "accessories": {"postgres": {"image": "postgres:17", "backup": {"method": "pg_dump"},
                                     "env": {"clear": {"POSTGRES_DB": "shop"}}}},
        "_snapshot_info": {"stdout": json.dumps(snaps), "rc": 0},
        **extra,
    }))
    (tmp_path / "ansible.cfg").write_text("[defaults]\n")
    env = {**os.environ, "ANSIBLE_CONFIG": str(tmp_path / "ansible.cfg"),
           "ANSIBLE_NOCOLOR": "1", "ANSIBLE_LOCALHOST_WARNING": "0",
           "ANSIBLE_INVENTORY_UNPARSED_WARNING": "0",
           "ANSIBLE_PYTHON_INTERPRETER": sys.executable}
    proc = subprocess.run(
        [sys.executable, "-m", "ansible.cli.playbook", "-i", "localhost,",
         "-e", f"@{tmp_path / 'vars.json'}", str(tmp_path / "play.yml")],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=300,
    )
    picked = None
    for line in proc.stdout.splitlines():
        if '"msg": "PICK ' in line:
            picked = json.loads(json.loads("{" + line.strip().rstrip(",") + "}")["msg"][5:])
    return proc, picked


def test_latest_with_several_databases_and_no_database_is_refused(tmp_path):
    proc, picked = _run(tmp_path, SNAPS)
    assert proc.returncode != 0 and picked is None
    assert "holds several databases (blog, shop)" in proc.stdout


def test_latest_of_a_named_database(tmp_path):
    proc, picked = _run(tmp_path, SNAPS, database="blog")
    assert proc.returncode == 0, proc.stdout[-2000:]
    assert picked == {"id": "dddd0004", "file": "/postgres-blog.dump", "custom": True, "db": "blog"}


def test_a_snapshot_id_names_its_file_and_database(tmp_path):
    proc, picked = _run(tmp_path, SNAPS, snapshot="aaaa0001")
    assert proc.returncode == 0, proc.stdout[-2000:]
    assert picked == {"id": "aaaa0001", "file": "/postgres-shop.dump", "custom": True, "db": "shop"}


def test_an_unknown_snapshot_id_fails(tmp_path):
    proc, picked = _run(tmp_path, SNAPS, snapshot="99999999")
    assert proc.returncode != 0 and picked is None


# ── review fixes: the database a snapshot holds ────────────────────────────

# One database, `shop`, on a server whose POSTGRES_DB is `main`.
MAIN = {"postgres": {"image": "postgres:17",
                     "backup": {"method": "pg_dump", "databases": ["shop"]},
                     "env": {"clear": {"POSTGRES_DB": "main"}}}}


def test_the_database_tag_decides(tmp_path):
    snaps = [_snap("abab0001", "/postgres.dump", "2026-11-17T03:00:00Z",
                   tags=["postgres", "pg_dump", "db:blog"])]
    proc, picked = _run(tmp_path, snaps)
    assert proc.returncode == 0, proc.stdout[-2000:]
    assert picked["db"] == "blog"


def test_a_database_that_the_snapshot_does_not_hold_is_refused(tmp_path):
    proc, picked = _run(tmp_path, SNAPS, snapshot="aaaa0001", database="blog")
    assert proc.returncode != 0 and picked is None
    assert "holds the database shop, not blog" in proc.stdout


def test_a_plain_to_custom_switch_takes_the_newest_snapshot(tmp_path):
    """Same database, two file names (`.sql` before the switch, `.dump`
    after): not "several databases", the newest wins."""
    snaps = [_snap("1111aaaa", "/postgres.sql", "2026-11-16T03:00:00Z"),
             _snap("2222bbbb", "/postgres-shop.dump", "2026-11-17T03:00:00Z")]
    proc, picked = _run(tmp_path, snaps)
    assert proc.returncode == 0, proc.stdout[-2000:]
    assert picked == {"id": "2222bbbb", "file": "/postgres-shop.dump", "custom": True, "db": "shop"}


# ── second review: a snapshot with no database recorded ─────────────────────
# A snapshot from before 2.24.0 with no `db:` tag and no `-<db>` in its file
# name holds an UNKNOWN database: today's config may name another one than the
# run that made it. It is never picked as `latest`; only an explicit
# --snapshot with an explicit --database restores it, with a warning.

LEGACY = [_snap("eeee0005", "/postgres.sql", "2026-11-16T03:00:00Z")]


def test_latest_never_picks_a_snapshot_with_no_database(tmp_path):
    proc, picked = _run(tmp_path, LEGACY)
    assert proc.returncode != 0 and picked is None
    assert "--snapshot <id> and --database <name>" in proc.stdout


def test_database_alone_never_matches_a_snapshot_with_no_database(tmp_path):
    proc, picked = _run(tmp_path, LEGACY, accessories=MAIN, database="shop")
    assert proc.returncode != 0 and picked is None


def test_an_old_snapshot_after_a_config_change_needs_the_database(tmp_path):
    """The config named app when the snapshot was made and shop today."""
    proc, picked = _run(tmp_path, LEGACY, accessories=MAIN, snapshot="eeee0005")
    assert proc.returncode != 0 and picked is None
    assert "records no database" in proc.stdout


def test_snapshot_and_database_restore_an_old_snapshot_with_a_warning(tmp_path):
    proc, picked = _run(tmp_path, LEGACY, accessories=MAIN, snapshot="eeee0005", database="app")
    assert proc.returncode == 0, proc.stdout[-2000:]
    assert picked == {"id": "eeee0005", "file": "/postgres.sql", "custom": False, "db": "app"}
    assert "cannot verify" in proc.stdout


def test_latest_skips_old_snapshots_for_the_newest_recorded_one(tmp_path):
    snaps = [_snap("1111aaaa", "/postgres-shop.dump", "2026-11-15T03:00:00Z"),
             _snap("2222bbbb", "/postgres.sql", "2026-11-17T03:00:00Z")]
    proc, picked = _run(tmp_path, snaps)
    assert proc.returncode == 0, proc.stdout[-2000:]
    assert picked["id"] == "1111aaaa" and picked["db"] == "shop"

