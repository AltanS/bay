"""`bay backup status` (2.24.0): expected targets from the config, state from the box.

Before 2.24.0 the command parsed the ad-hoc output of the whole env. Seen on a
production box right after two good manual runs: the ANSI colour codes and
the `<host> | CHANGED | rc=0 >>` lines became table rows, the target names
kept a colour code (so restic opened a repo that does not exist and counted 0
snapshots), and every target was asked on every box. `bay backup list` showed
the same snapshots correctly.

Now backup-status.yml resolves the expected targets with the backup role's own
resolver, one read-only probe per box reports the state, and the result is a
JSON file, not parsed terminal output. These tests run the real playbook and
probe on localhost (fake systemctl), the pure evaluation, and the CLI.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest
import yaml

from bay_cli import backup_status as bs
from bay_cli.commands import backup

ROOT = Path(__file__).resolve().parent.parent

FAKE_SYSTEMCTL = r"""#!/usr/bin/env python3
import json, os, sys
args = [a for a in sys.argv[1:] if not a.startswith("-")]
timers = json.loads(os.environ.get("FAKE_TIMERS", "{}"))
verb, unit = (args + ["", ""])[:2]
state = timers.get(unit)
if verb == "is-enabled":
    print(state[0] if state else "not-found"); sys.exit(0 if state and state[0] == "enabled" else 1)
if verb == "is-active":
    print(state[1] if state else "inactive"); sys.exit(0 if state and state[1] == "active" else 3)
if verb == "show":
    print("Wed 2026-11-18 03:00:00 UTC" if state else ""); sys.exit(0)
sys.exit(0)
"""

VARS: dict[str, Any] = {
    "backup_enabled": True,
    "stack_name": "bay",
    "secrets": {},
    "access_gateway": "none",
    "services": {"shop": {"image": "example/shop"}},
    "accessories": {
        "postgres": {"image": "postgres:17", "backup": {"method": "pg_dump", "max_age_hours": 30}},
        # Runs in another region: not a target of this box.
        "far-db": {"image": "postgres:17", "regions": ["elsewhere"], "backup": {"method": "pg_dump"}},
    },
    "volume_backups": {"shop-data": {"container": "shop", "path": "/data"}},
}

ENABLED = ["enabled", "active"]


def _run_play(
    tmp_path: Path,
    *,
    scripts: list[str],
    timers: dict[str, list[str]],
    status: dict[str, int] | None = None,
    manifest: list[str] | None = None,
) -> dict[str, Any]:
    if not shutil.which("jq"):
        pytest.skip("jq is not installed")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "systemctl").write_text(FAKE_SYSTEMCTL)
    (bin_dir / "systemctl").chmod(0o755)
    backup_dir = tmp_path / "opt" / "backup"
    (backup_dir / "status").mkdir(parents=True)
    for name in scripts:
        (backup_dir / f"backup-{name}.sh").write_text("#!/bin/sh\n")
    for name, at in (status or {}).items():
        (backup_dir / "status" / f"{name}.json").write_text(json.dumps({"at": at, "bytes": 1}))
    if manifest is not None:
        (backup_dir / "targets.json").write_text(json.dumps([{"name": n} for n in manifest]))
    (tmp_path / "hosts").write_text("[testing]\nlocalhost ansible_connection=local\n")
    (tmp_path / "vars.json").write_text(json.dumps({
        **VARS,
        "stack_dir": str(tmp_path / "opt"),
        "backup_scripts_dir": str(backup_dir),
        "ansible_become": False,
        "backup_status_out": str(tmp_path / "out.json"),
    }))
    (tmp_path / "ansible.cfg").write_text("[defaults]\n")
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "FAKE_TIMERS": json.dumps({f"bay-backup@{k}.timer": v for k, v in timers.items()}),
        "ANSIBLE_CONFIG": str(tmp_path / "ansible.cfg"),
        "ANSIBLE_ROLES_PATH": str(ROOT / "roles"),
        "ANSIBLE_NOCOLOR": "1",
        "ANSIBLE_LOCALHOST_WARNING": "0",
        "ANSIBLE_PYTHON_INTERPRETER": sys.executable,
    }
    proc = subprocess.run(
        [sys.executable, "-m", "ansible.cli.playbook", "-i", str(tmp_path / "hosts"),
         "-e", "target_host=testing", "-e", f"@{tmp_path / 'vars.json'}",
         str(ROOT / "backup-status.yml")],
        capture_output=True, text=True, env=env, cwd=tmp_path,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return json.loads((tmp_path / "out.json").read_text())


def test_the_playbook_expects_the_config_targets_and_reports_a_missing_one(tmp_path):
    """The eu2 case: targets in the config, nothing installed on the box."""
    doc = _run_play(tmp_path, scripts=[], timers={})
    res = doc["results"]["localhost"]
    assert sorted(res["expected"]) == ["bay_shop-data", "postgres"]
    rows, code = bs.evaluate(doc)
    assert code == bs.MISSING
    assert {r.target: r.state for r in rows} == {"postgres": "missing", "bay_shop-data": "missing"}
    assert "no script" in rows[0].detail and "timer not enabled" in rows[0].detail


def test_installed_and_fresh_targets_pass_and_names_are_clean(tmp_path):
    now = int(time.time())
    doc = _run_play(
        tmp_path,
        scripts=["postgres", "bay_shop-data", "old-thing"],
        timers={"postgres": ENABLED, "bay_shop-data": ENABLED},
        status={"postgres": now - 3600, "bay_shop-data": now - 7200},
        manifest=["postgres", "bay_shop-data"],
    )
    rows, code = bs.evaluate(doc)
    assert code == bs.OK, rows
    states = {r.target: r.state for r in rows}
    # A script on the box that the config does not name is shown, not judged.
    assert states == {"postgres": "ok", "bay_shop-data": "ok", "old-thing": "extra"}
    # The regression: no colour code and no Ansible host line in any name.
    for r in rows:
        assert "\x1b" not in r.target and "|" not in r.target
    assert rows[0].next_run == "Wed 2026-11-18 03:00:00 UTC"


def test_a_target_with_an_old_last_run_is_stale(tmp_path):
    now = int(time.time())
    doc = _run_play(
        tmp_path,
        scripts=["postgres", "bay_shop-data"],
        timers={"postgres": ENABLED, "bay_shop-data": ENABLED},
        status={"postgres": now - 31 * 3600, "bay_shop-data": now - 60},
        manifest=["postgres", "bay_shop-data"],
    )
    rows, code = bs.evaluate(doc)
    assert code == bs.STALE
    assert {r.target: r.state for r in rows}["postgres"] == "stale"


# ── pure evaluation ─────────────────────────────────────────────────────────

NOW = 1_800_000_000


def _doc(targets: dict[str, dict[str, Any]], *, manifest=True, hosts=("box",), deep=None):
    seen = {}
    for name, t in targets.items():
        seen[name] = {
            "script": t.get("script", True),
            "timer_enabled": t.get("enabled", "enabled"),
            "timer_active": t.get("active", "active"),
            "next": "",
            "installed": t.get("installed", NOW - 100 * 3600),
            "last_ok": {"at": t["last"]} if "last" in t else None,
            "deep": (deep or {}).get(name),
        }
    return {
        "hosts": list(hosts),
        "results": {"box": {
            "enabled": True,
            "expected": list(targets),
            "targets": {n: {"method": "pg_dump", "max_age_hours": 30} for n in targets},
            "probe": {"now": NOW, "manifest": list(targets) if manifest else None,
                      "manifest_mtime": NOW - 100 * 3600 if manifest else 0,
                      "targets": seen},
        }},
    }


def test_missing_wins_over_stale_and_no_answer():
    doc = _doc({"a": {"script": False}, "b": {"last": NOW - 40 * 3600}}, hosts=("box", "gone"))
    rows, code = bs.evaluate(doc)
    assert code == bs.MISSING
    assert {r.host: r.state for r in rows if r.host == "gone"} == {"gone": "no_answer"}


def test_stale_wins_over_no_answer():
    _, code = bs.evaluate(_doc({"b": {"last": NOW - 40 * 3600}}, hosts=("box", "gone")))
    assert code == bs.STALE


def test_a_box_that_did_not_answer_gives_3():
    _, code = bs.evaluate(_doc({"b": {"last": NOW - 60}}, hosts=("box", "gone")))
    assert code == bs.NO_ANSWER


def test_a_new_target_is_not_stale_before_max_age():
    rows, code = bs.evaluate(_doc({"a": {"installed": NOW - 3600}}))
    assert code == bs.OK
    assert rows[0].state == "new"


def test_a_target_that_never_ran_after_max_age_is_stale():
    rows, code = bs.evaluate(_doc({"a": {}}))
    assert code == bs.STALE and rows[0].state == "stale"


def test_a_box_from_before_2_24_has_no_record_and_is_not_stale():
    """No manifest and no status file: the age is unknown, not stale."""
    rows, code = bs.evaluate(_doc({"a": {}}, manifest=False))
    assert code == bs.OK
    assert rows[0].state == "no_record"


def test_deep_snapshot_time_counts_as_the_last_good_run():
    doc = _doc({"a": {}}, manifest=False,
               deep={"a": {"snapshots": 2, "latest": "2027-01-15T10:00:00.123456789+02:00"}})
    doc["results"]["box"]["probe"]["now"] = 1_800_003_600
    rows, code = bs.evaluate(doc)
    assert rows[0].snapshots == 2
    assert rows[0].last_ok == NOW and code == bs.OK
    assert bs.when(rows[0].last_ok) == "2027-01-15 08:00 UTC"


# ── CLI ─────────────────────────────────────────────────────────────────────


def _cli(monkeypatch, tmp_path, doc, *args):
    from typer.testing import CliRunner

    from bay_cli.context import Context

    cx = Context(fleet_root=tmp_path, framework_root=Path("/x/bay"), source="flag")
    seen: dict[str, Any] = {}

    def fake_run_playbook(playbook, env, **kw):
        seen.update(kw, playbook=playbook, env=env)
        extra = json.loads(kw["extra_args"][1])
        seen["extra"] = extra
        Path(extra["backup_status_out"]).write_text(json.dumps(doc))
        return subprocess.CompletedProcess([], 0, "", "")

    def boom(*a, **k):
        raise AssertionError("status must not parse ad-hoc output any more")

    monkeypatch.setattr(backup.ansible, "run_playbook", fake_run_playbook)
    monkeypatch.setattr(backup, "_run_on_host", boom)
    out = CliRunner().invoke(backup.app, ["status", *args], obj=cx)
    return out, seen


def test_cli_status_exit_code_and_json(monkeypatch, tmp_path):
    doc = _doc({"postgres": {"script": False}})
    out, seen = _cli(monkeypatch, tmp_path, doc, "--env", "production", "--json")
    assert out.exit_code == bs.MISSING, out.output
    assert seen["playbook"] == "backup-status" and seen["fleet_root"] == tmp_path
    assert seen["capture"] is True and seen["extra"]["backup_status_deep"] is False
    body = json.loads(out.stdout)
    assert body["exit_code"] == 1
    assert body["rows"][0]["state"] == "missing"


def test_cli_status_deep_passes_the_flag(monkeypatch, tmp_path):
    out, seen = _cli(monkeypatch, tmp_path, _doc({"a": {"last": NOW - 60}}), "--deep", "--json")
    assert seen["extra"]["backup_status_deep"] is True
    assert out.exit_code == 0


def test_cli_status_with_no_result_file_says_no_box_answered(monkeypatch, tmp_path):
    from typer.testing import CliRunner

    from bay_cli.context import Context

    cx = Context(fleet_root=tmp_path, framework_root=Path("/x/bay"), source="flag")
    monkeypatch.setattr(
        backup.ansible, "run_playbook",
        lambda *a, **k: subprocess.CompletedProcess([], 4, "UNREACHABLE! box\n", ""),
    )
    out = CliRunner().invoke(backup.app, ["status", "--json"], obj=cx)
    assert out.exit_code == bs.NO_ANSWER


def test_status_play_keeps_the_access_gateway_headscale_server_rule():
    """The status play restates headscale_server (it does not load the
    access_gateway defaults). A drift would expect Headscale on every box."""
    play = yaml.safe_load((ROOT / "backup-status.yml").read_text())[0]
    ours = " ".join(play["vars"]["headscale_server"].split())
    theirs = yaml.safe_load((ROOT / "roles/access_gateway/defaults/main.yml").read_text())
    assert ours == " ".join(theirs["headscale_server"].split())


# ── bay backup list: a configured target that no box has ────────────────────


def _list(monkeypatch, tmp_path, target):
    from typer.testing import CliRunner

    from bay_cli.context import Context

    gv = tmp_path / "group_vars" / "all"
    gv.mkdir(parents=True)
    (gv / "main.yml").write_text("---\nstack_name: shop\n")
    (gv / "services.yml").write_text(yaml.safe_dump({
        "accessories": {"postgres": {"image": "postgres:17", "backup": {"method": "pg_dump"}},
                        "cache": {"image": "redis:7"}},
        "volume_backups": {"data": {"container": "web", "path": "/data"}},
    }))
    cx = Context(fleet_root=tmp_path, framework_root=Path("/x/bay"), source="flag")
    monkeypatch.setattr(backup, "_hosts_with_target", lambda *a, **k: [])
    return CliRunner().invoke(backup.app, ["list", target], obj=cx)


@pytest.mark.parametrize("target", ["postgres", "shop_data"])
def test_list_of_a_configured_target_on_no_box_exits_1(monkeypatch, tmp_path, target):
    out = _list(monkeypatch, tmp_path, target)
    assert out.exit_code == 1, out.output
    assert "nothing backs it up" in " ".join(out.output.split())


def test_list_of_an_unknown_target_stays_exit_0(monkeypatch, tmp_path):
    assert _list(monkeypatch, tmp_path, "cache").exit_code == 0
