"""Alert credentials must not survive in anything rendered onto a host.

Every bay emitter used to carry the Telegram bot token and the alert webhook
URL as a literal: `_notify.sh.j2` baked them into eight shell scripts, three of
which were installed 0755, `docker-monitor.py` had the token as a bare Python
string literal at 0755, and `bay-logrotate@.service` put it in an inline
`Environment=` line that `systemctl show` prints to any local user.

They now live in exactly one place — /etc/bay/alert.env, 0600 root:root,
rendered by roles/alert_channel. systemd units point `EnvironmentFile=` at it
(systemd reads it as PID 1, before dropping to User=, so an unprivileged unit
still gets the values); everything else picks it up from the source line in
the shared snippet.

The sentinel used throughout is a string no template has any other reason to
emit, so a hit is unambiguous.
"""

from __future__ import annotations

import http.server
import os
import re
import subprocess
import threading
import sys
from pathlib import Path

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parent.parent
_ROLES = _REPO_ROOT / "roles"
_CANONICAL = _ROLES / "alert_channel" / "templates" / "_notify.sh.j2"
_ENV_PATH = "/etc/bay/alert.env"

_TESTS_DIR = Path(__file__).resolve().parent
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

from helpers import make_ansible_env  # noqa: E402

_SENTINEL_TOKEN = "SENTINEL-BOT-TOKEN-9f3a"
_SENTINEL_CHAT = "SENTINEL-CHAT-ID-9f3a"
_SENTINEL_URL = "https://sentinel.invalid/hook/9f3a"
_SENTINEL_RC_TOKEN = "SENTINEL-RECIPIENT-TOKEN-9f3a"
_SENTINEL_RC_URL = "https://sentinel.invalid/recipient/9f3a"

# Every role whose templates/ carries a link to the shared snippet.
_CONSUMING_ROLES = ["git_deploy", "outbound_monitor", "log_archive", "backup"]

_CREDENTIAL_VARS = dict(
    docker_monitor_telegram_bot_token=_SENTINEL_TOKEN,
    docker_monitor_telegram_chat_id=_SENTINEL_CHAT,
    alert_webhook_url=_SENTINEL_URL,
)


def _render_snippet(template_dir: Path, **ctx) -> str:
    env = make_ansible_env(template_dir)
    base = dict(_CREDENTIAL_VARS)
    base.update(ctx)
    return env.get_template("_notify.sh.j2").render(**base)


# ── The snippet itself ───────────────────────────────────────────────────


@pytest.mark.parametrize("role", _CONSUMING_ROLES)
def test_snippet_bakes_no_credential_through_any_symlink(role):
    """Passing the credentials as Ansible vars must change nothing on disk."""
    out = _render_snippet(_ROLES / role / "templates")
    for sentinel in (_SENTINEL_TOKEN, _SENTINEL_CHAT, _SENTINEL_URL):
        assert sentinel not in out, (
            f"{role}'s rendered alert snippet still contains {sentinel!r}. "
            f"Credentials belong in {_ENV_PATH}, never in a rendered script."
        )


def test_snippet_sources_the_root_owned_env_file():
    out = _render_snippet(_CANONICAL.parent)
    assert _ENV_PATH in out, "the snippet does not name the credential file"
    assert 'BAY_TG_TOKEN="${TELEGRAM_BOT_TOKEN:-}"' in out
    assert 'BAY_TG_CHAT="${TELEGRAM_CHAT_ID:-}"' in out
    assert 'BAY_ALERT_URL="${ALERT_WEBHOOK_URL:-}"' in out


def test_snippet_still_parses():
    out = _render_snippet(_CANONICAL.parent)
    proc = subprocess.run(
        ["bash", "-n"], input=out, capture_output=True, text=True, check=False
    )
    assert proc.returncode == 0, proc.stderr


def test_missing_env_file_is_survivable(tmp_path):
    """A host that has not been re-provisioned must not fail its backups.

    `set -e` plus the obvious `[[ -r f ]] && . f` spelling would exit the
    script when the file is absent, taking the backup or the build with it.
    """
    snippet = _render_snippet(
        _CANONICAL.parent, alert_env_path=str(tmp_path / "absent.env")
    )
    proc = subprocess.run(
        ["bash", "-c", "set -euo pipefail\n" + snippet + '\nbay_notify a.b "hi"\necho SURVIVED'],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert "SURVIVED" in proc.stdout


def test_env_file_supplies_the_credentials_at_run_time(tmp_path):
    """The cron path: no systemd, so the snippet has to read the file itself."""
    env_file = tmp_path / "alert.env"
    env_file.write_text(
        f"TELEGRAM_BOT_TOKEN='{_SENTINEL_TOKEN}'\n"
        f"TELEGRAM_CHAT_ID='{_SENTINEL_CHAT}'\n"
        f"ALERT_WEBHOOK_URL='{_SENTINEL_URL}'\n"
    )
    snippet = _render_snippet(_CANONICAL.parent, alert_env_path=str(env_file))
    body = (
        'printf "%s|%s|%s\\n" "${BAY_TG_TOKEN}" "${BAY_TG_CHAT}" "${BAY_ALERT_URL}"'
    )
    proc = subprocess.run(
        ["bash", "-c", "set -euo pipefail\n" + snippet + "\n" + body],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == f"{_SENTINEL_TOKEN}|{_SENTINEL_CHAT}|{_SENTINEL_URL}"


def test_systemd_environment_wins_without_the_file(tmp_path):
    """systemd's EnvironmentFile= has already exported them by then."""
    snippet = _render_snippet(
        _CANONICAL.parent, alert_env_path=str(tmp_path / "absent.env")
    )
    env = dict(os.environ)
    env.update(TELEGRAM_BOT_TOKEN=_SENTINEL_TOKEN, TELEGRAM_CHAT_ID=_SENTINEL_CHAT)
    proc = subprocess.run(
        ["bash", "-c", "set -euo pipefail\n" + snippet + '\nprintf "%s|%s\\n" "${BAY_TG_TOKEN}" "${BAY_TG_CHAT}"'],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == f"{_SENTINEL_TOKEN}|{_SENTINEL_CHAT}"


def test_a_quoted_value_cannot_become_shell_code(tmp_path):
    """The file is sourced, so its values must be single-quoted at render."""
    env_file = tmp_path / "alert.env"
    env_file.write_text("TELEGRAM_BOT_TOKEN='$(touch " + str(tmp_path / "pwned") + ")'\n")
    snippet = _render_snippet(_CANONICAL.parent, alert_env_path=str(env_file))
    subprocess.run(
        ["bash", "-c", "set -euo pipefail\n" + snippet],
        capture_output=True,
        text=True,
        check=False,
    )
    assert not (tmp_path / "pwned").exists(), (
        "a single-quoted value was expanded; alert.env.j2 must quote every value"
    )


# ── The role that renders the file ───────────────────────────────────────


def test_alert_channel_renders_the_file_root_only():
    tasks = yaml.safe_load((_ROLES / "alert_channel" / "tasks" / "main.yml").read_text())
    render = [t for t in tasks if t.get("ansible.builtin.template")]
    assert len(render) == 1, "expected exactly one template task in alert_channel"
    task = render[0]
    args = task["ansible.builtin.template"]
    assert args["dest"] == "{{ alert_env_path }}"
    assert args["owner"] == "root"
    assert args["mode"] == "0600", "the credential file must not be group-readable"
    assert task.get("become_user") == "root", (
        "deploy.yml runs this play as app_user; a task-level become: true does "
        "NOT reset the user, so become_user: root has to be explicit"
    )
    assert task.get("no_log") is True


def test_defaults_declare_the_path():
    declared = yaml.safe_load(
        (_ROLES / "alert_channel" / "defaults" / "main.yml").read_text()
    )
    assert declared["alert_env_path"] == _ENV_PATH


@pytest.mark.parametrize("playbook", ["deploy.yml", "provision.yml"])
def test_role_runs_from_both_playbooks(playbook):
    """outbound_monitor is provision-only and docker_monitor deploy-adjacent.

    A file only one playbook renders is GH#33 waiting to happen.
    """
    plays = yaml.safe_load((_REPO_ROOT / playbook).read_text())
    names = [
        r["role"] if isinstance(r, dict) else r
        for play in plays
        for r in (play.get("roles") or [])
    ]
    assert "alert_channel" in names, f"{playbook} never runs roles/alert_channel"


@pytest.mark.parametrize("playbook", ["deploy.yml", "provision.yml"])
def test_the_role_is_tagged_always(playbook):
    """A tag-scoped run re-renders the units, so it must render the file.

    The units say `EnvironmentFile=-{path}`, and the leading `-` makes a
    missing file SILENT. `--tags deploy_stack` or `--tags build` used to skip
    alert_channel while still rewriting the units, which produced emitters
    with no credentials and no error anywhere.
    """
    plays = yaml.safe_load((_REPO_ROOT / playbook).read_text())
    entry = next(
        r
        for play in plays
        for r in (play.get("roles") or [])
        if isinstance(r, dict) and r.get("role") == "alert_channel"
    )
    assert "always" in entry["tags"], (
        f"{playbook}: alert_channel must run whatever the tag selection is"
    )


# ── The emitters ─────────────────────────────────────────────────────────


def test_docker_monitor_reads_the_token_from_the_environment():
    src = (_ROLES / "docker_monitor" / "templates" / "docker-monitor.py.j2").read_text()
    assert 'TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")' in src
    assert 'TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")' in src
    assert 'ALERT_WEBHOOK_URL = os.environ.get("ALERT_WEBHOOK_URL", "")' in src
    assert "docker_monitor_telegram_bot_token" not in src


def test_docker_monitor_unit_supplies_the_environment():
    unit = (_ROLES / "docker_monitor" / "templates" / "docker-monitor.service.j2").read_text()
    assert "EnvironmentFile=-" in unit and "alert_env_path" in unit


def test_logrotate_unit_has_no_inline_credentials():
    unit = (_ROLES / "log_archive" / "templates" / "bay-logrotate@.service.j2").read_text()
    assert "Environment=TELEGRAM_BOT_TOKEN" not in unit, (
        "`systemctl show` prints Environment= to any local user"
    )
    assert "Environment=TELEGRAM_CHAT_ID" not in unit
    assert "EnvironmentFile=-" in unit


_UNITS_NEEDING_THE_FILE = [
    "git_deploy/templates/bay-build@.service.j2",
    "git_deploy/templates/bay-build-alert@.service.j2",
    "git_deploy/templates/bay-trigger-watchdog.service.j2",
    "backup/templates/bay-backup@.service.j2",
    "backup/templates/bay-backup-maintenance@.service.j2",
    "outbound_monitor/templates/bay-outbound-check.service.j2",
    "outbound_monitor/templates/bay-disk-alert.service.j2",
    "log_archive/templates/bay-logrotate@.service.j2",
    "docker_monitor/templates/docker-monitor.service.j2",
]


@pytest.mark.parametrize("unit", _UNITS_NEEDING_THE_FILE)
def test_every_alerting_unit_points_at_the_env_file(unit):
    """A unit running as app_user cannot read a 0600 root file itself.

    systemd can — it reads EnvironmentFile= as PID 1, before it drops
    privileges. Without the line, those emitters go silent.
    """
    src = (_ROLES / unit).read_text()
    assert "EnvironmentFile=-{{ alert_env_path" in src, (
        f"{unit} runs an emitter but never loads the alert credentials"
    )


# The scripts that leaked the token for the whole life of the framework, and
# the task file that installs each. Scoped to the SCRIPTS: the config
# directories in these roles stay 0755 on purpose — grafana bind-mounts them
# and runs as uid 472, so a 0750 root:root directory breaks it.
_EMITTER_SCRIPTS = [
    ("outbound_monitor/tasks/main.yml", "/usr/local/bin/bay-outbound-check"),
    ("outbound_monitor/tasks/main.yml", "/usr/local/bin/bay-disk-alert"),
    ("log_archive/tasks/main.yml", "{{ log_archive_bin_dir }}/rotate-logs.sh"),
    ("docker_monitor/tasks/main.yml", "{{ docker_monitor_script_path }}"),
]


def _flatten(tasks):
    """Tasks in this repo live inside `block:` as often as at the top level."""
    for task in tasks:
        yield task
        for key in ("block", "rescue", "always"):
            if key in task:
                yield from _flatten(task[key])


@pytest.mark.parametrize("task_file,dest", _EMITTER_SCRIPTS)
def test_no_emitter_is_installed_world_readable(task_file, dest):
    tasks = yaml.safe_load((_ROLES / task_file).read_text())
    task = next(
        t
        for t in _flatten(tasks)
        if t.get("ansible.builtin.template", {}).get("dest") == dest
    )
    assert task["ansible.builtin.template"]["mode"] == "0750", (
        f"{dest} is world-readable; it is one of the scripts the alert "
        f"credentials used to be baked into"
    )


# ── Explicit recipients ──────────────────────────────────────────────────
#
# alert_recipients entries may carry a literal `bot_token` or `url`. Those are
# credentials too, and they were rendered straight into every emitter script
# alongside the legacy pair. They now come from the same root-owned file, keyed
# by the recipient's 1-based position.

_RECIPIENTS = [
    {
        "name": "literal-telegram",
        "adapter": "telegram",
        "min_level": "warn",
        "config": {"bot_token": _SENTINEL_RC_TOKEN, "chat_id": "12345"},
    },
    {
        "name": "literal-webhook",
        "adapter": "webhook",
        "min_level": "warn",
        "config": {"url": _SENTINEL_RC_URL, "format": "raw"},
    },
    {
        "name": "env-webhook",
        "adapter": "webhook",
        "min_level": "warn",
        "config": {"url_env": "BAY_TEST_HOOK_URL"},
    },
]


@pytest.mark.parametrize("role", _CONSUMING_ROLES)
def test_recipient_credentials_are_not_baked_into_any_script(role):
    out = _render_snippet(_ROLES / role / "templates", alert_recipients=_RECIPIENTS)
    for sentinel in (_SENTINEL_RC_TOKEN, _SENTINEL_RC_URL):
        assert sentinel not in out, (
            f"{role}'s rendered snippet still contains the recipient credential "
            f"{sentinel!r}; it belongs in {_ENV_PATH}"
        )


def test_recipient_credentials_are_read_by_index_at_run_time():
    out = _render_snippet(_CANONICAL.parent, alert_recipients=_RECIPIENTS)
    assert 'local token="${BAY_RC_1_TOKEN:-}"' in out
    assert 'local url="${BAY_RC_2_URL:-}"' in out
    # token_env / url_env keep working: they were already run-time lookups.
    # Since the M109 config-to-shell hardening the NAME is validated at render
    # time and then dereferenced indirectly, instead of being pasted into the
    # parameter expansion (where a name of `X:-$(id)` executed at every alert).
    assert "local _url_name='BAY_TEST_HOOK_URL'" in out
    assert 'local url="${!_url_name:-}"' in out


def _render_env_file(**ctx) -> str:
    env = make_ansible_env(_CANONICAL.parent)
    base = dict(ansible_managed="test", alert_env_path=_ENV_PATH)
    base.update(ctx)
    return env.get_template("alert.env.j2").render(**base)


def test_recipient_credentials_land_in_the_env_file():
    out = _render_env_file(alert_recipients=_RECIPIENTS)
    assert f"BAY_RC_1_TOKEN='{_SENTINEL_RC_TOKEN}'" in out
    assert f"BAY_RC_2_URL='{_SENTINEL_RC_URL}'" in out
    # A recipient that names an env var has nothing to write here.
    assert "BAY_RC_3_" not in out


@pytest.mark.parametrize(
    "bad",
    ["tok'en", "tok\nen", "tok\ren"],
    ids=["single_quote", "newline", "carriage_return"],
)
@pytest.mark.parametrize("field", ["global", "recipient"])
def test_a_value_that_cannot_be_quoted_fails_the_render(bad, field):
    """/etc/bay/alert.env is KEY='VALUE', sourced by root cron under `set -e`.

    There is no escape for a literal `'` inside a single-quoted shell word,
    and both readers are line-based, so a CR or LF starts a second
    assignment. None of the three can be made safe, so the render refuses
    them rather than shipping a file that runs the tail of a token as root.
    """
    if field == "global":
        ctx = dict(docker_monitor_telegram_bot_token=bad)
    else:
        ctx = dict(
            alert_recipients=[
                {
                    "name": "ops",
                    "adapter": "telegram",
                    "min_level": "info",
                    "config": {"bot_token": bad, "chat_id": "123"},
                }
            ]
        )
    with pytest.raises(ValueError) as exc:
        _render_env_file(**ctx)
    message = str(exc.value).lower()
    assert "alert credential" in message
    assert "rotate" in message, "the operator needs to be told what to do"
    assert bad not in str(exc.value), "the failure must not print the credential"


def test_a_well_behaved_credential_still_renders():
    out = _render_env_file(alert_recipients=_RECIPIENTS, **_CREDENTIAL_VARS)
    assert f"TELEGRAM_BOT_TOKEN='{_SENTINEL_TOKEN}'" in out
    assert f"ALERT_WEBHOOK_URL='{_SENTINEL_URL}'" in out


def test_recipient_indices_agree_between_the_two_templates():
    """A mismatch is silent: the alert just never sends.

    _notify.sh.j2 and alert.env.j2 iterate the same list independently, so the
    only thing keeping the indices aligned is that they both use loop.index.
    """
    script = _render_snippet(_CANONICAL.parent, alert_recipients=_RECIPIENTS)
    env_file = _render_env_file(alert_recipients=_RECIPIENTS)
    for name in re.findall(r"BAY_RC_\d+_(?:TOKEN|URL)", script):
        assert f"{name}=" in env_file, (
            f"{name} is read by the snippet but never written to {_ENV_PATH}"
        )


def test_recipient_delivers_with_the_credential_from_the_env_file(tmp_path, sink):
    """End to end on the cron path: file on disk, message out to a real sink."""
    url, requests = sink
    recipients = [
        {
            "name": "literal-webhook",
            "adapter": "webhook",
            "min_level": "debug",
            "config": {"url": url, "format": "raw"},
        }
    ]
    env_file = tmp_path / "alert.env"
    env_file.write_text(f"BAY_RC_1_URL='{url}'\n")
    snippet = _render_snippet(
        _CANONICAL.parent,
        alert_recipients=recipients,
        alert_env_path=str(env_file),
    )
    proc = subprocess.run(
        ["bash", "-c", "set -euo pipefail\n" + snippet + '\nbay_notify alerts.test "hello"'],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert len(requests) == 1, "the recipient never fired; the index or the key is wrong"
    assert requests[0]["body"] == "hello"


class _Recorder(http.server.BaseHTTPRequestHandler):
    requests: list[dict] = []

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        type(self).requests.append({"body": self.rfile.read(length).decode()})
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):  # silence
        pass


@pytest.fixture
def sink():
    class Handler(_Recorder):
        requests: list[dict] = []

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    yield f"http://{host}:{port}/hook", Handler.requests
    server.shutdown()
    server.server_close()
