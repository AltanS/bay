"""`bay backup restore --into scratch|live` and `bay backup restore-test` (2.24.0).

A pg_dump target restores into a scratch container by default. A live restore
stops the containers that use the database, and only with --yes-stop-clients;
the refusal and the prompt name every container it will stop, and the play
gets exactly that list.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from bay_cli.commands import backup
from bay_cli.context import Context
from bay_cli.errors import BayError

SERVICES = {
    "services": {
        "shop": {"image": "shop:1"},
        "blog": {"image": "blog:1", "database": {"accessory": "postgres", "name": "blog"}},
    },
    "accessories": {
        "shop-postgres": {"image": "postgres:17", "backup": {
            "method": "pg_dump", "format": "custom", "clients": ["shop", "shop-worker"]}},
        "postgres": {"image": "postgres:17", "backup": {"method": "pg_dump"}},
        "cache": {"image": "redis:7", "backup": {"method": "redis"}},
        "solo-postgres": {"image": "postgres:17", "backup": {"method": "pg_dump"}},
    },
    "volume_backups": {"data": {"container": "shop", "path": "/data"}},
}


@pytest.fixture
def cx(tmp_path: Path) -> Context:
    gv = tmp_path / "group_vars" / "all"
    gv.mkdir(parents=True)
    (gv / "main.yml").write_text("---\nstack_name: bay\n")
    (gv / "services.yml").write_text(yaml.safe_dump(SERVICES))
    return Context(fleet_root=tmp_path, framework_root=Path("/x/bay"), source="flag")


@pytest.fixture
def played(monkeypatch) -> dict[str, Any]:
    seen: dict[str, Any] = {}
    monkeypatch.setattr(backup, "_hosts_with_target", lambda *a, **k: ["box"])

    def fake_run_playbook(playbook, env, **kw):
        seen.update(kw, playbook=playbook)
        seen["vars"] = json.loads(kw["extra_args"][1])
        out = seen["vars"].get("restore_test_out")
        if out and seen.get("report") is not None:
            Path(out).write_text(json.dumps(seen["report"]))
        return subprocess.CompletedProcess([], seen.get("rc", 0), "", "")

    monkeypatch.setattr(backup.ansible, "run_playbook", fake_run_playbook)
    return seen


def _restore(cx, *args, input="y\n"):
    return CliRunner().invoke(backup.app, ["restore", "production", *args], input=input, obj=cx)


def test_target_config_reads_method_and_clients(cx):
    # The other containers of a project: clients of every database of it ("").
    assert backup._target_config(cx, "shop-postgres", "bay") == {
        "method": "pg_dump", "databases": ["app"],
        "clients": [{"name": "shop", "database": ""}, {"name": "shop-worker", "database": ""}]}
    # A shared resource: the services bound to it by a database block, each
    # with the database it is bound to.
    assert backup._target_config(cx, "postgres", "bay") == {
        "method": "pg_dump", "databases": ["app", "blog"],
        "clients": [{"name": "blog", "database": "blog"}]}
    assert backup._target_config(cx, "bay_data", "bay")["method"] == "file"
    assert backup._target_config(cx, "cache", "bay")["method"] == "redis"


def test_a_pg_dump_target_restores_into_scratch_by_default(cx, played):
    out = _restore(cx, "shop-postgres", "--snapshot", "ab12cd34")
    assert out.exit_code == 0, out.output
    v = played["vars"]
    assert v["restore_into"] == "scratch" and v["restore_keep"] is True
    assert v["restore_stop_clients"] == []


def test_a_live_restore_without_the_flag_names_every_client_and_runs_nothing(cx, played):
    out = _restore(cx, "shop-postgres", "--into", "live", "--snapshot", "ab12cd34")
    assert out.exit_code != 0
    assert isinstance(out.exception, BayError)
    msg = str(out.exception)
    assert "shop, shop-worker" in msg and "--yes-stop-clients" in msg
    assert "vars" not in played


def test_a_live_restore_with_the_flag_stops_exactly_the_named_clients(cx, played):
    out = _restore(cx, "shop-postgres", "--into", "live", "--yes-stop-clients",
                   "--snapshot", "ab12cd34", "--database", "shop")
    assert out.exit_code == 0, out.output
    assert "Stop shop, shop-worker and restore into the live database?" in out.output
    v = played["vars"]
    assert v["restore_into"] == "live"
    assert v["restore_stop_clients"] == [{"name": "shop", "database": ""},
                                         {"name": "shop-worker", "database": ""}]
    assert v["database"] == "shop"
    assert v["restore_terminate_connections"] is False


def test_declining_the_prompt_runs_nothing(cx, played):
    out = _restore(cx, "shop-postgres", "--into", "live", "--yes-stop-clients",
                   "--snapshot", "ab12cd34", input="n\n")
    assert out.exit_code == 0 and "vars" not in played


def test_other_methods_restore_live_and_refuse_scratch(cx, played):
    out = _restore(cx, "cache", "--snapshot", "ab12cd34")
    assert out.exit_code == 0, out.output
    assert played["vars"]["restore_into"] == "live"
    played.clear()
    out = _restore(cx, "bay_data", "--into", "scratch", "--snapshot", "ab12cd34")
    assert isinstance(out.exception, BayError) and "vars" not in played


def _report(restored: list[str], live: list[str]) -> dict[str, Any]:
    """A report as the playbook builds it: the same filter, real input lines."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "bay_filters_for_restore_into", Path(backup.__file__).resolve().parents[3] / "filter_plugins" / "bay_filters.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return {"snapshot": "ab12cd34", "snapshot_time": "2026-11-17T03:00:00Z", "seconds": 9,
            **module.bay_restore_report(restored, live, 1.0, 1000, 1_800_000_000, 1_800_000_004)}


def test_restore_test_runs_scratch_without_keep_and_reports(cx, played):
    played["report"] = _report(
        ["T\tpublic.notes\t100", "T\tpublic.tags\t20", "T\tpublic.x\t0"],
        ["L\tpublic.notes", "L\tpublic.tags", "L\tpublic.x", "T\tpublic.notes\t100",
         "T\tpublic.tags\t20", "T\tpublic.x\t5"])
    out = CliRunner().invoke(backup.app, ["restore-test", "production", "shop-postgres"], obj=cx)
    # public.x is empty in the restore and live has 5 rows: the test fails.
    assert out.exit_code == 1, out.output
    v = played["vars"]
    assert v["restore_into"] == "scratch" and v["restore_keep"] is False
    assert "confirm" not in v and v["snapshot"] == "latest"
    # Its own alert on failure (backup.restore_test_failed), not restore.failed.
    assert v["restore_test"] is True
    assert "public.x" in out.output and "fail" in out.output
    assert "public.notes" not in out.output.replace("Failed:", ""), "an ok table is not listed"
    assert "2027-01-15T08:00:00Z" in out.output and "2026-11-17T03:00:00" in out.output


def test_restore_test_passes_when_every_table_is_ok(cx, played):
    played["report"] = _report(
        ["T\tpublic.notes\t100", "T\tpublic.events_a\t4", "P\tpublic.events\tpublic.events_a"],
        ["L\tpublic.notes", "L\tpublic.events_a", "T\tpublic.notes\t120", "T\tpublic.events_a\t4"])
    out = CliRunner().invoke(backup.app, ["restore-test", "production", "shop-postgres"], obj=cx)
    assert out.exit_code == 0, out.output
    assert "partitioned parent public.events" in out.output
    assert "Restore test passed" in out.output


def test_restore_test_passes_the_limits_only_when_given(cx, played):
    played["report"] = _report(["T\tpublic.notes\t1"], ["L\tpublic.notes", "T\tpublic.notes\t1"])
    out = CliRunner().invoke(backup.app, ["restore-test", "production", "shop-postgres"], obj=cx)
    assert out.exit_code == 0, out.output
    assert not any(k.startswith("restore_test_max") or k == "restore_test_count_timeout"
                   for k in played["vars"]), "else the fleet variables are overridden by the defaults"
    played.clear()
    played["report"] = _report(["T\tpublic.notes\t1"], ["L\tpublic.notes", "T\tpublic.notes\t1"])
    out = CliRunner().invoke(backup.app, ["restore-test", "production", "shop-postgres",
                                          "--max-loss-pct", "0.5", "--max-loss-rows", "100",
                                          "--count-timeout", "30"], obj=cx)
    assert out.exit_code == 0, out.output
    v = played["vars"]
    assert (v["restore_test_max_loss_pct"], v["restore_test_max_loss_rows"],
            v["restore_test_count_timeout"]) == (0.5, 100, 30)


def test_restore_test_rejects_a_nonsense_limit(cx, played):
    for bad in (["--max-loss-pct", "101"], ["--max-loss-rows", "-1"], ["--count-timeout", "0"]):
        out = CliRunner().invoke(backup.app, ["restore-test", "production", "shop-postgres", *bad], obj=cx)
        assert out.exit_code != 0 and "vars" not in played


def test_restore_test_json_carries_the_failures(cx, played):
    played["report"] = _report(["T\tpublic.notes\t0"], ["L\tpublic.notes", "T\tpublic.notes\t9"])
    out = CliRunner().invoke(backup.app, ["restore-test", "production", "shop-postgres", "--json"], obj=cx)
    assert out.exit_code == 1
    doc = json.loads(out.stdout)
    assert doc["ok"] is False and doc["report"]["failures"] == ["public.notes"]


def _unverified() -> dict[str, Any]:
    return _report(["T\tpublic.notes\t3"], [])


def test_an_unverified_restore_test_exits_3_and_warns(cx, played):
    """2.24.1: nothing was compared, so it must not look like a pass."""
    played["report"] = _unverified()
    out = CliRunner().invoke(backup.app, ["restore-test", "production", "shop-postgres"], obj=cx)
    assert out.exit_code == backup.RESTORE_TEST_UNVERIFIED == 3, out.output
    flat = " ".join(out.output.split())
    assert "UNVERIFIED" in flat and "nothing was compared" in flat
    assert "Restore test passed" not in flat


def test_an_unverified_restore_test_in_json_is_not_ok_and_says_why(cx, played):
    played["report"] = _unverified()
    out = CliRunner().invoke(backup.app, ["restore-test", "production", "shop-postgres", "--json"], obj=cx)
    assert out.exit_code == 3
    doc = json.loads(out.stdout)
    assert doc["ok"] is False and doc["verdict"] == "unverified" and doc["exit_code"] == 3


def test_restore_test_exit_codes_are_0_1_3(cx, played):
    played["report"] = _report(["T\tpublic.notes\t1"], ["L\tpublic.notes", "T\tpublic.notes\t1"])
    assert CliRunner().invoke(backup.app, ["restore-test", "production", "shop-postgres"], obj=cx).exit_code == 0
    played["report"] = _report(["T\tpublic.notes\t0"], ["L\tpublic.notes", "T\tpublic.notes\t9"])
    assert CliRunner().invoke(backup.app, ["restore-test", "production", "shop-postgres"], obj=cx).exit_code == 1
    played["rc"] = 2
    played["report"] = None
    assert CliRunner().invoke(backup.app, ["restore-test", "production", "shop-postgres"], obj=cx).exit_code == 1


def test_a_failed_play_is_never_reported_as_unverified(cx, played):
    played["rc"] = 2
    played["report"] = _unverified()
    out = CliRunner().invoke(backup.app, ["restore-test", "production", "shop-postgres"], obj=cx)
    assert out.exit_code == 1


def test_restore_test_fails_without_a_report(cx, played):
    played["rc"] = 2
    out = CliRunner().invoke(backup.app, ["restore-test", "production", "shop-postgres", "--json"],
                             obj=cx)
    assert out.exit_code == 1
    assert json.loads(out.stdout) == {"ok": False, "verdict": None, "exit_code": 1, "report": None}


def test_restore_test_refuses_a_target_that_is_not_pg_dump(cx, played):
    out = CliRunner().invoke(backup.app, ["restore-test", "production", "cache"], obj=cx)
    assert isinstance(out.exception, BayError) and "vars" not in played


# ── review fixes: live restore scope ────────────────────────────────────────


def test_a_live_restore_needs_the_flag_also_with_no_clients(cx, played):
    out = _restore(cx, "solo-postgres", "--into", "live", "--snapshot", "ab12cd34")
    assert isinstance(out.exception, BayError), out.output
    assert "--yes-stop-clients" in str(out.exception)
    assert "vars" not in played


def test_a_live_restore_stops_only_the_clients_of_the_restored_database(cx, played):
    out = _restore(cx, "postgres", "--into", "live", "--yes-stop-clients",
                   "--snapshot", "ab12cd34", "--database", "app")
    assert out.exit_code == 0, out.output
    # blog is bound to the database blog: restoring app does not stop it.
    assert played["vars"]["restore_stop_clients"] == []
    assert "blog" not in out.output
    played.clear()
    out = _restore(cx, "postgres", "--into", "live", "--yes-stop-clients",
                   "--snapshot", "ab12cd34", "--database", "blog")
    assert out.exit_code == 0, out.output
    assert played["vars"]["restore_stop_clients"] == [{"name": "blog", "database": "blog"}]


def test_terminate_connections_is_passed_only_when_asked(cx, played):
    out = _restore(cx, "solo-postgres", "--into", "live", "--yes-stop-clients",
                   "--terminate-connections", "--snapshot", "ab12cd34")
    assert out.exit_code == 0, out.output
    assert played["vars"]["restore_terminate_connections"] is True


def test_a_live_restore_says_what_it_does_not_undo_before_the_prompt(cx, played):
    """Second review: pg_restore --clean drops and makes the objects of the
    dump only; a table made after the snapshot stays, and a migrations table
    goes back to the snapshot."""
    out = _restore(cx, "shop-postgres", "--into", "live", "--yes-stop-clients",
                   "--snapshot", "ab12cd34", input="n\n")
    assert out.exit_code == 0
    text = " ".join(out.output.split())
    warn = text.index("made after the snapshot stay")
    assert "migrations" in text
    assert warn < text.index("restore into the live database?")

