"""Zot request time limits and pull-only registry accounts.

Two gaps in the registry role:

  1. Time limits. zot's `http.readTimeout` / `http.writeTimeout` default to
     60s and are ABSOLUTE deadlines for the whole request / response body.
     A layer upload still streaming at 60s is cut (`i/o timeout`, the proxy
     answers 502). The role now renders both from `zot_http_read_timeout` /
     `zot_http_write_timeout` (default "30m"); "" renders nothing.
  2. Pull-only accounts. `zot_pull_users` adds htpasswd accounts that may only
     read. With the list non-empty, `http.accessControl` gives them `read` on
     every repository and gives the admin (the docker_registries entry for
     zot_domain) every action through `adminPolicy`. With the list empty the
     block is absent, so existing fleets keep the single-account behaviour.

The htpasswd write logic is covered in test_zot_htpasswd_idempotency.py.
Here: config.json rendering, the validation asserts (evaluated with Jinja
against sample input, Ansible's own `match` test included), and static
guarantees on the task file (no password on a command line, no_log wherever
a task can print one).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml
from ansible.plugins.test.core import TestModule as _CoreTests

from helpers import make_ansible_env

_REPO_ROOT = Path(__file__).parent.parent
_ZOT_DEFAULTS = _REPO_ROOT / "roles" / "zot" / "defaults" / "main.yml"
_ZOT_TASKS = _REPO_ROOT / "roles" / "zot" / "tasks" / "main.yml"
_ZOT_TEMPLATES = _REPO_ROOT / "roles" / "zot" / "templates"

_ADMIN = {"domain": "registry.example.com", "username": "admin", "password": "admin-pw"}

# The `http` section exactly as the role rendered it before this change.
# With both timeouts "" and no pull users the role must render these bytes,
# so a fleet that opts out of everything sees no config change and no restart.
_LEGACY_HTTP = """  "http": {
    "address": "0.0.0.0",
    "port": "5000",
    "realm": "registry.example.com",
    "compat": ["docker2s2"],
    "auth": {
      "htpasswd": {
        "path": "/etc/zot/htpasswd"
      },
      "failDelay": 5
    }
  },
"""


def _defaults() -> dict:
    return yaml.safe_load(_ZOT_DEFAULTS.read_text())


def _context(**overrides) -> dict:
    """Role defaults (literal values) plus what the role computes."""
    ctx = {k: v for k, v in _defaults().items() if not (isinstance(v, str) and "{{" in v)}
    ctx.update(
        zot_domain="registry.example.com",
        zot_cache_dir="/var/lib/registry",
        _zot_creds=_ADMIN,
    )
    ctx.update(overrides)
    return ctx


def _render_source(source: str, **overrides) -> str:
    env = make_ansible_env(_ZOT_TEMPLATES)
    return env.from_string(source).render(**_context(**overrides))


def _render(**overrides) -> str:
    return _render_source((_ZOT_TEMPLATES / "config.json.j2").read_text(), **overrides)


def _http(**overrides) -> dict:
    return json.loads(_render(**overrides))["http"]


def _http_text(rendered: str) -> str:
    start = rendered.index('  "http": {')
    end = rendered.index('  "log": {')
    return rendered[start:end]


# ── config.json: time limits ────────────────────────────────────────


class TestTimeouts:
    def test_defaults_are_thirty_minutes(self) -> None:
        defaults = _defaults()
        assert defaults["zot_http_read_timeout"] == "30m"
        assert defaults["zot_http_write_timeout"] == "30m"

    def test_defaults_render_both_keys(self) -> None:
        http = _http()
        assert http["readTimeout"] == "30m"
        assert http["writeTimeout"] == "30m"

    def test_both_empty_render_neither(self) -> None:
        http = _http(zot_http_read_timeout="", zot_http_write_timeout="")
        assert "readTimeout" not in http
        assert "writeTimeout" not in http

    @pytest.mark.parametrize(
        ("read", "write"), [("", "30m"), ("30m", ""), ("  ", "1h")]
    )
    def test_one_empty_renders_only_the_other(self, read: str, write: str) -> None:
        http = _http(zot_http_read_timeout=read, zot_http_write_timeout=write)
        assert ("readTimeout" in http) == bool(read.strip())
        assert ("writeTimeout" in http) == bool(write.strip())

    def test_unset_vars_render_neither(self) -> None:
        """Other test harnesses render this template without the new vars."""
        ctx = _context()
        del ctx["zot_http_read_timeout"], ctx["zot_http_write_timeout"]
        env = make_ansible_env(_ZOT_TEMPLATES)
        http = json.loads(env.get_template("config.json.j2").render(**ctx))["http"]
        assert "readTimeout" not in http and "writeTimeout" not in http

    def test_value_is_trimmed(self) -> None:
        assert _http(zot_http_read_timeout=" 45m ")["readTimeout"] == "45m"


# ── config.json: pull-only accounts ─────────────────────────────────


class TestAccessControl:
    def test_default_pull_list_is_empty(self) -> None:
        assert _defaults()["zot_pull_users"] == []

    def test_empty_list_renders_no_access_control(self) -> None:
        assert "accessControl" not in _http()

    def test_null_list_renders_no_access_control(self) -> None:
        assert "accessControl" not in _http(zot_pull_users=None)

    def test_opted_out_http_section_is_byte_identical_to_before(self) -> None:
        rendered = _render(
            zot_http_read_timeout="", zot_http_write_timeout="", zot_pull_users=[]
        )
        assert _http_text(rendered) == _LEGACY_HTTP

    def test_one_pull_user_exact_http(self) -> None:
        http = _http(zot_pull_users=[{"username": "dev-laptop", "password": "pw"}])
        assert http == {
            "address": "0.0.0.0",
            "port": "5000",
            "realm": "registry.example.com",
            "readTimeout": "30m",
            "writeTimeout": "30m",
            "compat": ["docker2s2"],
            "auth": {"htpasswd": {"path": "/etc/zot/htpasswd"}, "failDelay": 5},
            "accessControl": {
                "repositories": {
                    "**": {"policies": [{"users": ["dev-laptop"], "actions": ["read"]}]}
                },
                "adminPolicy": {
                    "users": ["admin"],
                    "actions": ["read", "create", "update", "delete"],
                },
            },
        }

    def test_two_pull_users_share_one_read_policy(self) -> None:
        http = _http(
            zot_pull_users=[
                {"username": "dev-laptop", "password": "a"},
                {"username": "ci-reader", "password": "b"},
            ]
        )
        policies = http["accessControl"]["repositories"]["**"]["policies"]
        assert policies == [{"users": ["dev-laptop", "ci-reader"], "actions": ["read"]}]

    def test_no_password_reaches_the_config(self) -> None:
        rendered = _render(
            zot_pull_users=[{"username": "dev-laptop", "password": "pull-secret-xyz"}]
        )
        assert "pull-secret-xyz" not in rendered
        assert _ADMIN["password"] not in rendered

    def test_s3_driver_with_pull_users_is_valid_json(self) -> None:
        parsed = json.loads(
            _render(
                zot_storage_driver="s3",
                zot_s3_bucket="bucket",
                zot_s3_region="eu-central-1",
                zot_remote_cache=True,
                zot_pull_users=[{"username": "dev-laptop", "password": "pw"}],
            )
        )
        assert parsed["storage"]["storageDriver"]["name"] == "s3"
        assert parsed["http"]["accessControl"]["adminPolicy"]["users"] == ["admin"]

    @pytest.mark.parametrize("read", ["", "30m"])
    @pytest.mark.parametrize("write", ["", "30m"])
    @pytest.mark.parametrize("pull", [[], [{"username": "a", "password": "b"}]])
    @pytest.mark.parametrize("driver", ["filesystem", "s3"])
    def test_every_combination_is_valid_json(self, read, write, pull, driver) -> None:
        json.loads(
            _render(
                zot_http_read_timeout=read,
                zot_http_write_timeout=write,
                zot_pull_users=pull,
                zot_storage_driver=driver,
            )
        )

    def test_user_strings_are_json_escaped(self) -> None:
        """Validation refuses such names, but the template must not depend on it."""
        http = _http(
            zot_pull_users=[{"username": 'we"ird', "password": "pw"}],
            _zot_creds={"username": 'ad"min', "password": "pw"},
        )
        assert http["accessControl"]["repositories"]["**"]["policies"][0]["users"] == ['we"ird']
        assert http["accessControl"]["adminPolicy"]["users"] == ['ad"min']


# ── validation asserts, evaluated ───────────────────────────────────


def _tasks() -> list[dict]:
    return yaml.safe_load(_ZOT_TASKS.read_text())


def _task(name: str) -> dict:
    for task in _tasks():
        if task.get("name") == name:
            return task
    raise AssertionError(f"task {name!r} not found in {_ZOT_TASKS}")


def _assert_holds(name: str, **ctx) -> bool:
    """Evaluate every `that:` of an assert task the way Ansible would."""
    task = _task(name)
    env = make_ansible_env(_ZOT_TASKS.parent)
    env.tests.update(_CoreTests().tests())
    scope = {"_zot_creds": _ADMIN, "zot_pull_users": [], **ctx}
    for key, raw in (task.get("vars") or {}).items():
        body = raw.strip()
        assert body.startswith("{{") and body.endswith("}}")
        scope[key] = env.compile_expression(body[2:-2].strip())(**scope)
    return all(
        bool(env.compile_expression(cond.strip())(**scope))
        for cond in task["ansible.builtin.assert"]["that"]
    )


_SHAPE = "Validate zot_pull_users is a list of username/password entries"
_FIELDS = "Validate every zot_pull_users entry has a non-empty username and password"
_NAMES = "Validate zot_pull_users usernames"
_TIMEOUTS = "Validate zot request time limits"


def _pull_ok(users) -> bool:
    return all(_assert_holds(n, zot_pull_users=users) for n in (_SHAPE, _FIELDS, _NAMES))


class TestPullUserValidation:
    def test_empty_and_null_lists_pass(self) -> None:
        assert _pull_ok([])
        assert _pull_ok(None)

    def test_good_entries_pass(self) -> None:
        assert _pull_ok(
            [
                {"username": "dev-laptop", "password": "a"},
                {"username": "ci.reader_2", "password": "b"},
                {"username": "x" * 63, "password": "c"},
            ]
        )

    @pytest.mark.parametrize(
        "users",
        [
            {"username": "dev", "password": "pw"},
            "dev:pw",
            ["dev:pw"],
            [{"username": "dev"}],
            [{"password": "pw"}],
            [{"username": "", "password": "pw"}],
            [{"username": "dev", "password": ""}],
            [{"username": "Dev", "password": "pw"}],
            [{"username": "-dev", "password": "pw"}],
            [{"username": "dev laptop", "password": "pw"}],
            [{"username": "dev:x", "password": "pw"}],
            [{"username": "dev\nx", "password": "pw"}],
            [{"username": "x" * 64, "password": "pw"}],
            [{"username": "dev", "password": "a"}, {"username": "dev", "password": "b"}],
            [{"username": "admin", "password": "pw"}],
        ],
        ids=[
            "mapping-not-list", "string-not-list", "string-entry", "no-password",
            "no-username", "empty-username", "empty-password", "uppercase",
            "leading-dash", "space", "colon", "newline", "too-long", "duplicate",
            "admin-name",
        ],
    )
    def test_bad_input_is_refused(self, users) -> None:
        assert not _pull_ok(users)

    def test_password_tasks_use_no_log_and_name_the_problem(self) -> None:
        for name in (_SHAPE, _FIELDS):
            task = _task(name)
            assert task["no_log"] is True
            msg = task["ansible.builtin.assert"]["fail_msg"]
            assert "{{" not in msg, "a no_log assert message must not template values"

    def test_username_task_never_touches_a_password(self) -> None:
        task = _task(_NAMES)
        text = yaml.safe_dump(task)
        assert "password" not in text


class TestTimeoutValidation:
    @pytest.mark.parametrize("value", ["", "30m", "0s", "1h30m", "90s", "500ms", "2h"])
    def test_good_values_pass(self, value: str) -> None:
        assert _assert_holds(
            _TIMEOUTS, zot_http_read_timeout=value, zot_http_write_timeout=value
        )

    @pytest.mark.parametrize("value", ["30", "30 minutes", "1.5h", "m", "-1s", "30M", None])
    def test_bad_values_fail(self, value) -> None:
        assert not _assert_holds(
            _TIMEOUTS, zot_http_read_timeout=value, zot_http_write_timeout="30m"
        )
        assert not _assert_holds(
            _TIMEOUTS, zot_http_read_timeout="30m", zot_http_write_timeout=value
        )

    def test_validation_runs_before_the_config_is_templated(self) -> None:
        names = [t.get("name") for t in _tasks()]
        template = names.index("Template zot config")
        for name in (_TIMEOUTS, _SHAPE, _FIELDS, _NAMES):
            assert names.index(name) < template


# ── static guarantees on the task file ──────────────────────────────

# Jinja references that can carry a password value. A reference that only
# takes the usernames out of a list is fine; so is `_zot_creds.username`.
_SECRET_REFS = [
    re.compile(r"password"),
    re.compile(r"\b_zot_creds\b(?!\.username)"),
    re.compile(r"\b_zot_htpasswd_users\b(?!\s*\|\s*map\(attribute='username'\))"),
    re.compile(
        r"\bzot_pull_users\b(?!\s*(\|\s*default\(\[\],\s*true\)\s*)?\|\s*map\(attribute='username')"
    ),
    re.compile(r"\bzot_aws_secret_access_key\b"),
    re.compile(r"\b_zot_htpasswd\b"),
]
# Keys whose value Ansible never prints: conditions are shown as source text.
_SILENT_KEYS = {"name", "when", "that", "changed_when", "failed_when", "tags"}
_JINJA = re.compile(r"\{\{(.*?)\}\}|\{%(.*?)%\}", re.S)


def _printed_jinja(node, key: str | None = None) -> list[str]:
    """Every Jinja expression a task can print, plus set_fact keys."""
    if key in _SILENT_KEYS:
        return []
    if isinstance(node, dict):
        out: list[str] = []
        for k, v in node.items():
            if key == "ansible.builtin.set_fact":
                out.append(str(k))
            out += _printed_jinja(v, str(k))
        return out
    if isinstance(node, list):
        return [e for item in node for e in _printed_jinja(item, key)]
    if isinstance(node, str):
        return ["".join(m) for m in _JINJA.findall(node)]
    return []


def _sees_a_secret(task: dict) -> bool:
    return any(p.search(expr) for expr in _printed_jinja(task) for p in _SECRET_REFS)


class TestNoPasswordLeaks:
    def test_scanner_is_not_vacuous(self) -> None:
        """The tasks that obviously handle passwords must be flagged."""
        flagged = {t["name"] for t in _tasks() if _sees_a_secret(t)}
        for name in (
            "Look up Zot credentials from docker_registries",
            "Collect the htpasswd accounts (admin plus pull-only)",
            "Verify existing htpasswd file matches configured credentials",
            "Generate htpasswd file",
            "Write htpasswd file",
            "Render Zot AWS credentials env file (S3 driver)",
        ):
            assert name in flagged, name

    def test_every_task_that_can_print_a_password_has_no_log(self) -> None:
        missing = [
            t["name"] for t in _tasks() if _sees_a_secret(t) and t.get("no_log") is not True
        ]
        assert not missing, f"tasks that can print a password without no_log: {missing}"

    def test_no_password_on_a_command_line(self) -> None:
        """`ps` on the host shows argv to every local user."""
        for task in _tasks():
            command = task.get("ansible.builtin.command")
            if command is None:
                continue
            argv = command["cmd"] if isinstance(command, dict) else command
            assert not any(p.search(argv) for p in _SECRET_REFS), task["name"]

    def test_loops_over_accounts_label_with_the_username_only(self) -> None:
        for task in _tasks():
            if "_zot_htpasswd_users" in str(task.get("loop", "")):
                assert task["loop_control"]["label"] == "{{ item.username }}", task["name"]
                assert task["no_log"] is True, task["name"]
