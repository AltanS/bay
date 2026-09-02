"""Every shell template Bay renders, fed hostile config, must stay inert.

The v0.2.4 audit (S4/S8/S16) found the same bug in nine places at once: a
consumer-supplied value — a service key, an accessory name, a vault secret, an
alert recipient's `token_env` — interpolated into a shell word or a SQL
statement with no quoting. Several of the scripts involved run from root cron
or a root systemd unit, so the blast radius was root, and none of the 1500-odd
tests in this repo rendered a template with anything other than a well-behaved
name.

This file is that missing test. It renders each template with a battery of
payloads and asserts that wherever a payload survives into the output, the
shell context around it makes it inert.

  * The table is authoritative. `test_every_shell_template_is_covered`
    enumerates `roles/**/*.sh.j2` and fails on anything that is neither in
    `_CASES` nor in `_NO_CONSUMER_INPUT`, so a new template cannot land
    unexamined.
  * A render that RAISES counts as a pass. `bay_env_name`, `bay_env_value`
    and `_rule_literal` refuse values they cannot make safe; refusing at
    render time is the strongest possible outcome.
  * `_DOCUMENTED_RESIDUALS` is the escape hatch, and it is asserted to be
    exactly what is written here. Adding to it is a deliberate act with a
    reviewer.

The shell rules encoded in `_first_unsafe`:

  single quotes  inert except for a literal `'`
  double quotes  inert except for `$`, a backtick, `"` or a backslash
  comment        inert except for a newline (which ends the comment)
  bare word      never inert
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parent.parent
_ROLES = _REPO_ROOT / "roles"

_TESTS_DIR = Path(__file__).resolve().parent
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

from helpers import make_ansible_env  # noqa: E402


# ── Payloads ─────────────────────────────────────────────────────────────
#
# Each carries a recognisable marker so an occurrence in the output is
# unambiguous, and each attacks a different quoting mistake.

_PAYLOADS = {
    "cmdsub": "hz$(id)hz",
    "backtick": "hz`id`hz",
    "single_quote": "hz'; id; 'hz",
    "double_quote": 'hz" ; id ; "hz',
    "space": "hz id hz",
    "newline": "hz\nid\nhz",
}


def _needs(payload: str) -> dict[str, bool]:
    return {
        "sq": "'" in payload,
        "dq": any(c in payload for c in '$`"\\'),
        "comment": "\n" in payload,
    }


def _first_unsafe(text: str, payload: str) -> tuple[int, str] | None:
    """Return (offset, context) of the first occurrence bash would evaluate.

    Walks the rendered script tracking quote state, then checks every
    occurrence of `payload` against the state at its first character. An
    occurrence that starts in one context and ends in another is reported as
    unsafe on the strength of its opening context — a payload that changes
    the quote state is exactly the bug being hunted.
    """
    if payload not in text:
        return None

    hostile = _needs(payload)
    state = "bare"
    contexts: list[str] = []
    escaped = False
    for ch in text:
        contexts.append(state)
        if state == "comment":
            if ch == "\n":
                state = "bare"
            continue
        if state == "sq":
            if ch == "'":
                state = "bare"
            continue
        if state == "dq":
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                state = "bare"
            continue
        # bare
        if ch == "'":
            state = "sq"
        elif ch == '"':
            state = "dq"
        elif ch == "#":
            state = "comment"

    start = 0
    while True:
        idx = text.find(payload, start)
        if idx == -1:
            return None
        ctx = contexts[idx]
        if ctx == "bare" or hostile.get({"sq": "sq", "dq": "dq", "comment": "comment"}[ctx], True):
            return idx, ctx
        start = idx + 1


def _assert_inert(rendered: str, payload: str, label: str) -> None:
    hit = _first_unsafe(rendered, payload)
    if hit is None:
        return
    idx, ctx = hit
    window = rendered[max(0, idx - 120): idx + 120]
    pytest.fail(
        f"{label}: payload {payload!r} survives into a {ctx} context — "
        f"bash would evaluate it.\n...{window}..."
    )


# ── Render helpers ───────────────────────────────────────────────────────


def _render(template_rel: str, **ctx) -> str:
    path = _REPO_ROOT / template_rel
    env = make_ansible_env(path.parent)
    return env.get_template(path.name).render(**ctx)


@dataclass
class Case:
    """One template plus a way to poison it."""

    template: str
    render: Callable[[str], str]
    # Values this template renders through `| to_json` rather than `| quote`.
    # to_json escapes `"`, `\` and newlines but NOT `$` or a backtick, so a
    # value here is inert against three of the six payloads, not all six.
    residuals: set[str] = field(default_factory=set)


# ── Documented residuals ─────────────────────────────────────────────────
#
# `docker_monitor_alert_header` is rendered with `| to_json` in three
# git_deploy templates. That predates this spec and is the pattern the M109
# spec explicitly names as acceptable, so it is left alone here rather than
# changed under a security spec. What it buys and what it does not:
#
#   * safe: a quote, a backslash or a newline in the header cannot break the
#     assignment — the exposure that actually mattered, since the header is a
#     free-text string an operator types.
#   * NOT safe: `$(...)` or a backtick still expand, because the value lands
#     inside a bash double-quoted string.
#
# The value is a group_vars string with the same trust level as the playbook
# itself. Promoting these three to `| quote` closes the residual outright and
# is the right follow-up; it is recorded here so the gap is visible rather
# than forgotten.
_DOCUMENTED_RESIDUALS: dict[str, set[str]] = {
    "roles/git_deploy/templates/build-alert.sh.j2": {"docker_monitor_alert_header"},
    "roles/git_deploy/templates/bay-trigger-watchdog.sh.j2": {"docker_monitor_alert_header"},
    "roles/git_deploy/templates/rebuild.sh.j2": {"docker_monitor_alert_header"},
}


# Templates that interpolate nothing a consumer controls. Each needs a reason.
_NO_CONSUMER_INPUT: dict[str, str] = {
    # `_askpass_token` is a GitHub PAT (charset [A-Za-z0-9_]), rendered inside
    # a single-quoted printf argument. Covered by tests/test_git_askpass.py.
    "roles/git_deploy/templates/git-askpass.sh.j2":
        "only the PAT, shell-quoted with `| quote`; see test_git_askpass.py",
}


# ── Per-template render functions ────────────────────────────────────────


def _backup_sh(payload: str) -> str:
    return _render(
        "roles/backup/templates/backup.sh.j2",
        ansible_managed="test",
        accessory_name=payload,
        method="pg_dump",
        repo=f"s3:s3.example.com/{payload}",
        backup_restic_password=payload,
        backup_s3_access_key_id=payload,
        backup_s3_secret_access_key=payload,
        backup_scripts_dir="/opt/stack/backup",
        backup_lock_dir="/opt/stack/backup/locks",
        backup_restic_bin="/usr/local/bin/restic",
        docker_monitor_alert_header=payload,
        docker_monitor_alert_footer=payload,
        inventory_hostname="host1",
        retain=7,
        accessory_config={
            "env": {"clear": {"POSTGRES_USER": payload, "POSTGRES_DB": payload}},
            "backup": {"databases": [payload]},
        },
        bound_databases=[],
        region="",
        headscale_server_tailnet_ip="",
    )


def _backup_sh_mysql(payload: str) -> str:
    return _render(
        "roles/backup/templates/backup.sh.j2",
        ansible_managed="test",
        accessory_name=payload,
        method="mysql",
        repo="s3:s3.example.com/b",
        backup_restic_password=payload,
        backup_s3_access_key_id="k",
        backup_s3_secret_access_key="s",
        backup_scripts_dir="/opt/stack/backup",
        backup_lock_dir="/opt/stack/backup/locks",
        backup_restic_bin="/usr/local/bin/restic",
        docker_monitor_alert_header="[bay] ",
        docker_monitor_alert_footer="",
        inventory_hostname="host1",
        retain={"daily": 7, "weekly": 4, "monthly": 6},
        accessory_config={
            "env": {
                "clear": {
                    "MYSQL_ROOT_PASSWORD": payload,
                    "MYSQL_USER": payload,
                    "MYSQL_DATABASE": payload,
                }
            }
        },
        bound_databases=[],
        region="",
        headscale_server_tailnet_ip="",
    )


def _backup_sh_file(payload: str) -> str:
    return _render(
        "roles/backup/templates/backup.sh.j2",
        ansible_managed="test",
        accessory_name="redis",
        method="file",
        repo="s3:s3.example.com/b",
        backup_restic_password="p",
        backup_s3_access_key_id="k",
        backup_s3_secret_access_key="s",
        backup_scripts_dir="/opt/stack/backup",
        backup_lock_dir="/opt/stack/backup/locks",
        backup_restic_bin="/usr/local/bin/restic",
        docker_monitor_alert_header="[bay] ",
        docker_monitor_alert_footer="",
        inventory_hostname="host1",
        retain=7,
        accessory_config={"env": {"clear": {}}, "backup": {"source_path": payload}},
        bound_databases=[],
        region="",
        headscale_server_tailnet_ip="",
    )


def _maintenance_sh(payload: str) -> str:
    return _render(
        "roles/backup/templates/maintenance.sh.j2",
        ansible_managed="test",
        accessory_name=payload,
        repo=payload,
        backup_restic_password=payload,
        backup_s3_access_key_id=payload,
        backup_s3_secret_access_key=payload,
        backup_scripts_dir="/opt/stack/backup",
        backup_restic_bin=payload,
        docker_monitor_alert_header=payload,
        docker_monitor_alert_footer=payload,
        inventory_hostname="host1",
        region="",
        headscale_server_tailnet_ip="",
    )


def _notify_snippet(payload: str) -> str:
    return _render(
        "roles/alert_channel/templates/_notify.sh.j2",
        ansible_managed="test",
        alert_env_path=f"/etc/bay/{payload}.env",
        alert_webhook_format=payload,
        alert_webhook_max_chars=3500,
        alert_webhook_timeout=10,
        docker_monitor_telegram_bot_token=payload,
        docker_monitor_telegram_chat_id=payload,
        alert_webhook_url=payload,
        inventory_hostname="host1",
        region="",
        alert_recipients=[
            {
                "name": payload,
                "adapter": "telegram",
                "min_level": "info",
                "config": {"chat_id": payload},
            },
            {
                "name": payload,
                "adapter": "webhook",
                "min_level": "info",
                "config": {
                    "url": "https://example.invalid/hook",
                    "method": payload,
                    "content_type": payload,
                    "headers": {"X-Token": payload},
                },
            },
        ],
    )


def _notify_snippet_env_names(payload: str) -> str:
    """The `*_env` variant — a NAME pasted into a parameter expansion."""
    return _render(
        "roles/alert_channel/templates/_notify.sh.j2",
        ansible_managed="test",
        alert_webhook_max_chars=3500,
        alert_webhook_timeout=10,
        docker_monitor_telegram_bot_token="t",
        docker_monitor_telegram_chat_id="c",
        alert_webhook_url="https://example.invalid/hook",
        inventory_hostname="host1",
        region="",
        alert_recipients=[
            {
                "name": "ops",
                "adapter": "telegram",
                "min_level": "info",
                "config": {"token_env": payload, "chat_id_env": payload},
            },
            {
                "name": "ops2",
                "adapter": "webhook",
                "min_level": "info",
                "config": {"url_env": payload},
            },
        ],
    )


def _builder_prune(payload: str) -> str:
    return _render(
        "roles/cronjobs/templates/bay-docker-builder-prune.sh.j2",
        docker_prune_builders=["bay-builder", payload],
        docker_prune_builder_users=["appuser", payload],
        docker_prune_builder_keep_storage=payload,
    )


def _crowdsec_refresh(payload: str) -> str:
    return _render(
        "roles/crowdsec/templates/crowdsec-data-refresh.sh.j2",
        crowdsec_data_refresh_parsers=["crowdsecurity/geoip-enrich", payload],
    )


def _boot_safety(payload: str) -> str:
    return _render(
        "roles/boot_safety/templates/bay-infra-ensure.sh.j2",
        ansible_managed="test",
        boot_safety_compose_file=payload,
        traefik_container_name=payload,
    )


def _build_alert(payload: str) -> str:
    return _render(
        "roles/git_deploy/templates/build-alert.sh.j2",
        ansible_managed="test",
        stack_dir=payload,
        docker_monitor_alert_header=payload,
        git_deploy_build_timeout=1200,
        git_deploy_build_mem_limit="2g",
        inventory_hostname="host1",
        region="",
    )


def _trigger_watchdog(payload: str) -> str:
    return _render(
        "roles/git_deploy/templates/bay-trigger-watchdog.sh.j2",
        ansible_managed="test",
        stack_dir=payload,
        docker_monitor_alert_header=payload,
        git_deploy_stall_watchdog_threshold=600,
        git_deploy_stall_watchdog_repeat_sec=1800,
        inventory_hostname="host1",
        region="",
    )


def _log_archive(name: str):
    def _inner(payload: str) -> str:
        return _render(
            f"roles/log_archive/templates/{name}",
            ansible_managed="test",
            app_user=payload,
            log_archive_group=payload,
            stack_dir="/opt/stack",
            log_archive_dir="/opt/stack/logs/archive",
            log_archive_retention_days=30,
            log_archive_services=["app"],
            inventory_hostname="host1",
            region="",
        )

    return _inner


def _log_archive_setup(payload: str) -> str:
    """The batched artefact script (M111 P5): service KEYS reach a shell here."""
    return _render(
        "roles/log_archive/templates/setup-log-dirs.sh.j2",
        ansible_managed="test",
        app_user=payload,
        log_archive_group=payload,
        log_archive_log_root="/opt/stack/logs/services",
        log_archive_default_mode="normal",
        log_archive_default_compress=True,
        _log_archive_containers=[
            {
                "key": payload,
                "value": {
                    "log_retention": {
                        "mode": "normal",
                        "days": payload,
                        "max_total_size": payload,
                    }
                },
            },
            {
                "key": f"sensitive-{payload}",
                "value": {"log_retention": {"mode": "sensitive", "days": 7}},
            },
        ],
    )


def _disk_alert(payload: str) -> str:
    return _render(
        "roles/outbound_monitor/templates/bay-disk-alert.sh.j2",
        ansible_managed="test",
        outbound_check_state_dir=payload,
        disk_alert_warn_pct=80,
        disk_alert_page_pct=90,
        inventory_hostname="host1",
        region="",
    )


def _rebuild_sh(payload: str) -> str:
    from test_rebuild_config import _render_rebuild_sh

    services = {
        "api": {
            "access": "public",
            "image": f"registry.invalid/{payload}:latest",
            "domains": ["api.example.com"],
            "ports": {"internal": 3000},
            "env": {"clear": {"MODE": payload}},
            "build": {"repo": "git@github.com:acme/api.git", "branch": "main"},
        }
    }
    return _render_rebuild_sh(services, ["api"], git_deploy_services=["api"])


def _rebuild_sh_build_fields(payload: str) -> str:
    """The `build:` block itself — repo, branch, dockerfile, context.

    The v0.2.4 audit left these unexercised: the only rebuild.sh case poisoned
    the image and an env value, so the four fields that reach BRANCH/
    DOCKERFILE/CONTEXT and the eval'd CLONE/FETCH/PULL command strings were
    never rendered with anything hostile. `docker_monitor_alert_header` is
    left benign here on purpose, so this case carries no documented residual
    and every payload is a real assertion.
    """
    from test_rebuild_config import _render_rebuild_sh

    services = {
        "api": {
            "access": "public",
            "domains": ["api.example.com"],
            "ports": {"internal": 3000},
            "build": {
                "repo": f"git@github.com:acme/{payload}.git",
                "branch": payload,
                "dockerfile": payload,
                "context": payload,
            },
        }
    }
    return _render_rebuild_sh(services, ["api"], git_deploy_services=["api"])


def _rebuild_sh_build_fields_token(payload: str) -> str:
    """Same fields, HTTPS/token path — a different clone_url and PULL_CMD."""
    from test_rebuild_config import _render_rebuild_sh

    services = {
        "api": {
            "access": "public",
            "domains": ["api.example.com"],
            "ports": {"internal": 3000},
            "build": {
                "repo": f"https://github.com/acme/{payload}.git",
                "branch": payload,
                "dockerfile": payload,
                "context": payload,
                "token": "ghp_sentinel",
            },
        }
    }
    return _render_rebuild_sh(
        services, ["api"], git_deploy_services=["api"],
        secrets={"API_GIT_TOKEN": "ghp_sentinel"},
    )


def _rebuild_sh_build_fields_remote(payload: str) -> str:
    """Same fields, remote strategy — CLONE_CMD and FETCH_CMD."""
    from test_rebuild_config import _render_rebuild_sh

    services = {
        "api": {
            "access": "public",
            "image": "registry.invalid/acme/api:latest",
            "domains": ["api.example.com"],
            "ports": {"internal": 3000},
            "build": {
                "repo": f"https://github.com/acme/{payload}.git",
                "branch": payload,
                "dockerfile": payload,
                "context": payload,
                "token": "ghp_sentinel",
                "strategy": "remote",
            },
        }
    }
    return _render_rebuild_sh(
        services, ["api"], git_deploy_services=["api"],
        secrets={"API_GIT_TOKEN": "ghp_sentinel"},
    )


_CASES: list[Case] = [
    Case("roles/backup/templates/backup.sh.j2", _backup_sh),
    Case("roles/backup/templates/backup.sh.j2", _backup_sh_mysql),
    Case("roles/backup/templates/backup.sh.j2", _backup_sh_file),
    Case("roles/backup/templates/maintenance.sh.j2", _maintenance_sh),
    Case("roles/alert_channel/templates/_notify.sh.j2", _notify_snippet),
    Case("roles/alert_channel/templates/_notify.sh.j2", _notify_snippet_env_names),
    Case("roles/cronjobs/templates/bay-docker-builder-prune.sh.j2", _builder_prune),
    Case("roles/crowdsec/templates/crowdsec-data-refresh.sh.j2", _crowdsec_refresh),
    Case("roles/boot_safety/templates/bay-infra-ensure.sh.j2", _boot_safety),
    Case(
        "roles/git_deploy/templates/build-alert.sh.j2",
        _build_alert,
        residuals={"docker_monitor_alert_header"},
    ),
    Case(
        "roles/git_deploy/templates/bay-trigger-watchdog.sh.j2",
        _trigger_watchdog,
        residuals={"docker_monitor_alert_header"},
    ),
    Case(
        "roles/log_archive/templates/archive-logs.sh.j2",
        _log_archive("archive-logs.sh.j2"),
    ),
    Case(
        "roles/log_archive/templates/rotate-logs.sh.j2",
        _log_archive("rotate-logs.sh.j2"),
    ),
    Case(
        "roles/log_archive/templates/scrub-logs.sh.j2",
        _log_archive("scrub-logs.sh.j2"),
    ),
    Case(
        "roles/log_archive/templates/setup-log-dirs.sh.j2",
        _log_archive_setup,
    ),
    Case("roles/outbound_monitor/templates/bay-disk-alert.sh.j2", _disk_alert),
    Case(
        "roles/git_deploy/templates/rebuild.sh.j2",
        _rebuild_sh,
        residuals={"docker_monitor_alert_header"},
    ),
    Case("roles/git_deploy/templates/rebuild.sh.j2", _rebuild_sh_build_fields),
    Case("roles/git_deploy/templates/rebuild.sh.j2", _rebuild_sh_build_fields_token),
    Case("roles/git_deploy/templates/rebuild.sh.j2", _rebuild_sh_build_fields_remote),
]

_CASE_IDS = [f"{Path(c.template).name}-{c.render.__name__}" for c in _CASES]


# ── The guard ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("case", _CASES, ids=_CASE_IDS)
@pytest.mark.parametrize("payload_name", sorted(_PAYLOADS))
def test_shell_template_renders_hostile_input_inert(case: Case, payload_name: str):
    payload = _PAYLOADS[payload_name]
    if case.residuals and payload_name in ("cmdsub", "backtick"):
        # The residual value is to_json-escaped, which does not neutralise a
        # command substitution. Documented in _DOCUMENTED_RESIDUALS.
        pytest.skip(f"documented residual: {sorted(case.residuals)}")
    try:
        rendered = case.render(payload)
    except (ValueError, TypeError) as exc:
        # Refusing to render is the strongest outcome. Confirm it refused for
        # a reason we recognise rather than because the fixture is broken.
        assert any(
            word in str(exc).lower()
            for word in ("invalid", "newline", "cannot", "environment")
        ), f"{case.template} raised something unexpected: {exc}"
        return
    _assert_inert(rendered, payload, f"{case.template} [{payload_name}]")


def test_hostile_table_coverage_is_complete():
    """A new *.sh.j2 must be listed, or exempted with a reason.

    This is what stops the guard rotting: the audit's finding was not that
    one template was wrong, it was that nothing looked at any of them.
    """
    on_disk = {
        str(p.relative_to(_REPO_ROOT))
        for p in _ROLES.rglob("*.sh.j2")
        if not p.is_symlink()
    }
    covered = {c.template for c in _CASES} | set(_NO_CONSUMER_INPUT)
    missing = on_disk - covered
    assert not missing, (
        "these shell templates are in neither the hostile-input table nor the "
        f"documented exemption list: {sorted(missing)}"
    )
    stale = covered - on_disk
    assert not stale, f"listed but no longer on disk: {sorted(stale)}"


def test_documented_residuals_are_exactly_these():
    """The escape hatch is a ratchet, not a drawer."""
    from_cases = {c.template: c.residuals for c in _CASES if c.residuals}
    assert from_cases == _DOCUMENTED_RESIDUALS


# ── The SQL and shell that live in task files ────────────────────────────


_SQL_KEYWORDS = ("SELECT ", "CREATE ", "ALTER ", "GRANT ", "DROP ")


def _sql_strings(task: dict) -> list[str]:
    """Every string in a task that psql will parse as SQL."""
    out: list[str] = []
    mod = task["ansible.builtin.command"]
    out.extend(str(a) for a in mod.get("argv", []))
    if "stdin" in mod:
        out.append(str(mod["stdin"]))
    return [t for t in out if any(kw in t for kw in _SQL_KEYWORDS)]


def test_database_provision_sql_is_quoted():
    """No consumer name reaches SQL as anything but an escaped literal.

    M111 collapsed the six looped `docker exec` tasks into one templated
    script per accessory, so the SQL now lives in
    `templates/provision-db.sql.j2` and is rendered here rather than read out
    of the task file. The guarantee is unchanged and stronger: names cross as
    SQL string literals with `'` doubled, and `format('%I', ...)` turns them
    into identifiers server-side. Full coverage is in
    tests/test_database_provision_sql.py; this keeps the check inside the
    hostile-input suite so it is not forgotten.
    """
    task_path = _ROLES / "deploy_stack" / "tasks" / "database_provision.yml"
    task_text = task_path.read_text()
    tasks = yaml.safe_load(task_text)
    block = next(t["block"] for t in tasks if "block" in t)
    execs = [
        t for t in block
        if "ansible.builtin.command" in t
        and t["ansible.builtin.command"].get("argv", [])[:2] == ["docker", "exec"]
    ]
    assert len(execs) == 1, "one docker exec per accessory, not a fan"
    # `cmd:` would shlex-split the accessory name; argv does not.
    assert "argv" in execs[0]["ansible.builtin.command"]
    assert "cmd" not in execs[0]["ansible.builtin.command"]
    # No SQL text left in the task file at all.
    assert not any(kw in task_text for kw in _SQL_KEYWORDS)

    sql_path = _ROLES / "deploy_stack" / "templates" / "provision-db.sql.j2"
    env = make_ansible_env(sql_path.parent)
    template = env.get_template(sql_path.name)

    for payload in _PAYLOADS.values():
        vault_key = payload.upper().replace("-", "_") + "_POSTGRES_PASSWORD"
        sql = template.render(
            ansible_managed="test",
            _acc="postgres",
            _acc_bindings=[
                {
                    "key": payload,
                    "value": {"database": {"accessory": "postgres"}},
                }
            ],
            secrets={vault_key: payload},
        )
        body = "\n".join(
            line for line in sql.splitlines() if not line.lstrip().startswith("--")
        )
        escaped = payload.replace("'", "''")
        assert escaped in body, "fixture broken: payload never reached the SQL"
        for stmt in ("CREATE DATABASE ", "CREATE ROLE ", "ALTER ROLE ",
                     "GRANT ALL PRIVILEGES ON DATABASE "):
            assert f"{stmt}{payload}" not in body
        # Never spliced in as a double-quoted identifier either.
        assert f'"{payload}"' not in body


def test_log_retention_boundary_passes_the_service_key_via_environment():
    path = _ROLES / "deploy_stack" / "tasks" / "log_retention_boundary_post.yml"
    text = path.read_text()
    tasks = yaml.safe_load(text)
    sentinel = [t for t in tasks if "shell" in str(t.get("ansible.builtin.shell", ""))
                or "ansible.builtin.shell" in t]
    assert sentinel, "expected the sentinel-writing shell task"
    task = sentinel[0]
    # M111 batched the inspects, so the loop var is now a dict2items entry.
    assert task.get("environment", {}).get("SVC") == "{{ item.key }}"
    body = task["ansible.builtin.shell"]["cmd"]
    assert "${SVC}" in body
    assert "{{ item.key }}" not in body, (
        "the service key is back inside the shell string"
    )


def test_traefik_rule_builders_refuse_a_backtick():
    """Both copies of the filter, because they drift."""
    import importlib.util

    for rel in (
        "filter_plugins/bay_filters.py",
        "roles/container_lifecycle/filter_plugins/bay_filters.py",
    ):
        spec = importlib.util.spec_from_file_location(
            f"hostile_{rel.replace('/', '_')}", _REPO_ROOT / rel
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        assert mod._host_rule(["app.example.com"]) == "Host(`app.example.com`)"
        with pytest.raises(ValueError):
            mod._host_rule(["app.example.com`) || Host(`evil.invalid"])


def test_env_file_rejects_a_newline_in_a_value():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "hostile_env_value", _REPO_ROOT / "filter_plugins" / "bay_filters.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.bay_env_value("a$b") == "a$$b"
    with pytest.raises(ValueError):
        mod.bay_env_value("secret\nINJECTED=1")


def test_every_env_file_template_uses_bay_env_value():
    """A newline in a value injects a second KEY=VALUE into the container.

    env.j2 has gone through `bay_env_value` since the audit; webhook.env.j2 and
    watchtower.env.j2 were left doubling `$` by hand, which handles Compose
    interpolation and nothing else. The env-file format is one KEY=VALUE per
    line and has no escape for a newline, so the filter refuses it — a
    hand-rolled `replace()` cannot.
    """
    templates = sorted(
        (_ROLES / "deploy_stack" / "templates").glob("*env.j2")
    )
    assert {p.name for p in templates} >= {
        "env.j2", "webhook.env.j2", "watchtower.env.j2"
    }
    for path in templates:
        text = path.read_text()
        assert "replace('$', '$$')" not in text, (
            f"{path.name} doubles `$` by hand instead of using bay_env_value, "
            "so a newline in the value is not refused"
        )
        if path.name == "env.j2":
            # env.j2 also writes DB_HOST/DB_PORT/DB_NAME/DB_USER, which are
            # schema-checked identifiers rather than free-text credentials.
            continue
        for line in text.splitlines():
            if "=" in line and "{{" in line and not line.startswith("#"):
                assert "bay_env_value" in line, (
                    f"{path.name}: unfiltered value in {line!r}"
                )


def test_bay_env_value_is_what_refuses_the_newline():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "hostile_env_value_2", _REPO_ROOT / "filter_plugins" / "bay_filters.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for bad in ("a\nINJECTED=1", "a\rINJECTED=1"):
        with pytest.raises(ValueError):
            mod.bay_env_value(bad)
