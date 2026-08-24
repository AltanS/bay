"""Operator mute overrides: parsed not sourced, TTL'd, and failing open upward.

Three properties are load-bearing and each is asserted against the RENDERED
bash rather than the template text:

  * the file is never `source`d — it is root-owned config, and the scripts that
    read it run privileged
  * every failure path yields NO mutes, so absence can only produce MORE alerts
  * an expired mute is inert
"""

from __future__ import annotations

import http.server
import os
import subprocess
import threading
import time
from pathlib import Path

import pytest

from helpers import make_ansible_env

_REPO_ROOT = Path(__file__).resolve().parent.parent
_CANONICAL = _REPO_ROOT / "roles" / "alert_channel" / "templates" / "_notify.sh.j2"
_ROLE = _REPO_ROOT / "roles" / "alert_policy"


class _Recorder(http.server.BaseHTTPRequestHandler):
    requests: list = []

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        type(self).requests.append(self.rfile.read(length).decode())
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args):
        pass


@pytest.fixture
def sink():
    class Handler(_Recorder):
        requests: list = []

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    host, port = server.server_address
    yield f"http://{host}:{port}/hook", Handler.requests
    server.shutdown()
    server.server_close()


def _render(policy_path, url):
    return make_ansible_env(_CANONICAL.parent).get_template("_notify.sh.j2").render(
        docker_monitor_telegram_bot_token="",
        docker_monitor_telegram_chat_id="",
        alert_policy_path=str(policy_path),
        alert_recipients=[{
            "name": "r", "adapter": "webhook", "min_level": "debug",
            "config": {"url": url, "format": "raw"},
        }],
    )


def _fire(snippet, alert_id="alerts.test"):
    script = f'set -euo pipefail\n{snippet}\nbay_notify {alert_id} "<b>m</b>"\necho DONE'
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True)


def _policy(tmp_path, body):
    path = tmp_path / "alert-overrides"
    path.write_text(body)
    return path


_FUTURE = int(time.time()) + 3600
_PAST = int(time.time()) - 3600


# ── The mute works at all ────────────────────────────────────────────────


def test_active_mute_suppresses_the_named_alert(tmp_path, sink):
    url, requests = sink
    path = _policy(tmp_path, f"BAY_ALERTS_SCHEMA=1\nBAY_ALERTS_MUTE=alerts.test\nBAY_ALERTS_MUTE_UNTIL={_FUTURE}\n")
    proc = _fire(_render(path, url))
    assert proc.returncode == 0, proc.stderr
    assert requests == []


def test_an_unmuted_alert_still_fires(tmp_path, sink):
    url, requests = sink
    path = _policy(tmp_path, f"BAY_ALERTS_SCHEMA=1\nBAY_ALERTS_MUTE=log.retention_prune\nBAY_ALERTS_MUTE_UNTIL={_FUTURE}\n")
    proc = _fire(_render(path, url))
    assert proc.returncode == 0, proc.stderr
    assert len(requests) == 1


def test_expired_mute_is_inert(tmp_path, sink):
    """A mute with no working expiry is GH#33 with extra steps."""
    url, requests = sink
    path = _policy(tmp_path, f"BAY_ALERTS_SCHEMA=1\nBAY_ALERTS_MUTE=alerts.test\nBAY_ALERTS_MUTE_UNTIL={_PAST}\n")
    proc = _fire(_render(path, url))
    assert proc.returncode == 0, proc.stderr
    assert len(requests) == 1, "an expired mute must not keep suppressing"


# ── Fail-open, in the correct direction ──────────────────────────────────


@pytest.mark.parametrize(
    "body,label",
    [
        (None, "missing file"),
        ("", "empty file"),
        ("BAY_ALERTS_SCHEMA=1\nBAY_ALERTS_MUTE=deploy.comp", "truncated mid-write"),
        ("\x00\x01garbage\xff", "binary garbage"),
        (f"BAY_ALERTS_SCHEMA=99\nBAY_ALERTS_MUTE=alerts.test\nBAY_ALERTS_MUTE_UNTIL={_FUTURE}", "unknown schema major"),
        ("BAY_ALERTS_SCHEMA=1\nBAY_ALERTS_MUTE=alerts.test\nBAY_ALERTS_MUTE_UNTIL=notanumber", "malformed epoch"),
        ("BAY_ALERTS_SCHEMA=x\nBAY_ALERTS_MUTE=alerts.test\nBAY_ALERTS_MUTE_UNTIL=1", "malformed schema"),
    ],
)
def test_every_broken_policy_file_still_delivers(tmp_path, sink, body, label):
    """Absence or corruption may only produce MORE alerts, never fewer."""
    url, requests = sink
    if body is None:
        path = tmp_path / "does-not-exist"
    else:
        path = tmp_path / "alert-overrides"
        path.write_bytes(body.encode("utf-8", "surrogateescape"))
    proc = _fire(_render(path, url))
    assert proc.returncode == 0, proc.stderr
    assert len(requests) == 1, f"{label}: alert was lost — fail-open is inverted"


def test_unreadable_policy_file_still_delivers(tmp_path, sink):
    url, requests = sink
    path = _policy(tmp_path, f"BAY_ALERTS_SCHEMA=1\nBAY_ALERTS_MUTE=alerts.test\nBAY_ALERTS_MUTE_UNTIL={_FUTURE}\n")
    os.chmod(path, 0o000)
    try:
        proc = _fire(_render(path, url))
        assert proc.returncode == 0, proc.stderr
        # root ignores mode bits; skip rather than assert a false property.
        if os.geteuid() != 0:
            assert len(requests) == 1
    finally:
        os.chmod(path, 0o644)


# ── It is never sourced ──────────────────────────────────────────────────


def test_policy_file_is_never_sourced(tmp_path, sink):
    """A shell command in the file must not execute.

    The file is root-owned for exactly this reason; this proves the reader
    would not be an escalation path even if that ownership were wrong.
    """
    url, requests = sink
    marker = tmp_path / "PWNED"
    path = _policy(
        tmp_path,
        f"BAY_ALERTS_SCHEMA=1\n"
        f"BAY_ALERTS_MUTE=alerts.test\n"
        f"BAY_ALERTS_MUTE_UNTIL={_FUTURE}\n"
        f"touch {marker}\n",
    )
    proc = _fire(_render(path, url))
    assert proc.returncode == 0, proc.stderr
    assert not marker.exists(), "the override file was SOURCED — code execution"


def test_no_role_sources_the_override_file():
    hits = subprocess.run(
        ["grep", "-rnE", r"(^|[^a-z_])(\.|source) +[^ ]*alert-overrides",
         str(_REPO_ROOT / "roles")],
        capture_output=True, text=True,
    )
    assert hits.stdout == "", f"override file is sourced somewhere:\n{hits.stdout}"


# ── Role shape ───────────────────────────────────────────────────────────


def test_role_is_included_by_both_playbooks():
    """The actual GH#33 fix: outbound_monitor lives in provision.yml, so a
    deploy-only role would strand mutes for exactly the alerts that matter."""
    for playbook in ("provision.yml", "deploy.yml"):
        text = (_REPO_ROOT / playbook).read_text()
        assert "alert_policy" in text, f"{playbook} does not include alert_policy"


def test_rendered_file_carries_no_credentials():
    """Recipients stay baked; this file carries mute state only."""
    template = (_ROLE / "templates" / "alert-overrides.j2").read_text()
    for forbidden in ("token", "url", "chat_id", "password", "secret"):
        assert forbidden not in template.lower().replace("alert-overrides", ""), (
            f"policy template references {forbidden!r} — it must carry no credentials"
        )


def test_role_writes_the_file_root_owned_and_not_world_writable():
    tasks = (_ROLE / "tasks" / "main.yml").read_text()
    assert "owner: root" in tasks
    assert 'mode: "0644"' in tasks
    assert "0777" not in tasks


def test_every_task_escalates_to_root_explicitly():
    """`owner: root` is what we WANT; `become_user: root` is what lets us do it.

    deploy.yml's play runs with `become: true` + `become_user: app_user`, and a
    task-level `become: true` does NOT reset the user — it stays app_user, which
    cannot write /etc. The first release of this role asserted `owner: root` in
    the task text and shipped anyway: the deploy failed on the real host with
    'Permission denied: /etc/bay'. Asserting the intended file ownership says
    nothing about the privilege used to achieve it.
    """
    import re

    from ruamel.yaml import YAML

    tasks = YAML(typ="safe").load((_ROLE / "tasks" / "main.yml").open())
    assert tasks, "alert_policy has no tasks"
    for task in tasks:
        name = task.get("name", "<unnamed>")
        assert task.get("become") is True, f"{name}: missing become: true"
        assert task.get("become_user") == "root", (
            f"{name}: missing `become_user: root`. deploy.yml's play sets "
            f"become_user to app_user, so become alone is not enough to write /etc."
        )


def test_deploy_play_really_does_run_as_a_non_root_user():
    """Pins the assumption the test above rests on.

    If deploy.yml ever stops setting become_user, the guard above becomes
    cbay cult rather than a real constraint — this makes that visible.
    """
    text = (_REPO_ROOT / "deploy.yml").read_text()
    assert 'become_user: "{{ app_user }}"' in text, (
        "deploy.yml no longer runs as app_user — re-check whether alert_policy "
        "still needs an explicit become_user: root."
    )
