"""The webhook receiver routes alerts the way bay_notify and docker-monitor do.

Before this, `send_alert()` in roles/git_deploy/files/webhook/app.py read only
the LEGACY pair (TELEGRAM_* and ALERT_WEBHOOK_URL). A consumer on
`alert_recipients` has those empty, so `webhook.fanout_failed` (warn, on by
default) reached nobody while `bin/bay alerts list` said it was delivered.

These tests render the real bay-webhook.env template, feed it through the
reconciler's own env-file parser into the environment, load a fresh copy of
app.py, and record every outbound request. Nothing touches the network.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path

import pytest
import yaml

_TESTS_DIR = Path(__file__).resolve().parent
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

from helpers import make_ansible_env  # noqa: E402

_REPO_ROOT = _TESTS_DIR.parent
_ROLES = _REPO_ROOT / "roles"
_WEBHOOK_DIR = _ROLES / "git_deploy" / "files" / "webhook"
_DEPLOY_STACK_TEMPLATES = _ROLES / "deploy_stack" / "templates"
_ALERT_TEMPLATES = _ROLES / "alert_channel" / "templates"

if str(_WEBHOOK_DIR) not in sys.path:
    sys.path.insert(0, str(_WEBHOOK_DIR))

_SECRET_TOKEN = "SENTINEL-WH-TOKEN-7d2b"
_SECRET_URL = "https://hooks.example.com/SENTINEL-WH-URL-7d2b"
_SECRET_HEADER = "Bearer SENTINEL-WH-HEADER-7d2b"
_LEGACY_TOKEN = "SENTINEL-WH-LEGACY-TOKEN-7d2b"
_LEGACY_URL = "https://legacy.example.com/SENTINEL-WH-LEGACY-7d2b"
_SECRETS = (_SECRET_TOKEN, _SECRET_URL, _SECRET_HEADER)

# A critical-only Telegram pager and a warn-floor webhook: the shape the docs
# recommend. webhook.fanout_failed is warn, so only the webhook may get it.
_RECIPIENTS = [
    {
        "name": "pager",
        "adapter": "telegram",
        "min_level": "critical",
        "config": {"bot_token": _SECRET_TOKEN, "chat_id": "-1001"},
    },
    {
        "name": "chat",
        "adapter": "webhook",
        "min_level": "warn",
        "config": {
            "url": _SECRET_URL,
            "format": "raw",
            "headers": {"Authorization": _SECRET_HEADER},
        },
    },
]

_LEGACY_VARS = dict(
    docker_monitor_telegram_bot_token=_LEGACY_TOKEN,
    docker_monitor_telegram_chat_id="-3003",
    alert_webhook_url=_LEGACY_URL,
)


def _parse_env_file(content: str) -> dict[str, str]:
    """The reconciler's parser: production reads bay-webhook.env through it."""
    spec = importlib.util.spec_from_file_location(
        "bay_filters_for_webhook_routing", _REPO_ROOT / "filter_plugins" / "bay_filters.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.parse_env_file(content)


def _render_webhook_env(**ctx) -> str:
    env = make_ansible_env(_DEPLOY_STACK_TEMPLATES)
    base = dict(ansible_managed="test", webhook={"secret": "s3cret"})
    base.update(ctx)
    return env.get_template("webhook.env.j2").render(**base)


def _render_alert_env(**ctx) -> str:
    env = make_ansible_env(_ALERT_TEMPLATES)
    base = dict(ansible_managed="test", alert_env_path="/etc/bay/alert.env")
    base.update(ctx)
    return env.get_template("alert.env.j2").render(**base)


_LOADED = 0


def _load_app(monkeypatch, tmp_path, env_file: str, policy: Path | None = None):
    """A fresh app.py, imported with exactly the env the container would get."""
    global _LOADED
    for name in list(os.environ):
        if name.startswith(("BAY_RC_", "BAY_ALERT_")) or name in (
            "TELEGRAM_BOT_TOKEN",
            "TELEGRAM_CHAT_ID",
            "ALERT_WEBHOOK_URL",
            "ALERT_POLICY_PATH",
            "TELEGRAM_HEADER",
        ):
            monkeypatch.delenv(name, raising=False)
    for key, value in _parse_env_file(env_file).items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("ALERT_POLICY_PATH", str(policy or tmp_path / "alert-overrides"))
    monkeypatch.setenv("TELEGRAM_FAILURES_LOG", str(tmp_path / "failures.log"))
    _LOADED += 1
    spec = importlib.util.spec_from_file_location(
        f"webhook_app_routing_{_LOADED}", _WEBHOOK_DIR / "app.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def sent(monkeypatch):
    calls: list[dict] = []

    def fake_urlopen(request, timeout=None):
        calls.append(
            {
                "url": request.full_url,
                "data": request.data,
                "headers": dict(request.header_items()),
                "method": request.get_method(),
                "timeout": timeout,
            }
        )
        return None

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return calls


def _telegram(calls):
    return [c for c in calls if c["url"].startswith("https://api.telegram.org/")]


def _to(calls, url):
    return [c for c in calls if c["url"] == url]


# ── Routing for a recipients-only consumer ───────────────────────────────


def test_fanout_failed_reaches_the_warn_webhook_not_the_critical_pager(
    tmp_path, sent, monkeypatch
):
    app = _load_app(monkeypatch, tmp_path, _render_webhook_env(alert_recipients=_RECIPIENTS))
    app.send_alert("webhook.fanout_failed", "<b>Fan-out failed</b>")

    assert _telegram(sent) == [], "webhook.fanout_failed is warn; the critical pager must not get it"
    hooks = _to(sent, _SECRET_URL)
    assert len(hooks) == 1, "the warn-floor recipient never got webhook.fanout_failed"
    assert hooks[0]["data"] == b"Fan-out failed", "raw preset strips tags"
    assert hooks[0]["headers"]["Authorization"] == _SECRET_HEADER, (
        "declarative headers must reach the recipient from BAY_RC_<n>_HEADERS"
    )
    assert hooks[0]["headers"]["Content-type"] == "text/plain"


def test_alerts_disabled_suppresses(tmp_path, sent, monkeypatch):
    env_file = _render_webhook_env(
        alert_recipients=_RECIPIENTS, alerts_disabled=["webhook.fanout_failed"]
    )
    app = _load_app(monkeypatch, tmp_path, env_file)
    app.send_alert("webhook.fanout_failed", "<b>Fan-out failed</b>")
    assert sent == []


@pytest.mark.parametrize(
    "alert_id",
    ["webhook.received", "webhook.pull_signal_received", "webhook.image_pull_signal_received"],
)
def test_default_off_ids_are_not_sent(tmp_path, sent, monkeypatch, alert_id):
    app = _load_app(monkeypatch, tmp_path, _render_webhook_env(alert_recipients=_RECIPIENTS))
    app.send_alert(alert_id, "<b>noise</b>")
    assert sent == [], f"{alert_id} is enabled_by_default: false"


def test_alerts_enabled_opts_a_default_off_id_back_in(tmp_path, sent, monkeypatch):
    env_file = _render_webhook_env(
        alert_recipients=_RECIPIENTS, alerts_enabled=["webhook.received"]
    )
    app = _load_app(monkeypatch, tmp_path, env_file)
    app.send_alert("webhook.received", "<b>push</b>")
    assert len(_to(sent, _SECRET_URL)) == 1
    assert len(_telegram(sent)) == 1, "alerts_enabled overrides min_level too"


def _mute(path: Path, ids, until=None):
    until = int(time.time()) + 3600 if until is None else until
    path.write_text(
        f"BAY_ALERTS_SCHEMA=1\nBAY_ALERTS_MUTE={' '.join(ids)}\nBAY_ALERTS_MUTE_UNTIL={until}\n"
    )


def test_mute_suppresses_recipients_and_legacy(tmp_path, sent, monkeypatch):
    policy = tmp_path / "alert-overrides"
    _mute(policy, ["webhook.fanout_failed"])
    env_file = _render_webhook_env(alert_recipients=_RECIPIENTS, **_LEGACY_VARS)
    app = _load_app(monkeypatch, tmp_path, env_file, policy=policy)
    app.send_alert("webhook.fanout_failed", "<b>Fan-out failed</b>")
    assert sent == [], "a mute must silence every sink, legacy included"


def test_mute_is_read_at_call_time(tmp_path, sent, monkeypatch):
    """The receiver runs for weeks; a mute set after start must still apply."""
    policy = tmp_path / "alert-overrides"
    app = _load_app(
        monkeypatch, tmp_path, _render_webhook_env(alert_recipients=_RECIPIENTS), policy=policy
    )
    _mute(policy, ["webhook.fanout_failed"])
    app.send_alert("webhook.fanout_failed", "m")
    assert sent == []


@pytest.mark.parametrize("state", ["missing", "expired", "garbage"])
def test_mute_fails_open(tmp_path, sent, monkeypatch, state):
    policy = tmp_path / "alert-overrides"
    if state == "expired":
        _mute(policy, ["webhook.fanout_failed"], until=int(time.time()) - 60)
    elif state == "garbage":
        policy.write_bytes(b"\x00\xffBAY_ALERTS_MUTE=webhook.fanout_failed")
    app = _load_app(
        monkeypatch, tmp_path, _render_webhook_env(alert_recipients=_RECIPIENTS), policy=policy
    )
    app.send_alert("webhook.fanout_failed", "m")
    assert len(_to(sent, _SECRET_URL)) == 1


def test_a_dead_recipient_never_raises_and_is_logged(tmp_path, monkeypatch):
    def boom(request, timeout=None):
        raise OSError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    app = _load_app(monkeypatch, tmp_path, _render_webhook_env(alert_recipients=_RECIPIENTS))
    app.send_alert("webhook.fanout_failed", "m")
    log = (tmp_path / "failures.log").read_text()
    assert "recipient chat send failed" in log
    for secret in _SECRETS:
        assert secret not in log


# ── The legacy pair: unchanged, byte for byte ────────────────────────────


def test_legacy_requests_are_byte_identical(tmp_path, sent, monkeypatch):
    """The exact requests v0.6.11's inline transport built."""
    app = _load_app(monkeypatch, tmp_path, _render_webhook_env(**_LEGACY_VARS))
    monkeypatch.setattr(app, "MSG_HEADER", "H")
    app.send_alert("webhook.fanout_failed", "<b>m</b>")

    assert len(sent) == 2
    tg, hook = sent
    assert tg["url"] == f"https://api.telegram.org/bot{_LEGACY_TOKEN}/sendMessage"
    assert tg["method"] == "POST"
    assert tg["timeout"] == 10
    assert tg["headers"] == {"Content-type": "application/json"}
    assert tg["data"] == json.dumps(
        {
            "chat_id": "-3003",
            "text": "H<b>m</b>",
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
    ).encode()
    assert hook["url"] == _LEGACY_URL
    assert hook["data"] == b"H<b>m</b>"
    assert hook["headers"]["Content-type"] == "text/html"


def test_legacy_pair_ignores_the_registry(tmp_path, sent, monkeypatch):
    """Grandfathered like bay_notify: default-off still reaches it."""
    app = _load_app(monkeypatch, tmp_path, _render_webhook_env(**_LEGACY_VARS))
    app.send_alert("webhook.received", "m")
    assert len(_telegram(sent)) == 1 and len(_to(sent, _LEGACY_URL)) == 1


def test_legacy_consumer_env_file_gains_nothing():
    out = _render_webhook_env(**_LEGACY_VARS)
    assert not re.search(r"^BAY_", out, flags=re.MULTILINE)


# ── Secrets at rest ──────────────────────────────────────────────────────


def test_routing_table_holds_no_secret():
    env = _parse_env_file(_render_webhook_env(alert_recipients=_RECIPIENTS))
    routing = env["BAY_ALERT_ROUTING"]
    for secret in _SECRETS:
        assert secret not in routing, f"{secret!r} leaked into the routing table"
    entries = json.loads(routing)
    assert [e["index"] for e in entries] == [1, 2]
    assert all("headers" not in e for e in entries)
    assert "webhook.fanout_failed" in entries[1]["ids"]
    assert "webhook.fanout_failed" not in entries[0]["ids"]


def test_secrets_are_only_in_the_indexed_env_lines():
    env = _parse_env_file(_render_webhook_env(alert_recipients=_RECIPIENTS))
    assert env["BAY_RC_1_TOKEN"] == _SECRET_TOKEN
    assert env["BAY_RC_2_URL"] == _SECRET_URL
    assert json.loads(env["BAY_RC_2_HEADERS"]) == {"Authorization": _SECRET_HEADER}
    for key, value in env.items():
        if key in ("BAY_RC_1_TOKEN", "BAY_RC_2_URL", "BAY_RC_2_HEADERS"):
            continue
        for secret in _SECRETS:
            assert secret not in value, f"{secret!r} leaked into {key}"


def test_recipient_indices_agree_with_alert_env():
    """A mismatch is silent: the alert just never sends."""
    webhook_env = _parse_env_file(_render_webhook_env(alert_recipients=_RECIPIENTS))
    alert_env = _render_alert_env(alert_recipients=_RECIPIENTS)
    for key in ("BAY_RC_1_TOKEN", "BAY_RC_2_URL"):
        assert f"{key}='{webhook_env[key]}'" in alert_env


def test_env_recipients_write_no_credential_line():
    recipients = [
        {"name": "e", "adapter": "webhook", "min_level": "warn",
         "config": {"url_env": "BAY_TEST_HOOK_URL"}},
    ]
    env = _parse_env_file(_render_webhook_env(alert_recipients=recipients))
    assert "BAY_RC_1_URL" not in env
    assert json.loads(env["BAY_ALERT_ROUTING"])[0]["url_env"] == "BAY_TEST_HOOK_URL"


def test_invalid_env_name_fails_the_render():
    recipients = [
        {"name": "e", "adapter": "webhook", "min_level": "warn",
         "config": {"url_env": "X:-$(id)"}},
    ]
    with pytest.raises(ValueError):
        _render_webhook_env(alert_recipients=recipients)


def test_a_newline_in_a_credential_fails_the_render():
    recipients = [
        {"name": "t", "adapter": "telegram", "min_level": "warn",
         "config": {"bot_token": "tok\nEVIL=1", "chat_id": "1"}},
    ]
    with pytest.raises(ValueError):
        _render_webhook_env(alert_recipients=recipients)


def test_app_source_holds_no_credential_and_no_private_transport():
    src = (_WEBHOOK_DIR / "app.py").read_text()
    assert "api.telegram.org" not in src and "sendMessage" not in src, (
        "the receiver must use bay_send_telegram from bay_alert.py"
    )


def test_env_file_is_rendered_private():
    tasks = yaml.safe_load((_ROLES / "deploy_stack" / "tasks" / "main.yml").read_text())

    def _walk(nodes):
        for node in nodes:
            yield node
            for key in ("block", "rescue", "always"):
                if key in node:
                    yield from _walk(node[key])

    task = next(
        t for t in _walk(tasks)
        if (t.get("ansible.builtin.template") or {}).get("src") == "webhook.env.j2"
    )
    assert task["ansible.builtin.template"]["mode"] == "0640"
    assert task.get("no_log") is True


# ── How the container gets the mute file ─────────────────────────────────


def _webhook_spec_block() -> str:
    text = (_ROLES / "container_lifecycle" / "tasks" / "build_specs.yml").read_text()
    start = text.index("_webhook_spec:")
    return text[start:text.index("labels:", start)]


@pytest.mark.parametrize(
    "source",
    ["reconciler", "compose"],
)
def test_policy_directory_is_mounted_read_only(source):
    if source == "reconciler":
        block = _webhook_spec_block()
    else:
        block = (_DEPLOY_STACK_TEMPLATES / "_webhook_receiver.j2").read_text()
    assert "| dirname }}:/etc/bay-alert-policy:ro" in block, (
        "mount the DIRECTORY read-only: alert_policy replaces the file by "
        "rename, which a single-file bind mount never sees"
    )
    assert "ALERT_POLICY_PATH" in block and "/etc/bay-alert-policy/" in block


def test_spec_env_carries_no_credential():
    """`env:` lands in Config.Env for anyone who can `docker inspect`."""
    block = _webhook_spec_block()
    env_section = block[block.index("      env:\n"):block.index("      volumes:\n")]
    for needle in ("bot_token", "TELEGRAM_BOT_TOKEN", "alert_recipients", "BAY_RC_", "url }}"):
        assert needle not in env_section


def test_loaded_app_routing_matches_the_monitor_table():
    """Same filter, same entries: headers are the only intended difference."""
    spec = importlib.util.spec_from_file_location(
        "bay_filters_for_parity", _REPO_ROOT / "filter_plugins" / "bay_filters.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with_headers = module.bay_alert_routing(_RECIPIENTS)
    without = module.bay_alert_routing(_RECIPIENTS, with_headers=False)
    for a, b in zip(with_headers, without):
        a = dict(a)
        a.pop("headers")
        assert a == b



def test_no_utcnow_and_the_timestamp_format_is_unchanged(tmp_path, monkeypatch):
    """datetime.utcnow() is deprecated; the alert timestamp must read the same."""
    assert "utcnow(" not in (_WEBHOOK_DIR / "app.py").read_text()
    app = _load_app(monkeypatch, tmp_path, _render_webhook_env())
    assert re.fullmatch(r"[A-Z][a-z]{2} \d{2}, \d{2}:\d{2} UTC", app.format_timestamp())
