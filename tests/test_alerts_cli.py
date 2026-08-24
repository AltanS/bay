"""`bay alerts` — the operator surface over the alert registry."""

from __future__ import annotations

import json
import time

import pytest
import typer

from bay_cli.commands import alerts
from bay_cli.errors import BayError


@pytest.fixture
def consumer(tmp_path, monkeypatch):
    (tmp_path / "group_vars" / "all").mkdir(parents=True)
    monkeypatch.setattr(alerts.paths, "consumer_root", lambda *a, **k: tmp_path)
    return tmp_path


def _write(consumer, data):
    alerts._save_config(consumer, data)


def _webhook(name, url, min_level="info"):
    return {"name": name, "adapter": "webhook", "min_level": min_level,
            "config": {"url": url}}


# ── list ─────────────────────────────────────────────────────────────────


def test_list_json_covers_every_registry_entry(consumer, capsys):
    alerts.list_alerts(recipient=None, level=None, as_json=True)
    payload = json.loads(capsys.readouterr().out)
    assert len(payload["alerts"]) == len(alerts._load_registry())


def test_list_shows_effective_state_not_configured_state(consumer, capsys):
    """A critical-only recipient must not be listed against an info alert."""
    _write(consumer, {"alert_recipients": [_webhook("oncall", "https://p.invalid", "critical")]})
    alerts.list_alerts(recipient=None, level=None, as_json=True)
    rows = {r["id"]: r for r in json.loads(capsys.readouterr().out)["alerts"]}
    assert rows["deploy.failed"]["recipients"] == ["oncall"]
    assert rows["deploy.complete"]["recipients"] == []


def test_list_marks_muted_alerts(consumer, capsys):
    _write(consumer, {
        "alert_recipients": [_webhook("chat", "https://c.invalid", "debug")],
        "alerts_disabled": ["deploy.complete"]})
    alerts.list_alerts(recipient=None, level=None, as_json=True)
    rows = {r["id"]: r for r in json.loads(capsys.readouterr().out)["alerts"]}
    assert rows["deploy.complete"]["state"] == "muted"
    assert rows["deploy.complete"]["recipients"] == []


def test_list_shows_alerts_test_still_delivered_by_default(consumer, capsys):
    """alerts.test is the one info alert that stays enabled_by_default: true —
    `bin/bay alerts test` (and its dry run) depend on that. A sibling info
    alert (deploy.complete) must show the opt-in "default off" state instead."""
    _write(consumer, {"alert_recipients": [_webhook("chat", "https://c.invalid", "info")]})
    alerts.list_alerts(recipient=None, level=None, as_json=True)
    rows = {r["id"]: r for r in json.loads(capsys.readouterr().out)["alerts"]}
    assert rows["alerts.test"]["recipients"] == ["chat"]
    assert rows["alerts.test"]["state"] == "delivered"
    assert rows["deploy.complete"]["recipients"] == []
    assert rows["deploy.complete"]["state"] == "default off"


def test_list_rejects_an_unknown_level(consumer):
    with pytest.raises(BayError, match="Unknown level"):
        alerts.list_alerts(recipient=None, level="emergency", as_json=True)


def test_list_rejects_an_unknown_recipient(consumer):
    with pytest.raises(BayError, match="No recipient named"):
        alerts.list_alerts(recipient="ghost", level=None, as_json=True)


# ── disable / enable ─────────────────────────────────────────────────────


def test_disable_requires_an_expiry(consumer):
    """A mute with no TTL is GH#33 with extra steps."""
    with pytest.raises(BayError, match="needs an expiry"):
        alerts.disable_alert(pattern="deploy.complete", for_=None, permanent=False)


def test_disable_with_duration_records_a_ttl(consumer):
    before = int(time.time())
    alerts.disable_alert(pattern="deploy.complete", for_="2h", permanent=False)
    config = alerts._load_config(consumer)
    assert config["alerts_disabled"] == ["deploy.complete"]
    assert config["alert_policy_mute_until"] >= before + 7200


def test_disable_permanent_is_allowed_but_explicit(consumer):
    alerts.disable_alert(pattern="deploy.complete", for_=None, permanent=True)
    config = alerts._load_config(consumer)
    assert config["alerts_disabled"] == ["deploy.complete"]
    assert "alert_policy_mute_until" not in config


def test_disable_accepts_a_glob(consumer):
    alerts.disable_alert(pattern="host.disk_*", for_="1h", permanent=False)
    assert alerts._load_config(consumer)["alerts_disabled"] == [
        "host.disk_page", "host.disk_recovered", "host.disk_warn"]


def test_disable_rejects_an_unknown_id(consumer):
    with pytest.raises(BayError, match="No alert matches"):
        alerts.disable_alert(pattern="not.real", for_="1h", permanent=False)


def test_enable_removes_the_mute(consumer):
    alerts.disable_alert(pattern="deploy.complete", for_="1h", permanent=False)
    alerts.enable_alert(pattern="deploy.complete")
    assert alerts._load_config(consumer)["alerts_disabled"] == []


@pytest.mark.parametrize("text,seconds", [("90", 90), ("30m", 1800), ("2h", 7200), ("7d", 604800)])
def test_duration_parsing(text, seconds):
    assert alerts._parse_duration(text) == seconds


def test_bad_duration_is_rejected():
    with pytest.raises(BayError, match="Cannot parse duration"):
        alerts._parse_duration("soon")


# ── secrets hygiene ──────────────────────────────────────────────────────


def test_writes_never_contain_a_literal_secret(consumer):
    """The CLI writes vault references, never values."""
    alerts.disable_alert(pattern="deploy.complete", for_="1h", permanent=False)
    text = alerts._config_path(consumer).read_text()
    for leaked in ("bot_token:", "password", "AAAA", "http"):
        assert leaked not in text, f"{leaked!r} appeared in written config"


def test_comments_survive_a_round_trip(consumer):
    """Config goes through ruamel so an operator's notes are not eaten."""
    alerts._config_path(consumer).write_text(
        "---\n# keep me: explains why prune noise is muted\nalerts_disabled: []\n")
    alerts.disable_alert(pattern="deploy.complete", for_="1h", permanent=False)
    assert "# keep me" in alerts._config_path(consumer).read_text()


# ── doctor ───────────────────────────────────────────────────────────────


def test_doctor_passes_on_clean_config(consumer):
    _write(consumer, {"alert_recipients": [_webhook("chat", "https://c.invalid", "info")]})
    alerts.doctor()  # no raise


def test_doctor_flags_duplicate_targets(consumer):
    _write(consumer, {"alert_recipients": [
        _webhook("a", "https://same.invalid"), _webhook("b", "https://same.invalid")]})
    with pytest.raises(typer.Exit):
        alerts.doctor()


def test_doctor_flags_a_permanent_mute(consumer):
    _write(consumer, {
        "alert_recipients": [_webhook("chat", "https://c.invalid", "debug")],
        "alerts_disabled": ["deploy.complete"]})
    with pytest.raises(typer.Exit):
        alerts.doctor()


def test_doctor_flags_a_muted_id_not_in_the_registry(consumer):
    _write(consumer, {
        "alert_recipients": [_webhook("chat", "https://c.invalid", "debug")],
        "alerts_disabled": ["typo.alert"],
        "alert_policy_mute_until": int(time.time()) + 3600})
    with pytest.raises(typer.Exit):
        alerts.doctor()


def test_doctor_flags_a_recipient_with_no_target(consumer):
    _write(consumer, {"alert_recipients": [
        {"name": "empty", "adapter": "webhook", "min_level": "info", "config": {}}]})
    with pytest.raises(typer.Exit):
        alerts.doctor()


# ── test ─────────────────────────────────────────────────────────────────


def test_test_is_a_dry_run_by_default(consumer, capsys):
    """Uses alerts.test, not deploy.complete — deploy.complete is off by
    default now, so an info-floor recipient would show zero recipients and
    the "chat" assertion below would fail for the wrong reason."""
    _write(consumer, {"alert_recipients": [_webhook("chat", "https://c.invalid", "info")]})
    alerts.test_alert(alert_id="alerts.test", recipient=None, live=False)
    out = capsys.readouterr().out
    assert "Dry run" in out
    assert "chat" in out


def test_test_rejects_an_unknown_alert(consumer):
    with pytest.raises(BayError, match="Unknown alert"):
        alerts.test_alert(alert_id="not.real", recipient=None, live=False)


def test_live_refuses_rather_than_sending_from_the_control_node(consumer):
    """A control-node send would prove nothing about the rendered host scripts,
    and a stale rendered script is exactly what GH#33 was."""
    _write(consumer, {"alert_recipients": [_webhook("chat", "https://c.invalid", "info")]})
    with pytest.raises(BayError, match="RENDERED emitter"):
        alerts.test_alert(alert_id="deploy.complete", recipient=None, live=True)
