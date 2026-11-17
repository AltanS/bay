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


def test_restore_test_runs_scratch_without_keep_and_reports(cx, played):
    played["report"] = {"snapshot": "ab12cd34", "tables": 3, "rows": 120, "seconds": 9,
                        "counts": {"public.notes": 100, "public.tags": 20, "public.x": 0},
                        "live_estimates": {"public.notes": 98, "public.x": 5},
                        "empty_but_live_has_rows": ["public.x"]}
    out = CliRunner().invoke(backup.app, ["restore-test", "production", "shop-postgres"], obj=cx)
    assert out.exit_code == 0, out.output
    v = played["vars"]
    assert v["restore_into"] == "scratch" and v["restore_keep"] is False
    assert "confirm" not in v and v["snapshot"] == "latest"
    # Its own alert on failure (backup.restore_test_failed), not restore.failed.
    assert v["restore_test"] is True
    assert "public.x" in out.output


def test_restore_test_fails_without_a_report(cx, played):
    played["rc"] = 2
    out = CliRunner().invoke(backup.app, ["restore-test", "production", "shop-postgres", "--json"],
                             obj=cx)
    assert out.exit_code == 1
    assert json.loads(out.stdout) == {"ok": False, "report": None}


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

