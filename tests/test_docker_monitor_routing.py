"""docker-monitor routes alerts the way bay_notify does.

Before this, `send_alert` in docker-monitor.py.j2 threw its alert ID away and
sent only through the LEGACY pair (TELEGRAM_* and ALERT_WEBHOOK_URL). A
consumer that had moved to `alert_recipients`, as docs/alerting.md tells it
to, has those empty, so container.crash, container.restart_loop and
container.health_check_failed went nowhere while `bin/bay alerts list` said
they were delivered. A legacy consumer, meanwhile, could not mute them.

These tests render the real template, import the result as a module, drive
its event handlers and record every outbound request. Nothing touches the
network: urllib's urlopen is replaced for the duration of each test.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import types
import urllib.request
from pathlib import Path

import pytest

_TESTS_DIR = Path(__file__).resolve().parent
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

from helpers import make_ansible_env  # noqa: E402

_REPO_ROOT = _TESTS_DIR.parent
_TEMPLATES = _REPO_ROOT / "roles" / "docker_monitor" / "templates"
_CANONICAL = _REPO_ROOT / "roles" / "alert_channel" / "templates"

_SECRET_TOKEN = "SENTINEL-DM-TOKEN-5c1e"
_SECRET_URL = "https://hooks.example.com/SENTINEL-DM-URL-5c1e"
_LEGACY_TOKEN = "SENTINEL-DM-LEGACY-TOKEN-5c1e"
_LEGACY_URL = "https://legacy.example.com/SENTINEL-DM-LEGACY-5c1e"

# A critical-only Telegram pager and a warn-floor chat webhook: the shape the
# docs recommend, and the one that exposed the bug.
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
        "min_level": "info",
        "config": {"url": _SECRET_URL, "format": "slack"},
    },
]


def _render(tmp_path: Path, **ctx) -> str:
    env = make_ansible_env(_TEMPLATES)
    env.filters["bool"] = bool
    env.filters["string"] = str
    base = dict(
        ansible_managed="test",
        inventory_hostname="host1",
        stack_dir=str(tmp_path),
        docker_monitor_alert_container_crash=True,
        docker_monitor_alert_restart_loop=True,
        docker_monitor_alert_healthcheck_failure=True,
        docker_monitor_restart_loop_threshold=3,
        docker_monitor_restart_loop_window=300,
        docker_monitor_restart_loop_cooldown=1800,
        docker_monitor_ignore_containers=["-new"],
        docker_monitor_alert_header="",
        docker_monitor_alert_footer="",
        alert_policy_path=str(tmp_path / "alert-overrides"),
    )
    base.update(ctx)
    return env.get_template("docker-monitor.py.j2").render(**base)


def _load(source: str) -> types.ModuleType:
    """Import a rendered monitor. `main()` is behind the __main__ guard."""
    module = types.ModuleType("docker_monitor_under_test")
    exec(compile(source, "docker-monitor.py", "exec"), module.__dict__)
    return module


@pytest.fixture
def sent(monkeypatch):
    """Record every request instead of sending it."""
    calls: list[dict] = []

    def fake_urlopen(request, timeout=None):
        calls.append(
            {
                "url": request.full_url,
                "body": request.data.decode("utf-8") if request.data else "",
                "content_type": request.get_header("Content-type"),
                "method": request.get_method(),
            }
        )
        return None

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return calls


@pytest.fixture
def recipients_only(monkeypatch):
    """A migrated consumer: legacy pair empty, recipient creds in alert.env."""
    for name in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "ALERT_WEBHOOK_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("BAY_RC_1_TOKEN", _SECRET_TOKEN)
    monkeypatch.setenv("BAY_RC_2_URL", _SECRET_URL)


def _telegram(calls):
    return [c for c in calls if c["url"].startswith("https://api.telegram.org/")]


def _to(calls, url):
    return [c for c in calls if c["url"] == url]


def _die(monitor, name, code="1"):
    monitor.handle_die({"Action": "die", "Actor": {"Attributes": {"name": name, "exitCode": code}}})


def _start(monitor, name):
    monitor.handle_start({"Action": "start", "Actor": {"Attributes": {"name": name}}})


def _loop(monitor, name):
    for _ in range(3):
        _start(monitor, name)


# ── (a) routing by severity ──────────────────────────────────────────────


def test_restart_loop_reaches_the_critical_pager(tmp_path, sent, recipients_only):
    monitor = _load(_render(tmp_path, alert_recipients=_RECIPIENTS))
    _loop(monitor, "web")

    tg = _telegram(sent)
    assert len(tg) == 1, "container.restart_loop is critical and must page"
    assert tg[0]["url"] == f"https://api.telegram.org/bot{_SECRET_TOKEN}/sendMessage"
    payload = json.loads(tg[0]["body"])
    assert payload["chat_id"] == "-1001"
    assert payload["parse_mode"] == "HTML"
    assert "Restart loop detected" in payload["text"]
    # warn floor clears critical too.
    assert len(_to(sent, _SECRET_URL)) == 1


def test_crash_reaches_only_the_warn_floor_webhook(tmp_path, sent, recipients_only):
    monitor = _load(_render(tmp_path, alert_recipients=_RECIPIENTS))
    _die(monitor, "web")

    assert _telegram(sent) == [], "container.crash is warn; the critical pager must not get it"
    hooks = _to(sent, _SECRET_URL)
    assert len(hooks) == 1
    assert hooks[0]["content_type"] == "application/json"
    body = json.loads(hooks[0]["body"])
    assert "*Container crash*" in body["text"], "slack preset must transform to mrkdwn"


def test_token_env_and_url_env_are_read_from_the_environment(tmp_path, sent, monkeypatch):
    for name in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "ALERT_WEBHOOK_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DM_TEST_TOKEN", _SECRET_TOKEN)
    monkeypatch.setenv("DM_TEST_CHAT", "-2002")
    monkeypatch.setenv("DM_TEST_URL", _SECRET_URL)
    recipients = [
        {"name": "p", "adapter": "telegram", "min_level": "warn",
         "config": {"token_env": "DM_TEST_TOKEN", "chat_id_env": "DM_TEST_CHAT"}},
        {"name": "h", "adapter": "webhook", "min_level": "warn",
         "config": {"url_env": "DM_TEST_URL", "content_type": "text/plain",
                    "transform": "text", "method": "PUT", "headers": {"X-Key": "k"}}},
    ]
    monitor = _load(_render(tmp_path, alert_recipients=recipients))
    _die(monitor, "web")

    tg = _telegram(sent)
    assert len(tg) == 1 and json.loads(tg[0]["body"])["chat_id"] == "-2002"
    hooks = _to(sent, _SECRET_URL)
    assert len(hooks) == 1
    assert hooks[0]["method"] == "PUT"
    assert hooks[0]["content_type"] == "text/plain"
    assert "<b>" not in hooks[0]["body"], "text transform strips tags"


def test_a_dead_recipient_never_raises(tmp_path, monkeypatch, recipients_only):
    def boom(request, timeout=None):
        raise OSError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    monitor = _load(_render(tmp_path, alert_recipients=_RECIPIENTS))
    _die(monitor, "web")
    _loop(monitor, "api")


# ── (b) alerts_disabled ──────────────────────────────────────────────────


def test_alerts_disabled_suppresses_for_recipients(tmp_path, sent, recipients_only):
    monitor = _load(
        _render(tmp_path, alert_recipients=_RECIPIENTS, alerts_disabled=["container.crash"])
    )
    _die(monitor, "web")
    assert sent == []


# ── (c) default-off container.recovered ──────────────────────────────────


def test_recovered_is_off_by_default(tmp_path, sent, recipients_only):
    monitor = _load(_render(tmp_path, alert_recipients=_RECIPIENTS))
    _die(monitor, "web")
    sent.clear()
    _start(monitor, "web")
    assert sent == [], "container.recovered is enabled_by_default: false"


def test_recovered_is_sent_with_alerts_enabled(tmp_path, sent, recipients_only):
    monitor = _load(
        _render(tmp_path, alert_recipients=_RECIPIENTS, alerts_enabled=["container.recovered"])
    )
    _die(monitor, "web")
    sent.clear()
    _start(monitor, "web")
    # alerts_enabled overrides min_level too, so the critical pager gets it.
    assert len(_to(sent, _SECRET_URL)) == 1
    assert len(_telegram(sent)) == 1
    assert "Container recovered" in json.loads(_to(sent, _SECRET_URL)[0]["body"])["text"]


# ── The legacy pair: grandfathered exactly like bay_notify ───────────────


@pytest.fixture
def legacy_only(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", _LEGACY_TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "-3003")
    monkeypatch.setenv("ALERT_WEBHOOK_URL", _LEGACY_URL)


def test_legacy_pair_fires_unconditionally_like_bay_notify(tmp_path, sent, legacy_only):
    """bay_notify never consults the registry for the legacy pair, so neither
    does docker-monitor: default-off and alerts_disabled do not reach it."""
    monitor = _load(_render(tmp_path, alerts_disabled=["container.crash"]))
    _die(monitor, "web")
    _start(monitor, "web")  # container.recovered, default-off
    tg = _telegram(sent)
    assert len(tg) == 2 and len(_to(sent, _LEGACY_URL)) == 2


def test_legacy_telegram_request_is_unchanged(tmp_path, sent, legacy_only):
    monitor = _load(_render(tmp_path, docker_monitor_alert_header="H", docker_monitor_alert_footer="F"))
    monitor.send_alert("container.crash", "<b>m</b>")
    tg = _telegram(sent)
    assert tg[0]["url"] == f"https://api.telegram.org/bot{_LEGACY_TOKEN}/sendMessage"
    assert tg[0]["content_type"] == "application/json"
    assert tg[0]["body"] == json.dumps(
        {"chat_id": "-3003", "text": "H<b>m</b>F", "parse_mode": "HTML",
         "disable_web_page_preview": True}
    )
    hook = _to(sent, _LEGACY_URL)
    assert hook[0]["content_type"] == "text/html" and hook[0]["body"] == "H<b>m</b>F"


# ── (d) the operator mute file ───────────────────────────────────────────


def _mute(tmp_path, ids, until=None):
    until = int(time.time()) + 3600 if until is None else until
    (tmp_path / "alert-overrides").write_text(
        f"BAY_ALERTS_SCHEMA=1\nBAY_ALERTS_MUTE={' '.join(ids)}\nBAY_ALERTS_MUTE_UNTIL={until}\n"
    )


def test_mute_file_suppresses_recipients_and_legacy(tmp_path, sent, recipients_only, legacy_only):
    _mute(tmp_path, ["container.crash"])
    monitor = _load(_render(tmp_path, alert_recipients=_RECIPIENTS))
    _die(monitor, "web")
    assert sent == [], "a mute must silence every sink, legacy included"
    _loop(monitor, "api")
    assert sent, "an unmuted alert still fires"


def test_expired_mute_is_inert(tmp_path, sent, recipients_only):
    _mute(tmp_path, ["container.crash"], until=int(time.time()) - 60)
    monitor = _load(_render(tmp_path, alert_recipients=_RECIPIENTS))
    _die(monitor, "web")
    assert len(_to(sent, _SECRET_URL)) == 1


_POLICY_BODIES = [
    pytest.param(None, id="missing"),
    pytest.param("", id="empty"),
    pytest.param("BAY_ALERTS_SCHEMA=1\nBAY_ALERTS_MUTE=container.crash", id="truncated"),
    pytest.param("BAY_ALERTS_SCHEMA=2\nBAY_ALERTS_MUTE=container.crash\nBAY_ALERTS_MUTE_UNTIL=9999999999", id="newer-schema"),
    pytest.param("BAY_ALERTS_SCHEMA=1\nBAY_ALERTS_MUTE=container.crash\nBAY_ALERTS_MUTE_UNTIL=soon", id="bad-epoch"),
    pytest.param("BAY_ALERTS_SCHEMA=x\nBAY_ALERTS_MUTE=container.crash\nBAY_ALERTS_MUTE_UNTIL=9999999999", id="bad-schema"),
    pytest.param("BAY_ALERTS_SCHEMA=1\r\nBAY_ALERTS_MUTE=container.crash\r\nBAY_ALERTS_MUTE_UNTIL=9999999999\r\n", id="crlf"),
    pytest.param("\x00\x01garbage\xff", id="binary"),
    pytest.param("BAY_ALERTS_SCHEMA=1\nBAY_ALERTS_MUTE=a.b container.crash\nBAY_ALERTS_MUTE_UNTIL=9999999999", id="muted-list"),
    pytest.param("BAY_ALERTS_SCHEMA=1\nBAY_ALERTS_MUTE=container.crashed\nBAY_ALERTS_MUTE_UNTIL=9999999999", id="no-prefix-match"),
]


@pytest.mark.parametrize("body", _POLICY_BODIES)
def test_python_mute_check_agrees_with_bash(tmp_path, body):
    """bay_alert_muted is a port of _bay_muted; the two must never disagree."""
    policy = tmp_path / "alert-overrides"
    if body is not None:
        policy.write_bytes(body.encode("utf-8", "surrogateescape"))

    snippet = make_ansible_env(_CANONICAL).get_template("_notify.sh.j2").render(
        alert_policy_path=str(policy),
        alert_env_path=str(tmp_path / "absent.env"),
    )
    proc = subprocess.run(
        ["bash", "-c", snippet + "\n_bay_muted container.crash && echo MUTED || echo OPEN"],
        capture_output=True, text=True, check=True,
    )
    bash_muted = proc.stdout.strip() == "MUTED"

    monitor = _load(_render(tmp_path, alert_policy_path=str(policy)))
    assert monitor.bay_alert_muted("container.crash", str(policy)) is bash_muted


# ── (e) secrets stay out of the rendered file ────────────────────────────


def test_no_secret_is_rendered_into_the_monitor(tmp_path):
    out = _render(
        tmp_path,
        alert_recipients=_RECIPIENTS,
        docker_monitor_telegram_bot_token=_LEGACY_TOKEN,
        docker_monitor_telegram_chat_id="-3003",
        alert_webhook_url=_LEGACY_URL,
    )
    for secret in (_SECRET_TOKEN, _SECRET_URL, _LEGACY_TOKEN, _LEGACY_URL):
        assert secret not in out, f"{secret!r} was rendered into docker-monitor.py"
    # ...and they DO reach the at-rest store under the index the monitor reads.
    env_file = make_ansible_env(_CANONICAL).get_template("alert.env.j2").render(
        ansible_managed="test", alert_recipients=_RECIPIENTS,
    )
    assert f"BAY_RC_1_TOKEN='{_SECRET_TOKEN}'" in env_file
    assert f"BAY_RC_2_URL='{_SECRET_URL}'" in env_file


def test_hostile_recipient_values_stay_data(tmp_path, sent, recipients_only):
    """A quote or a newline in a name or chat ID must not become Python."""
    hostile = '"); import os; os.system("id"); ("\n'
    recipients = [{"name": hostile, "adapter": "telegram", "min_level": "warn",
                   "config": {"bot_token": "x", "chat_id": hostile}}]
    monitor = _load(_render(tmp_path, alert_recipients=recipients))
    assert monitor.BAY_RECIPIENTS[0]["name"] == hostile
    assert monitor.BAY_RECIPIENTS[0]["chat_id"] == hostile


def test_invalid_env_name_fails_the_render(tmp_path):
    recipients = [{"name": "p", "adapter": "webhook", "config": {"url_env": "X:-$(id)"}}]
    with pytest.raises(ValueError):
        _render(tmp_path, alert_recipients=recipients)


# ── (f) restart-loop cooldown ────────────────────────────────────────────


def test_restart_loop_cooldown_suppresses_then_allows(tmp_path, sent, recipients_only):
    monitor = _load(_render(tmp_path, alert_recipients=_RECIPIENTS))
    _loop(monitor, "web")
    assert len(_telegram(sent)) == 1

    _loop(monitor, "web")
    _loop(monitor, "web")
    assert len(_telegram(sent)) == 1, "a looping container re-alerted inside the cooldown"

    # Another container has its own cooldown.
    _loop(monitor, "api")
    assert len(_telegram(sent)) == 2

    # Move the last alert back past the window.
    monitor.restart_loop_alerted["web"] -= monitor.RESTART_LOOP_COOLDOWN + 1
    _loop(monitor, "web")
    assert len(_telegram(sent)) == 3, "the cooldown must expire"


def test_restart_loop_cooldown_survives_a_monitor_restart(tmp_path, sent, recipients_only):
    source = _render(tmp_path, alert_recipients=_RECIPIENTS)
    first = _load(source)
    first.crash_state = first.load_crash_state()
    _loop(first, "web")
    assert len(_telegram(sent)) == 1

    state = json.loads((tmp_path / ".container-crashes.json").read_text())
    assert "web" in state[first.RESTART_LOOP_STATE_KEY]

    second = _load(source)
    second.crash_state = second.load_crash_state()
    assert first.RESTART_LOOP_STATE_KEY not in second.crash_state, (
        "the cooldown key must not be read back as a crashed container"
    )
    _loop(second, "web")
    assert len(_telegram(sent)) == 1, "a monitor restart reset the cooldown"


def test_cooldown_zero_disables_it(tmp_path, sent, recipients_only):
    monitor = _load(
        _render(tmp_path, alert_recipients=_RECIPIENTS, docker_monitor_restart_loop_cooldown=0)
    )
    _loop(monitor, "web")
    _loop(monitor, "web")
    assert len(_telegram(sent)) == 2


def test_cooldown_default_is_in_the_role_defaults():
    import yaml

    defaults = yaml.safe_load(
        (_REPO_ROOT / "roles" / "docker_monitor" / "defaults" / "main.yml").read_text()
    )
    assert defaults["docker_monitor_restart_loop_cooldown"] == 1800


# ── crash_state is pruned by age ─────────────────────────────────────────
#
# A crash row is written on `die` and removed on the next `start`. A one-off
# `docker run` container never starts again, so its row stayed in the state
# file for ever. save_crash_state now drops rows older than
# docker_monitor_crash_state_max_age.


def _seed_state(tmp_path, rows):
    (tmp_path / ".container-crashes.json").write_text(json.dumps(rows))


def _state(tmp_path):
    return json.loads((tmp_path / ".container-crashes.json").read_text())


def _crash_row(age_seconds):
    from datetime import datetime, timedelta

    return {
        "exit_code": 1,
        "crashed_at": (datetime.now() - timedelta(seconds=age_seconds)).isoformat(),
        "crash_count": 1,
    }


def test_old_crash_rows_are_pruned_on_save(tmp_path, sent, recipients_only):
    _seed_state(
        tmp_path,
        {
            "one-off-run": _crash_row(30 * 86400),
            "fresh": _crash_row(3600),
        },
    )
    monitor = _load(_render(tmp_path, alert_recipients=_RECIPIENTS))
    monitor.crash_state = monitor.load_crash_state()
    _die(monitor, "web")  # any write goes through save_crash_state

    state = _state(tmp_path)
    assert "one-off-run" not in state, "a 30-day-old crash row survived the save"
    assert "fresh" in state and "web" in state


@pytest.mark.parametrize(
    "row",
    [
        {"exit_code": 1, "crashed_at": "yesterday-ish", "crash_count": 1},
        {"exit_code": 1, "crash_count": 1},
        {"exit_code": 1, "crashed_at": None, "crash_count": 1},
        "not-a-row",
    ],
    ids=["garbage", "missing", "null", "not-a-dict"],
)
def test_an_unparsable_crash_row_is_dropped_not_fatal(tmp_path, sent, recipients_only, row):
    """Its age can never be known, so keeping it would mean keeping it for ever."""
    _seed_state(tmp_path, {"broken": row, "fresh": _crash_row(60)})
    monitor = _load(_render(tmp_path, alert_recipients=_RECIPIENTS))
    monitor.crash_state = monitor.load_crash_state()
    _die(monitor, "web")

    state = _state(tmp_path)
    assert "broken" not in state
    assert "fresh" in state and "web" in state
    assert len(_to(sent, _SECRET_URL)) == 1, "the crash alert still fired"


def test_an_aware_timestamp_is_compared_not_fatal(tmp_path, sent, recipients_only):
    from datetime import datetime, timedelta, timezone

    old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    new = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    _seed_state(
        tmp_path,
        {
            "old": {"exit_code": 1, "crashed_at": old, "crash_count": 1},
            "new": {"exit_code": 1, "crashed_at": new, "crash_count": 1},
        },
    )
    monitor = _load(_render(tmp_path, alert_recipients=_RECIPIENTS))
    monitor.crash_state = monitor.load_crash_state()
    _die(monitor, "web")
    state = _state(tmp_path)
    assert "old" not in state and "new" in state


def test_crash_state_max_age_is_configurable(tmp_path, sent, recipients_only):
    _seed_state(tmp_path, {"hour-old": _crash_row(3600)})
    monitor = _load(
        _render(tmp_path, alert_recipients=_RECIPIENTS, docker_monitor_crash_state_max_age=600)
    )
    monitor.crash_state = monitor.load_crash_state()
    _die(monitor, "web")
    assert "hour-old" not in _state(tmp_path)


def test_crash_state_max_age_zero_disables_the_prune(tmp_path, sent, recipients_only):
    _seed_state(
        tmp_path,
        {
            "ancient": _crash_row(365 * 86400),
            "broken": {"exit_code": 1, "crashed_at": "garbage", "crash_count": 1},
        },
    )
    monitor = _load(
        _render(tmp_path, alert_recipients=_RECIPIENTS, docker_monitor_crash_state_max_age=0)
    )
    monitor.crash_state = monitor.load_crash_state()
    _die(monitor, "web")
    state = _state(tmp_path)
    assert "ancient" in state and "broken" in state


def test_restart_loop_cooldowns_survive_the_crash_prune(tmp_path, sent, recipients_only):
    """The cooldown map shares the file; the prune must not eat it."""
    monitor = _load(_render(tmp_path, alert_recipients=_RECIPIENTS))
    monitor.crash_state = monitor.load_crash_state()
    _loop(monitor, "web")
    assert "web" in _state(tmp_path)[monitor.RESTART_LOOP_STATE_KEY]


def test_crash_state_max_age_default_is_in_the_role_defaults():
    import yaml

    defaults = yaml.safe_load(
        (_REPO_ROOT / "roles" / "docker_monitor" / "defaults" / "main.yml").read_text()
    )
    assert defaults["docker_monitor_crash_state_max_age"] == 604800

