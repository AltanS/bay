"""Tests for the pluggable alert sink (GH bay#30, M104).

Bay alerts were Telegram-or-nothing, hardcoded across five different
idioms. `roles/alert_channel/templates/_notify.sh.j2` is now the single
definition of "send an alert"; it is symlinked into every consuming role's
templates/ directory and included by each script.

The behavioural tests here run the rendered bash against a real HTTP sink
rather than grepping the template. Two bugs found during development were
invisible to text inspection: bash expands an unescaped `&` in a
substitution's replacement to the matched text (so `&lt;` rendered as
`<lt;`), and `${#var}` puts a `{#` in the template that Jinja lexes as a
comment opener.
"""

from __future__ import annotations

import http.server
import json
import os
import re
import subprocess
import sys
import threading
from pathlib import Path

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parent.parent
_ROLES = _REPO_ROOT / "roles"
_CANONICAL = _ROLES / "alert_channel" / "templates" / "_notify.sh.j2"
_DEFAULTS = _ROLES / "alert_channel" / "defaults" / "main.yml"

_TESTS_DIR = Path(__file__).resolve().parent
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

from helpers import make_ansible_env  # noqa: E402

# Roles whose templates include the snippet.
_CONSUMING_ROLES = ["git_deploy", "outbound_monitor", "log_archive", "backup"]

# Every shell emitter that must now route through bay_notify.
_CONVERTED_SCRIPTS = [
    "git_deploy/templates/rebuild.sh.j2",
    "git_deploy/templates/build-alert.sh.j2",
    "git_deploy/templates/bay-trigger-watchdog.sh.j2",
    "outbound_monitor/templates/bay-outbound-check.j2",
    "outbound_monitor/templates/bay-disk-alert.sh.j2",
    "log_archive/templates/rotate-logs.sh.j2",
    "backup/templates/backup.sh.j2",
    "backup/templates/maintenance.sh.j2",
]


# ── Structure ────────────────────────────────────────────────────────────


def test_canonical_snippet_exists():
    assert _CANONICAL.is_file()


@pytest.mark.parametrize("role", _CONSUMING_ROLES)
def test_snippet_is_symlinked_into_consuming_role(role):
    """Ansible's template loader only searches the current role's templates/.

    The snippet is shared by symlink, so each consuming role needs a link
    that resolves to the one canonical file.
    """
    link = _ROLES / role / "templates" / "_notify.sh.j2"
    assert link.is_symlink(), f"{role}/templates/_notify.sh.j2 must be a symlink"
    assert link.resolve() == _CANONICAL.resolve(), (
        f"{role}'s link points at {link.resolve()}, not the canonical snippet"
    )


@pytest.mark.parametrize("role", _CONSUMING_ROLES)
def test_symlink_is_committed_as_a_symlink(role):
    """Git must store mode 120000, or `bin/bay install`'s clone breaks sharing."""
    rel = f"roles/{role}/templates/_notify.sh.j2"
    out = subprocess.run(
        ["git", "-C", str(_REPO_ROOT), "ls-files", "-s", rel],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert out.startswith("120000"), (
        f"{rel} is not staged as a symlink (git mode 120000): {out!r}. "
        f"A regular-file copy would silently drift from the canonical snippet."
    )


@pytest.mark.parametrize("script", _CONVERTED_SCRIPTS)
def test_no_script_keeps_a_private_telegram_curl(script):
    """The whole point is one implementation — no role may keep its own sender."""
    content = (_ROLES / script).read_text()
    assert "api.telegram.org" not in content, (
        f"{script} still builds its own Telegram request; it must call "
        f"bay_notify so a second sink reaches it too."
    )
    assert "{% include '_notify.sh.j2' %}" in content, (
        f"{script} does not include the shared alert snippet"
    )


def test_defaults_match_snippet_fallbacks():
    """The snippet repeats the defaults so it works from any role. Catch drift.

    `_notify.sh.j2` is included by roles that do not depend on alert_channel,
    so it cannot rely on that role's defaults being loaded and carries its own
    `| default(...)` values. Two sources of truth is a drift risk; this test is
    the mechanism that keeps them honest.
    """
    declared = yaml.safe_load(_DEFAULTS.read_text())
    snippet = _CANONICAL.read_text()
    expected = {
        "alert_webhook_url": "''",
        "alert_webhook_format": "'campfire'",
        "alert_webhook_max_chars": "3500",
        "alert_webhook_timeout": "10",
    }
    for var, literal in expected.items():
        assert f"{var} | default({literal})" in snippet, (
            f"{var} fallback missing or changed in the snippet"
        )
    assert declared["alert_webhook_url"] == ""
    assert declared["alert_webhook_format"] == "campfire"
    assert declared["alert_webhook_max_chars"] == 3500
    assert declared["alert_webhook_timeout"] == 10


# ── Rendering ────────────────────────────────────────────────────────────


def _render(**ctx) -> str:
    base = {
        "docker_monitor_telegram_bot_token": "",
        "docker_monitor_telegram_chat_id": "",
    }
    base.update(ctx)
    env = make_ansible_env(_CANONICAL.parent)
    return env.get_template("_notify.sh.j2").render(**base)


def test_explicit_defaults_render_identically_to_omitted_defaults():
    """Opting out must be indistinguishable from never having heard of the feature."""
    omitted = _render()
    explicit = _render(
        alert_webhook_url="",
        alert_webhook_format="campfire",
        alert_webhook_max_chars=3500,
        alert_webhook_timeout=10,
    )
    assert omitted == explicit


def test_rendered_snippet_parses():
    script = _render(alert_webhook_url="https://example.invalid/hook")
    proc = subprocess.run(
        ["bash", "-n"], input=script, capture_output=True, text=True, check=False
    )
    assert proc.returncode == 0, proc.stderr


@pytest.mark.parametrize("role", _CONSUMING_ROLES)
def test_snippet_renders_through_each_symlink(role):
    """A broken link would fail here rather than at deploy time."""
    env = make_ansible_env(_ROLES / role / "templates")
    out = env.get_template("_notify.sh.j2").render(
        docker_monitor_telegram_bot_token="",
        docker_monitor_telegram_chat_id="",
    )
    assert "bay_notify()" in out


def test_env_mode_reads_credentials_from_environment():
    """log_archive's systemd unit supplies the credentials as env vars."""
    env = make_ansible_env(_CANONICAL.parent)
    out = env.get_template("_notify.sh.j2").render(
        alert_telegram_env=True,
        docker_monitor_telegram_bot_token="SHOULD-NOT-APPEAR",
        docker_monitor_telegram_chat_id="SHOULD-NOT-APPEAR",
    )
    assert 'BAY_TG_TOKEN="${TELEGRAM_BOT_TOKEN:-}"' in out
    assert "SHOULD-NOT-APPEAR" not in out


def test_outbound_monitor_falls_back_to_its_legacy_var_names():
    """outbound_monitor read a bare telegram_bot_token no consumer ever set.

    Its alerts were rendered with an empty token and silently discarded. The
    templates now prefer docker_monitor_* but must still honour an existing
    override of the old names.
    """
    env = make_ansible_env(_ROLES / "outbound_monitor" / "templates")
    out = env.get_template("bay-disk-alert.sh.j2").render(
        outbound_check_state_dir="/var/lib/x",
        disk_alert_warn_pct=80,
        disk_alert_page_pct=90,
        inventory_hostname="h1",
        telegram_bot_token="LEGACY",
        telegram_chat_id="LEGACY-CHAT",
    )
    assert "BAY_TG_TOKEN='LEGACY'" in out

    out2 = env.get_template("bay-disk-alert.sh.j2").render(
        outbound_check_state_dir="/var/lib/x",
        disk_alert_warn_pct=80,
        disk_alert_page_pct=90,
        inventory_hostname="h1",
        telegram_bot_token="LEGACY",
        telegram_chat_id="LEGACY-CHAT",
        docker_monitor_telegram_bot_token="PREFERRED",
        docker_monitor_telegram_chat_id="PREFERRED-CHAT",
    )
    assert "BAY_TG_TOKEN='PREFERRED'" in out2


# ── Behaviour, against a real HTTP sink ──────────────────────────────────


class _Recorder(http.server.BaseHTTPRequestHandler):
    requests: list[dict] = []

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        type(self).requests.append(
            {
                "content_type": self.headers.get("Content-Type"),
                "body": self.rfile.read(length).decode(),
            }
        )
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args):  # silence
        pass


@pytest.fixture
def sink():
    """A real HTTP endpoint that records what the adapters actually POST."""

    class Handler(_Recorder):
        requests: list[dict] = []

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    yield f"http://{host}:{port}/hook", Handler.requests
    server.shutdown()
    server.server_close()


def _run(snippet: str, body: str, env_extra=None) -> subprocess.CompletedProcess:
    script = "set -euo pipefail\n" + snippet + "\n" + body
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


# A payload with every character class that has broken an alert here.
_HOSTILE = 'cannot read <config> && retry "x" > /dev/null\nsecond line'
_MESSAGE = (
    'MSG="$(printf \'<b>Build failed</b>\\n<pre>%s</pre>\' '
    '"$(bay_html_escape "$UNTRUSTED")")"'
)


def test_campfire_adapter_posts_escaped_html(sink):
    url, requests = sink
    snippet = _render(alert_webhook_url=url, alert_webhook_format="campfire")
    proc = _run(snippet, f"UNTRUSTED={_HOSTILE!r}\n{_MESSAGE}\nbay_notify alerts.test \"$MSG\"")
    assert proc.returncode == 0, proc.stderr
    assert len(requests) == 1
    assert requests[0]["content_type"] == "text/html"
    body = requests[0]["body"]
    assert "&lt;config&gt;" in body, f"< was not escaped correctly: {body!r}"
    assert "&amp;&amp;" in body, f"& was not escaped correctly: {body!r}"
    assert "<lt;" not in body, (
        "bash expanded the & in the replacement to the matched text — "
        "bay_html_escape needs the backslash before each &"
    )
    # Our own markup must survive as real HTML.
    assert "<b>Build failed</b>" in body


def test_slack_adapter_posts_valid_json_mrkdwn(sink):
    url, requests = sink
    snippet = _render(alert_webhook_url=url, alert_webhook_format="slack")
    proc = _run(snippet, f"UNTRUSTED={_HOSTILE!r}\n{_MESSAGE}\nbay_notify alerts.test \"$MSG\"")
    assert proc.returncode == 0, proc.stderr
    assert len(requests) == 1
    assert requests[0]["content_type"] == "application/json"
    payload = json.loads(requests[0]["body"])  # must be valid JSON
    text = payload["text"]
    assert "*Build failed*" in text, "b tags were not converted to mrkdwn"
    # Entities are decoded again for a non-HTML sink.
    assert "<config>" in text and "&&" in text
    assert "&lt;" not in text


def test_raw_adapter_strips_tags_and_decodes_entities(sink):
    url, requests = sink
    snippet = _render(alert_webhook_url=url, alert_webhook_format="raw")
    proc = _run(snippet, f"UNTRUSTED={_HOSTILE!r}\n{_MESSAGE}\nbay_notify alerts.test \"$MSG\"")
    assert proc.returncode == 0, proc.stderr
    body = requests[0]["body"]
    assert requests[0]["content_type"] == "text/plain"
    assert "<b>" not in body and "<pre>" not in body
    assert "<config>" in body and "&&" in body


def test_empty_url_sends_nothing_and_logs_nothing(sink, tmp_path):
    """Feature off means the webhook path is inert, not merely unreachable.

    Asserting "no request arrived at the sink" is not enough on its own: an
    empty URL cannot reach the sink either way, so that assertion passes even
    if the guard is deleted. The observable difference is the failure log —
    without the guard, curl fails on the empty URL and every single alert
    records a bogus delivery failure.
    """
    _, requests = sink
    log = tmp_path / "telegram-failures.log"
    snippet = _render(alert_webhook_url="")
    proc = _run(
        snippet,
        f"BAY_ALERT_FAILURE_LOG={str(log)!r}\nbay_notify alerts.test \"hello\"",
    )
    assert proc.returncode == 0, proc.stderr
    assert requests == [], "the webhook fired despite no URL being configured"
    assert not log.exists(), (
        "an alert with no webhook configured recorded a delivery failure; "
        "the empty-URL guard in _bay_send_webhook is missing"
    )


def test_header_and_footer_are_applied(sink):
    url, requests = sink
    snippet = _render(alert_webhook_url=url, alert_webhook_format="raw")
    proc = _run(
        snippet,
        'BAY_ALERT_HEADER="HEAD::"\nBAY_ALERT_FOOTER="::FOOT"\nbay_notify alerts.test "body"',
    )
    assert proc.returncode == 0, proc.stderr
    assert requests[0]["body"] == "HEAD::body::FOOT"


def test_oversized_message_is_clipped_with_a_marker(sink):
    url, requests = sink
    snippet = _render(
        alert_webhook_url=url, alert_webhook_format="raw", alert_webhook_max_chars=40
    )
    proc = _run(snippet, 'bay_notify alerts.test "$(printf \'%0.sA\' {1..200})"')
    assert proc.returncode == 0, proc.stderr
    body = requests[0]["body"]
    assert "[truncated]" in body
    assert body.count("A") == 40


def test_clip_does_not_leave_a_dangling_tag_fragment(sink):
    """Cutting mid-tag would emit broken markup to an HTML sink."""
    url, requests = sink
    snippet = _render(
        alert_webhook_url=url, alert_webhook_format="campfire",
        alert_webhook_max_chars=12,
    )
    proc = _run(snippet, 'bay_notify alerts.test "AAAAAAAAAA<code>x</code>"')
    assert proc.returncode == 0, proc.stderr
    body = requests[0]["body"]
    assert "<cod" not in body.replace("<code>", ""), f"dangling tag fragment: {body!r}"


def test_unreachable_sink_never_fails_the_caller():
    """A dead alert endpoint must not fail a deploy, a backup or a build."""
    snippet = _render(
        alert_webhook_url="http://127.0.0.1:1/hook", alert_webhook_timeout=2
    )
    proc = _run(snippet, 'bay_notify alerts.test "hello"\necho SURVIVED')
    assert proc.returncode == 0, proc.stderr
    assert "SURVIVED" in proc.stdout


def test_html_escape_round_trips_through_unescape(sink):
    """escape -> strip must return the original bytes for a non-HTML sink."""
    url, requests = sink
    snippet = _render(alert_webhook_url=url, alert_webhook_format="raw")
    proc = _run(
        snippet,
        'UNTRUSTED=\'a<b>&c&amp;d>e\'\nbay_notify alerts.test "$(bay_html_escape "$UNTRUSTED")"',
    )
    assert proc.returncode == 0, proc.stderr
    assert requests[0]["body"] == "a<b>&c&amp;d>e"


# ── Stage 2: Python + control-node emitters ──────────────────────────────

_BAY_ALERT = _ROLES / "alert_channel" / "files" / "bay_alert.py"
_DEPLOY_TASKS = _ROLES / "deploy_stack" / "tasks" / "main.yml"
_SEND_DEPLOY_ALERT = _ROLES / "deploy_stack" / "tasks" / "send_deploy_alert.yml"
_BUILD_SPECS = _ROLES / "container_lifecycle" / "tasks" / "build_specs.yml"


def _load_bay_alert():
    import importlib.util

    spec = importlib.util.spec_from_file_location("bay_alert_t", _BAY_ALERT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_bay_alert_module_contains_no_jinja_delimiters():
    """It is pulled verbatim into docker-monitor.py.j2 — a delimiter breaks the render.

    Enforced rather than left as a comment: an innocent f-string with a literal
    brace would turn a monitoring script into a template syntax error at deploy.
    """
    content = _BAY_ALERT.read_text()
    for delim in ("{{", "{%", "{#"):
        assert delim not in content, (
            f"bay_alert.py contains the Jinja delimiter {delim!r}; it is "
            f"included verbatim into docker-monitor.py.j2 and would break it"
        )


@pytest.mark.parametrize(
    "link",
    [
        "docker_monitor/templates/bay_alert.py",
        "git_deploy/files/webhook/bay_alert.py",
    ],
)
def test_python_adapters_are_shared_by_symlink(link):
    path = _ROLES / link
    assert path.is_symlink(), f"{link} must be a symlink to the canonical module"
    assert path.resolve() == _BAY_ALERT.resolve()
    out = subprocess.run(
        ["git", "-C", str(_REPO_ROOT), "ls-files", "-s", f"roles/{link}"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert out.startswith("120000"), f"roles/{link} is not committed as a symlink"


def test_docker_monitor_renders_to_valid_python():
    """The include must produce a script Python can actually parse."""
    env = make_ansible_env(_ROLES / "docker_monitor" / "templates")
    env.filters["to_json"] = json.dumps
    env.filters["bool"] = bool
    env.filters["string"] = str
    rendered = env.get_template("docker-monitor.py.j2").render(
        ansible_managed="test",
        docker_monitor_telegram_bot_token="",
        docker_monitor_telegram_chat_id="",
        inventory_hostname="h1",
        docker_monitor_alert_container_crash=True,
        docker_monitor_alert_restart_loop=True,
        docker_monitor_alert_healthcheck_failure=True,
        docker_monitor_restart_loop_threshold=3,
        docker_monitor_restart_loop_window=300,
        docker_monitor_ignore_containers=["-new"],
        docker_monitor_alert_header="H",
        docker_monitor_alert_footer="F",
        alert_webhook_url="https://example.invalid/hook",
    )
    assert "def bay_send_webhook(" in rendered, "the shared adapters were not included"
    compile(rendered, "docker-monitor.py", "exec")


def test_webhook_app_imports_the_shared_adapters():
    content = (_ROLES / "git_deploy" / "files" / "webhook" / "app.py").read_text()
    assert "from bay_alert import bay_send_webhook" in content
    dockerfile = (
        _ROLES / "git_deploy" / "files" / "webhook" / "Dockerfile"
    ).read_text()
    assert "COPY bay_alert.py ." in dockerfile, (
        "the webhook image would not contain bay_alert.py — app.py fails to import"
    )


def test_deploy_stack_sends_both_deploy_alerts_to_the_webhook():
    """Deploy complete/failed are the most visible alerts; the issue omits them.

    Delivery moved out of main.yml's two hard-coded `uri` tasks and into
    send_deploy_alert.yml (routed by recipient like every other alert), so the
    success and rescue blocks now each just include that file once. The
    best-effort guarantee is checked on the actual delivery tasks there, not
    on the include_tasks call.
    """
    content = _DEPLOY_TASKS.read_text()
    assert content.count("Deliver deploy alert to routed recipients") == 2, (
        "expected one include for the success path and one for the rescue path"
    )
    assert content.count("include_tasks: send_deploy_alert.yml") == 2
    tasks = yaml.safe_load(content)

    def _find(name, node):
        if isinstance(node, dict):
            if node.get("name") == name:
                return node
            for value in node.values():
                found = _find(name, value)
                if found:
                    return found
        elif isinstance(node, list):
            for item in node:
                found = _find(name, item)
                if found:
                    return found
        return None

    deliver = _find("Deliver deploy alert to routed recipients", tasks)
    assert deliver is not None, "task 'Deliver deploy alert to routed recipients' not found"

    send_tasks = yaml.safe_load(_SEND_DEPLOY_ALERT.read_text())
    _gate_needle = {
        # Recipients are routed per-adapter now, not gated on the single
        # legacy alert_webhook_url — each task must check its own config key.
        "Send deploy alert to Telegram recipients": "item.config.bot_token",
        "Send deploy alert to webhook recipients": "item.config.url",
    }
    for name, needle in _gate_needle.items():
        task = _find(name, send_tasks)
        assert task is not None, f"task {name!r} not found in send_deploy_alert.yml"
        assert task.get("ignore_errors") is True, (
            f"{name} must not be able to fail a deploy"
        )
        assert any(needle in cond for cond in task["when"]), (
            f"{name} must be gated on its recipient config being set"
        )
        assert any("bay_alert_ids_for" in cond for cond in task["when"]), (
            f"{name} must be gated on registry routing (enabled_by_default / "
            f"min_level / alerts_disabled / alerts_enabled), not just presence"
        )


def test_webhook_receiver_env_omits_alert_keys_when_unset():
    """Default-off must not change the container spec — see the reconciler trap.

    Adding `ALERT_WEBHOOK_URL: ""` would change config_hash for every consumer
    and recreate the webhook receiver on their next deploy, opted in or not.
    """
    def _find_key(key, node):
        """Return the value bound to `key` anywhere in the parsed YAML."""
        if isinstance(node, dict):
            for name, value in node.items():
                if name == key:
                    return value
                found = _find_key(key, value)
                if found is not None:
                    return found
        elif isinstance(node, list):
            for item in node:
                found = _find_key(key, item)
                if found is not None:
                    return found
        return None

    tasks = yaml.safe_load(_BUILD_SPECS.read_text())
    # Assert per key. Checking a blob that contains several env entries lets
    # one key's `else omit` mask another key that lost it.
    for key in ("ALERT_WEBHOOK_URL", "ALERT_WEBHOOK_FORMAT"):
        expr = _find_key(key, tasks)
        assert expr is not None, f"{key} not found in build_specs.yml"
        assert "else omit" in expr, (
            f"{key} must use the `else omit` pattern, not | default(''), or "
            f"every consumer's webhook receiver is recreated on the next deploy. "
            f"Found: {expr}"
        )


# ── The two implementations must not drift ───────────────────────────────

_AGREEMENT_CASES = [
    "plain text",
    "<b>bold</b> and <code>code</code>",
    "<pre>a &amp; b</pre>",
    "unicode ✅ ▸ ⛵",
    "quote \" and ' apostrophe",
    "multi\nline\ntext",
]


@pytest.mark.parametrize("fmt", ["campfire", "slack", "raw"])
@pytest.mark.parametrize("message", _AGREEMENT_CASES)
def test_bash_and_python_adapters_agree(sink, fmt, message):
    """The shell snippet and the Python module must render identically.

    They are separate implementations of one contract (bash cannot import
    Python, and the webhook container cannot source bash). Divergence would
    mean the same alert looks different depending on which subsystem sent it,
    which is exactly the drift this pins down.
    """
    url, requests = sink
    snippet = _render(alert_webhook_url=url, alert_webhook_format=fmt)
    proc = _run(snippet, 'bay_notify alerts.test "$BAY_TEST_MSG"', {"BAY_TEST_MSG": message})
    assert proc.returncode == 0, proc.stderr

    shell_body = requests[0]["body"]
    python_body = _load_bay_alert().bay_alert_body(message, fmt)
    if fmt == "slack":
        # jq and json.dumps both emit valid JSON but format it differently
        # (jq pretty-prints and adds a trailing newline). The contract is the
        # decoded payload, not the byte layout.
        assert json.loads(shell_body) == json.loads(python_body), (
            f"bash and Python disagree for format={fmt} on {message!r}"
        )
    else:
        assert shell_body == python_body, (
            f"bash and Python disagree for format={fmt} on {message!r}"
        )


@pytest.mark.parametrize(
    "raw", ["a<b>&c", "plain", "& & &", "<<>>", "already &amp; escaped"]
)
def test_bash_and_python_escape_identically(sink, raw):
    url, requests = sink
    snippet = _render(alert_webhook_url=url, alert_webhook_format="campfire")
    proc = _run(
        snippet,
        'bay_notify alerts.test "$(bay_html_escape "$BAY_TEST_RAW")"',
        {"BAY_TEST_RAW": raw},
    )
    assert proc.returncode == 0, proc.stderr
    assert requests[0]["body"] == _load_bay_alert().bay_html_escape(raw)


# ── Recipient routing (M106-S3) ──────────────────────────────────────────
#
# Routing is resolved at render time: bay_alert_ids_for turns (registry,
# min_level) into a literal ID list baked into a bash `case`. These tests run
# the RENDERED bash against a real HTTP sink, so they check what a host would
# actually do rather than what the resolver believes.


def _render_with(recipients, **ctx):
    base = {
        "docker_monitor_telegram_bot_token": "",
        "docker_monitor_telegram_chat_id": "",
        "alert_recipients": recipients,
    }
    base.update(ctx)
    return make_ansible_env(_CANONICAL.parent).get_template("_notify.sh.j2").render(**base)


def test_recipient_receives_an_alert_at_its_min_level(sink):
    url, requests = sink
    snippet = _render_with(
        [{"name": "ops", "adapter": "webhook", "min_level": "warn",
          "config": {"url": url, "format": "raw"}}]
    )
    proc = _run(snippet, 'bay_notify build.failed "<b>x</b>"')
    assert proc.returncode == 0, proc.stderr
    assert len(requests) == 1, "build.failed is warn and must reach a warn recipient"


def test_recipient_does_not_receive_an_alert_below_its_min_level(sink):
    """The whole point of the feature: a critical-only sink stays quiet."""
    url, requests = sink
    snippet = _render_with(
        [{"name": "oncall", "adapter": "webhook", "min_level": "critical",
          "config": {"url": url, "format": "raw"}}]
    )
    proc = _run(snippet, 'bay_notify alerts.test "<b>prune</b>"')
    assert proc.returncode == 0, proc.stderr
    assert requests == [], (
        "alerts.test is info; a critical-only recipient must not get it"
    )


def test_critical_alert_reaches_a_critical_only_recipient(sink):
    url, requests = sink
    snippet = _render_with(
        [{"name": "oncall", "adapter": "webhook", "min_level": "critical",
          "config": {"url": url, "format": "raw"}}]
    )
    proc = _run(snippet, 'bay_notify deploy.failed "<b>boom</b>"')
    assert proc.returncode == 0, proc.stderr
    assert len(requests) == 1


def test_globally_disabled_alert_reaches_nobody(sink):
    """alerts_disabled must override even an alert that is on by default —
    log.retention_prune is already off by default, which would make this
    vacuous, so use alerts.test (enabled_by_default: true) instead."""
    url, requests = sink
    snippet = _render_with(
        [{"name": "ops", "adapter": "webhook", "min_level": "debug",
          "config": {"url": url, "format": "raw"}}],
        alerts_disabled=["alerts.test"],
    )
    proc = _run(snippet, 'bay_notify alerts.test "<b>prune</b>"')
    assert proc.returncode == 0, proc.stderr
    assert requests == []


def test_two_recipients_get_independently_filtered_streams(sink):
    """One deploy, two sinks, different thresholds — the routing table is
    per-recipient, not global."""
    url, requests = sink
    snippet = _render_with(
        [
            {"name": "chat", "adapter": "webhook", "min_level": "info",
             "config": {"url": url + "?r=chat", "format": "raw"}},
            {"name": "oncall", "adapter": "webhook", "min_level": "critical",
             "config": {"url": url + "?r=oncall", "format": "raw"}},
        ]
    )
    proc = _run(
        snippet,
        'bay_notify alerts.test "<b>ok</b>"\nbay_notify deploy.failed "<b>bad</b>"',
    )
    assert proc.returncode == 0, proc.stderr
    # chat (info) sees both; oncall (critical) sees only deploy.failed.
    assert len(requests) == 3, [r["body"] for r in requests]


def test_declarative_headers_and_content_type_reach_the_sink(sink):
    """Adding an HTTP recipient type must need no framework code."""
    url, requests = sink
    snippet = _render_with(
        [{"name": "custom", "adapter": "webhook", "min_level": "debug",
          "config": {"url": url, "content_type": "application/x-custom",
                     "transform": "text", "headers": {"X-Bay-Token": "abc123"}}}]
    )
    proc = _run(snippet, 'bay_notify alerts.test "<b>hi</b>"')
    assert proc.returncode == 0, proc.stderr
    assert len(requests) == 1
    assert requests[0]["content_type"] == "application/x-custom"


def test_a_dead_recipient_never_fails_the_caller():
    """Fail-open is the guarantee the whole channel rests on.

    Uses alerts.test (enabled_by_default: true) rather than deploy.complete —
    deploy.complete is off by default now, so bay_notify would skip the
    recipient entirely and never actually attempt the dead connection.
    """
    snippet = _render_with(
        [{"name": "dead", "adapter": "webhook", "min_level": "debug",
          "config": {"url": "http://127.0.0.1:1/nope", "format": "raw"}}]
    )
    proc = _run(snippet, 'bay_notify alerts.test "<b>hi</b>"\necho SURVIVED')
    assert proc.returncode == 0, proc.stderr
    assert "SURVIVED" in proc.stdout


def test_url_env_indirection_supplies_the_target_at_runtime(sink):
    """log_archive's systemd unit supplies credentials from the environment;
    a render-time recipient list must be able to express that."""
    url, requests = sink
    snippet = _render_with(
        [{"name": "envhook", "adapter": "webhook", "min_level": "debug",
          "config": {"url_env": "BAY_TEST_HOOK", "format": "raw"}}]
    )
    proc = _run(snippet, 'bay_notify alerts.test "<b>hi</b>"',
                {"BAY_TEST_HOOK": url})
    assert proc.returncode == 0, proc.stderr
    assert len(requests) == 1


def test_no_recipients_renders_identically_to_the_legacy_snippet():
    """Adoption is additive: a consumer who never configures a recipient must
    get byte-identical output, so their config_hash does not churn."""
    omitted = _render()
    empty = _render_with([])
    assert omitted == empty

# ── Routing correctness against hand-written expectations (M106-S3) ──────
#
# NOT a bash-vs-Python parity test. Under the precompute design (D1) the bash
# side holds no routing logic at all — Jinja bakes a literal ID list into a
# `case`. Comparing that against the resolver that generated it is comparing a
# function to itself: an injected resolver bug moves both sides together and
# the test stays green. It was written that way first and caught nothing.
#
# These expectations are hand-written from the severity ladder instead, so a
# resolver bug has something independent to contradict.

_MUST_DELIVER = [
    ("deploy.failed", "critical"),      # critical alert, critical floor
    ("deploy.failed", "info"),          # critical alert clears a lower floor
    ("build.failed", "warn"),           # warn alert, warn floor
    ("alerts.test", "info"),            # info alert, info floor
    ("alerts.test", "debug"),           # info alert clears the debug floor
]

_MUST_NOT_DELIVER = [
    ("alerts.test", "warn"),            # info alert, warn floor
    ("alerts.test", "critical"),
    ("build.failed", "critical"),       # warn alert, critical floor
]


@pytest.mark.parametrize("alert_id,min_level", _MUST_DELIVER)
def test_alert_at_or_above_the_floor_is_delivered(sink, alert_id, min_level):
    url, requests = sink
    snippet = _render_with([{"name": "r", "adapter": "webhook",
                             "min_level": min_level,
                             "config": {"url": url, "format": "raw"}}])
    proc = _run(snippet, f'bay_notify {alert_id} "<b>m</b>"')
    assert proc.returncode == 0, proc.stderr
    assert len(requests) == 1, (
        f"{alert_id} should reach a min_level={min_level} recipient"
    )


@pytest.mark.parametrize("alert_id,min_level", _MUST_NOT_DELIVER)
def test_alert_below_the_floor_is_not_delivered(sink, alert_id, min_level):
    url, requests = sink
    snippet = _render_with([{"name": "r", "adapter": "webhook",
                             "min_level": min_level,
                             "config": {"url": url, "format": "raw"}}])
    proc = _run(snippet, f'bay_notify {alert_id} "<b>m</b>"')
    assert proc.returncode == 0, proc.stderr
    assert requests == [], (
        f"{alert_id} must NOT reach a min_level={min_level} recipient"
    )


# ── enabled_by_default / alerts_enabled (M106-S4) ─────────────────────────
#
# Info-tier success chatter is opt-in now: every info alert except
# alerts.test defaults to enabled_by_default: false. alerts_enabled is the
# new force-on list, symmetric with alerts_disabled, and it overrides BOTH
# enabled_by_default and min_level (see bay_recipient_alert_ids's docstring).


def test_default_off_info_alert_does_not_reach_an_info_floor_recipient(sink):
    """deploy.complete is enabled_by_default: false now. A recipient whose
    min_level is info would have cleared the severity floor under the old
    behaviour — it must still get nothing, because the floor is not the only
    gate any more."""
    url, requests = sink
    snippet = _render_with(
        [{"name": "chat", "adapter": "webhook", "min_level": "info",
          "config": {"url": url, "format": "raw"}}]
    )
    proc = _run(snippet, 'bay_notify deploy.complete "<b>ok</b>"')
    assert proc.returncode == 0, proc.stderr
    assert requests == [], (
        "deploy.complete is enabled_by_default: false — an info-floor "
        "recipient must not receive it without opting in"
    )


def test_alerts_enabled_forces_delivery_past_enabled_by_default_and_min_level(sink):
    """Listing an ID in alerts_enabled overrides BOTH gates at once: it
    reaches a recipient whose min_level is critical, for an alert that is
    also enabled_by_default: false."""
    url, requests = sink
    snippet = _render_with(
        [{"name": "oncall", "adapter": "webhook", "min_level": "critical",
          "config": {"url": url, "format": "raw"}}],
        alerts_enabled=["deploy.complete"],
    )
    proc = _run(snippet, 'bay_notify deploy.complete "<b>ok</b>"')
    assert proc.returncode == 0, proc.stderr
    assert len(requests) == 1, (
        "alerts_enabled must force delivery past both enabled_by_default and "
        "min_level"
    )


def test_alerts_disabled_wins_over_alerts_enabled_for_the_same_id(sink):
    """The two force lists are not just independent switches — disabled is
    the higher-precedence one when an ID appears in both."""
    url, requests = sink
    snippet = _render_with(
        [{"name": "chat", "adapter": "webhook", "min_level": "debug",
          "config": {"url": url, "format": "raw"}}],
        alerts_disabled=["deploy.complete"],
        alerts_enabled=["deploy.complete"],
    )
    proc = _run(snippet, 'bay_notify deploy.complete "<b>ok</b>"')
    assert proc.returncode == 0, proc.stderr
    assert requests == [], (
        "an ID in both alerts_disabled and alerts_enabled must not be "
        "delivered — disabled wins"
    )


# ── Host identity (M107) ─────────────────────────────────────────────────
#
# Every alert has to name the machine it came from, in a form an operator
# woken at 03:00 can act on. Bay shipped three different answers: most
# emitters printed `inventory_hostname` — a bare IP for any consumer whose
# inventory lists addresses — log_archive shelled out to `hostname -f`, and
# outbound_monitor printed `region`, which names a region and not a host.
#
# _host_label.j2 is now the single definition, symlinked the same way
# _notify.sh.j2 is. These tests exist so a new emitter cannot quietly invent
# a fourth answer.

_HOST_LABEL = _ROLES / "alert_channel" / "templates" / "_host_label.j2"

# Every role whose templates name a host in an alert.
_HOST_LABEL_ROLES = _CONSUMING_ROLES + ["docker_monitor", "deploy_stack"]


def test_host_label_snippet_exists():
    assert _HOST_LABEL.is_file()


@pytest.mark.parametrize("role", _HOST_LABEL_ROLES)
def test_host_label_is_symlinked_into_each_role(role):
    link = _ROLES / role / "templates" / "_host_label.j2"
    assert link.is_symlink(), f"{role} needs a _host_label.j2 symlink"
    assert link.resolve() == _HOST_LABEL.resolve()


def _label(**ctx) -> str:
    env = make_ansible_env(_HOST_LABEL.parent)
    return env.get_template("_host_label.j2").render(**ctx)


def test_explicit_label_wins_and_keeps_the_address():
    """The label says WHICH box; the address is how you reach it."""
    assert _label(
        bay_host_label="infra.bay.example.com",
        ansible_hostname="example-infra",
        inventory_hostname="203.0.113.42",
    ) == "infra.bay.example.com (203.0.113.42)"


def test_unlabelled_host_falls_back_to_its_own_hostname():
    """Adoption must be safe on a host nobody has labelled yet."""
    assert (
        _label(ansible_hostname="example-infra", inventory_hostname="203.0.113.42")
        == "example-infra (203.0.113.42)"
    )


def test_last_resort_is_the_inventory_address():
    """Never empty. A worse name beats an anonymous alert."""
    assert _label(inventory_hostname="203.0.113.42") == "203.0.113.42"


def test_empty_label_is_treated_as_unset():
    """`bay_host_label: ""` is the documented default, not a blank host name."""
    assert (
        _label(
            bay_host_label="",
            ansible_hostname="",
            inventory_hostname="203.0.113.42",
        )
        == "203.0.113.42"
    )


def test_label_equal_to_address_is_not_printed_twice():
    """A consumer that configures nothing must see no change in its alerts."""
    assert _label(ansible_hostname="bay-na", inventory_hostname="bay-na") == "bay-na"


def test_label_renders_inline_with_no_stray_whitespace():
    """It is interpolated into a shell string and a compose env value."""
    out = _label(bay_host_label="infra", inventory_hostname="10.0.0.1")
    assert out == out.strip()
    assert "\n" not in out


def test_snippet_exports_bay_host():
    out = _render(bay_host_label="infra", inventory_hostname="10.0.0.1")
    assert "BAY_HOST='infra (10.0.0.1)'" in out


@pytest.mark.parametrize("script", _CONVERTED_SCRIPTS)
def test_no_emitter_names_the_host_its_own_way(script):
    """The whole point: one definition, not one per script.

    A shell emitter must reach for ${BAY_HOST}. Referencing inventory_hostname
    or region directly is how the bare-IP alerts happened in the first place.
    """
    src = (_ROLES / script).read_text()
    # Strip Jinja comments — they discuss the old behaviour by name.
    body = re.sub(r"\{#.*?#\}", "", src, flags=re.DOTALL)
    offenders = [
        line.strip()
        for line in body.splitlines()
        if ("{{ inventory_hostname }}" in line or "{{ region " in line)
        # restic --tag keys the backup history; it is data, not a display name.
        and "--tag" not in line
    ]
    assert not offenders, f"{script} names the host directly: {offenders}"


def test_ansible_var_twin_matches_the_template():
    """The two control-node deploy alerts are composed in a task var, where
    {% include %} is unavailable, so bay_alert_host restates the logic. The
    copies drifting apart is the failure mode this guards."""
    declared = yaml.safe_load(_DEFAULTS.read_text())
    expr = declared["bay_alert_host"]
    env = make_ansible_env(_HOST_LABEL.parent)
    cases = [
        {"bay_host_label": "infra.bay.example.com", "ansible_hostname": "x",
         "inventory_hostname": "203.0.113.42"},
        {"bay_host_label": "", "ansible_hostname": "example-infra",
         "inventory_hostname": "203.0.113.42"},
        {"bay_host_label": "", "ansible_hostname": "", "inventory_hostname": "1.2.3.4"},
        {"bay_host_label": "", "ansible_hostname": "bay-na",
         "inventory_hostname": "bay-na"},
    ]
    for ctx in cases:
        assert env.from_string(expr).render(**ctx) == env.get_template(
            "_host_label.j2"
        ).render(**ctx), ctx


# Roles that restate bay_alert_host because they do not depend on
# alert_channel. Same convention docker_monitor_alert_header already follows.
_VAR_TWIN_ROLES = ["deploy_stack", "container_lifecycle"]


@pytest.mark.parametrize("role", _VAR_TWIN_ROLES)
def test_var_twin_copies_are_identical(role):
    canonical = yaml.safe_load(_DEFAULTS.read_text())["bay_alert_host"]
    theirs = yaml.safe_load(
        (_ROLES / role / "defaults" / "main.yml").read_text()
    )["bay_alert_host"]
    assert theirs == canonical


def test_reconciler_spec_uses_the_shared_identity():
    """The reconciler builds container specs from build_specs.yml, not from the
    compose partials — editing a partial alone silently no-ops. The webhook
    receiver's HOSTNAME is set here, so this is the copy that actually reaches
    a running container."""
    src = (_ROLES / "container_lifecycle" / "tasks" / "build_specs.yml").read_text()
    assert 'HOSTNAME: "{{ bay_alert_host }}"' in src
    assert 'HOSTNAME: "{{ inventory_hostname }}"' not in src


def test_line_ending_includes_disable_block_trimming():
    """Ansible renders with trim_blocks=True, which eats the newline directly
    after a block tag. An `{% include %}` that ends its line therefore welds
    the next line onto it. That shipped once: the webhook receiver's compose
    env became `- HOSTNAME=infra (1.2.3.4)      - LOCAL_REGION=infra`, one YAML
    item, silently dropping LOCAL_REGION. `+%}` disables the trim.

    Mid-line includes (a quote follows the tag) are unaffected and need no `+`.
    """
    offenders = []
    for path in sorted(_REPO_ROOT.rglob("*.j2")):
        if "vendor" in path.parts or path.is_symlink():
            continue
        for n, line in enumerate(path.read_text().splitlines(), 1):
            if "_host_label.j2" not in line:
                continue
            stripped = line.rstrip()
            if stripped.endswith("%}") and not stripped.endswith("+%}"):
                rel = path.relative_to(_REPO_ROOT)
                offenders.append(f"{rel}:{n}: {stripped.strip()}")
    assert not offenders, (
        "include ends the line but does not disable block trimming: "
        + "; ".join(offenders)
    )
