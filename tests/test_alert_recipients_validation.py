"""Validation for the alert recipient list, and the two migration traps.

Both traps are real, not hypothetical: they were identified against the actual
consumer configs in this workspace, where `alert_webhook_url` lives in
`group_vars/production/main.yml` while the CLI writes `group_vars/all/`.
"""

from __future__ import annotations

import pytest

from bay_cli.commands.validate import (
    ValidationResult,
    _validate_alert_recipients,
)


def _run(files: dict) -> ValidationResult:
    result = ValidationResult()
    _validate_alert_recipients(files, result)
    return result


def _webhook(name, url, **cfg):
    config = {"url": url}
    config.update(cfg)
    return {"name": name, "adapter": "webhook", "config": config}


# ── Shape ────────────────────────────────────────────────────────────────


def test_no_config_at_all_is_not_an_error():
    assert _run({"group_vars/all/main.yml": {}}).failed == []


def test_valid_recipient_passes():
    result = _run({"group_vars/all/alerts.yml": {
        "alert_recipients": [_webhook("chat", "https://example.invalid/h")]}})
    assert result.failed == []


def test_unknown_adapter_is_rejected():
    result = _run({"group_vars/all/alerts.yml": {"alert_recipients": [
        {"name": "x", "adapter": "carrier-pigeon", "config": {"url": "u"}}]}})
    assert any("carrier-pigeon" in f for f in result.failed)


def test_unknown_min_level_is_rejected():
    result = _run({"group_vars/all/alerts.yml": {"alert_recipients": [
        {"name": "x", "adapter": "webhook", "min_level": "emergency",
         "config": {"url": "u"}}]}})
    assert any("emergency" in f for f in result.failed)


@pytest.mark.parametrize("reserved", ["include", "exclude"])
def test_reserved_keys_are_rejected_rather_than_ignored(reserved):
    """Silently ignoring a reserved key ships config that does nothing."""
    result = _run({"group_vars/all/alerts.yml": {"alert_recipients": [
        {"name": "x", "adapter": "webhook", "config": {"url": "u"},
         reserved: ["build.failed"]}]}})
    assert any(reserved in f for f in result.failed)


def test_webhook_without_url_is_rejected():
    result = _run({"group_vars/all/alerts.yml": {"alert_recipients": [
        {"name": "x", "adapter": "webhook", "config": {}}]}})
    assert any("url" in f for f in result.failed)


def test_telegram_without_credentials_is_rejected():
    result = _run({"group_vars/all/alerts.yml": {"alert_recipients": [
        {"name": "x", "adapter": "telegram", "config": {}}]}})
    assert len(result.failed) == 2  # bot_token and chat_id


def test_env_indirection_satisfies_the_credential_requirement():
    result = _run({"group_vars/all/alerts.yml": {"alert_recipients": [
        {"name": "x", "adapter": "telegram",
         "config": {"token_env": "TG_TOKEN", "chat_id_env": "TG_CHAT"}}]}})
    assert result.failed == []


def test_duplicate_names_are_rejected():
    result = _run({"group_vars/all/alerts.yml": {"alert_recipients": [
        _webhook("dup", "https://a.invalid"), _webhook("dup", "https://b.invalid")]}})
    assert any("Duplicate recipient name" in f for f in result.failed)


# ── Trap 1: duplicate delivery ───────────────────────────────────────────


def test_two_recipients_sharing_a_target_are_rejected():
    result = _run({"group_vars/all/alerts.yml": {"alert_recipients": [
        _webhook("a", "https://same.invalid/h"),
        _webhook("b", "https://same.invalid/h")]}})
    assert any("twice" in f for f in result.failed)


def test_explicit_recipient_colliding_with_the_legacy_webhook_is_rejected():
    """The migration failure mode: keep alert_webhook_url, add a recipient
    pointing at the same place, and every alert arrives twice."""
    result = _run({
        "group_vars/production/main.yml": {"alert_webhook_url": "https://chat.invalid/h"},
        "group_vars/all/alerts.yml": {
            "alert_recipients": [_webhook("chat", "https://chat.invalid/h")]},
    })
    assert any("twice" in f for f in result.failed)


def test_explicit_recipient_at_a_different_target_is_fine():
    result = _run({
        "group_vars/production/main.yml": {"alert_webhook_url": "https://chat.invalid/h"},
        "group_vars/all/alerts.yml": {
            "alert_recipients": [_webhook("oncall", "https://pager.invalid/h")]},
    })
    assert result.failed == []


# ── Trap 2: group_vars precedence ────────────────────────────────────────


def test_env_level_legacy_beside_all_level_list_warns():
    """Ansible precedence puts group_vars/<env>/ above group_vars/all/, so a
    legacy sink at env level keeps firing alongside the new list."""
    result = _run({
        "group_vars/production/main.yml": {"alert_webhook_url": "https://chat.invalid/h"},
        "group_vars/all/alerts.yml": {
            "alert_recipients": [_webhook("oncall", "https://pager.invalid/h")]},
    })
    assert any("precedence" in w for w in result.warnings)


def test_both_at_all_level_does_not_warn():
    result = _run({"group_vars/all/alerts.yml": {
        "alert_webhook_url": "https://chat.invalid/h",
        "alert_recipients": [_webhook("oncall", "https://pager.invalid/h")]}})
    assert not any("precedence" in w for w in result.warnings)
