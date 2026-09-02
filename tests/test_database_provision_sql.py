"""The provisioning SQL is one idempotent script per accessory (M111 P6).

Six looped `docker exec` tasks per binding became one `docker exec -i ... psql`
per accessory, with the idempotence guards moved into SQL. This file pins the
two properties that made that safe to do:

  * no consumer-supplied name is ever emitted as anything but a SQL string
    literal — identifiers are produced server-side by `format('%I', ...)`, and
  * the `pg_database` / `pg_roles` guards that replaced the exists-check tasks
    are actually there, so a second deploy is a no-op.
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parent.parent
_TEMPLATE = (
    _REPO_ROOT / "roles" / "deploy_stack" / "templates" / "provision-db.sql.j2"
)
_TASKS = _REPO_ROOT / "roles" / "deploy_stack" / "tasks" / "database_provision.yml"

_TESTS_DIR = Path(__file__).resolve().parent
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

from helpers import make_ansible_env  # noqa: E402

_HOSTILE = 'a"b; DROP DATABASE x'


def _render(bindings, secrets) -> str:
    env = make_ansible_env(_TEMPLATE.parent)
    return env.get_template(_TEMPLATE.name).render(
        ansible_managed="test",
        _acc="postgres",
        _acc_bindings=bindings,
        secrets=secrets,
    )


def _binding(key, *, name=None, user=None, accessory="postgres"):
    db = {"accessory": accessory}
    if name is not None:
        db["name"] = name
    if user is not None:
        db["user"] = user
    return {"key": key, "value": {"database": db}}


def _vault_key(user: str) -> str:
    return user.upper().replace("-", "_") + "_POSTGRES_PASSWORD"


def _strip_comments(sql: str) -> str:
    """Drop `--` comment lines — the header prose has apostrophes in it."""
    return "\n".join(
        line for line in sql.splitlines() if not line.lstrip().startswith("--")
    )


def _literal_spans(sql: str) -> list[tuple[int, int]]:
    """Half-open spans of the inside of every single-quoted SQL literal.

    `''` inside a literal is an escaped quote and stays part of the same
    literal, which is exactly the case a naive toggle gets wrong — and
    exactly the case a hostile password exercises.
    """
    spans: list[tuple[int, int]] = []
    i = 0
    n = len(sql)
    while i < n:
        if sql[i] != "'":
            i += 1
            continue
        start = i + 1
        i = start
        while i < n:
            if sql[i] != "'":
                i += 1
            elif i + 1 < n and sql[i + 1] == "'":
                i += 2
            else:
                break
        assert i < n, "unbalanced single quote in rendered SQL"
        spans.append((start, i))
        i += 1
    return spans


def _assert_only_inside_literals(sql: str, payload: str) -> None:
    sql = _strip_comments(sql)
    spans = _literal_spans(sql)
    idx = sql.find(payload)
    assert idx != -1, "fixture broken: the payload never reached the SQL"
    while idx != -1:
        end = idx + len(payload)
        assert any(s <= idx and end <= e for s, e in spans), (
            f"payload escapes its literal at offset {idx}:\n"
            f"...{sql[max(0, idx - 120): idx + 120]}..."
        )
        idx = sql.find(payload, idx + 1)


def test_a_hostile_database_name_never_leaves_its_literal():
    sql = _render(
        [_binding("app", name=_HOSTILE, user="app")],
        {_vault_key("app"): "pw"},
    )
    _assert_only_inside_literals(sql, _HOSTILE)
    # And it is never spliced in as a bare identifier.
    assert f'CREATE DATABASE {_HOSTILE}' not in sql
    assert f'"{_HOSTILE}"' not in sql


def test_a_hostile_role_name_never_leaves_its_literal():
    sql = _render(
        [_binding("app", name="app", user=_HOSTILE)],
        {_vault_key(_HOSTILE): "pw"},
    )
    _assert_only_inside_literals(sql, _HOSTILE)
    assert f"CREATE ROLE {_HOSTILE}" not in sql
    assert f"ALTER ROLE {_HOSTILE}" not in sql


def test_a_hostile_password_never_leaves_its_literal():
    payload = "pw'; DROP DATABASE x; --"
    sql = _render([_binding("app")], {_vault_key("app"): payload})
    # The quote is doubled at render time, so the raw payload is absent and
    # the escaped form sits inside a literal that `format('%L')` then quotes.
    assert payload not in _strip_comments(sql)
    _assert_only_inside_literals(sql, payload.replace("'", "''"))


def test_a_single_quote_is_doubled_not_dropped():
    sql = _render([_binding("app", name="o'brien")], {_vault_key("app"): "pw"})
    assert "'o''brien'" in sql
    assert "'o'brien'" not in sql


def test_idempotence_guards_replaced_the_exists_check_tasks():
    sql = _render([_binding("app")], {_vault_key("app"): "pw"})
    assert "FROM pg_database WHERE datname =" in sql
    assert "FROM pg_roles WHERE rolname =" in sql
    assert "WHERE NOT EXISTS" in sql


def test_create_database_runs_outside_a_do_block():
    """CREATE DATABASE cannot run in a transaction, so it must use \\gexec."""
    sql = _render([_binding("app")], {_vault_key("app"): "pw"})
    create = sql.index("'CREATE DATABASE '")
    tail = sql[create:]
    assert tail.split("\n\n")[0].rstrip().endswith("\\gexec")
    # ...and every other statement does go through format() in a DO block.
    assert "DO $bay$" in sql
    assert sql.count("format('%I'") >= 3


def test_every_identifier_goes_through_format_i():
    sql = _render(
        [_binding("app", name="appdb", user="appuser")],
        {_vault_key("appuser"): "pw"},
    )
    for stmt in ("CREATE DATABASE ", "CREATE ROLE ", "ALTER ROLE ",
                 "GRANT ALL PRIVILEGES ON DATABASE "):
        assert f"{stmt}appdb" not in sql
        assert f"{stmt}appuser" not in sql
    assert "format('%L'" in sql, "the password must be server-quoted too"


def test_changed_marker_is_emitted_only_for_real_work():
    sql = _strip_comments(_render([_binding("app")], {_vault_key("app"): "pw"}))
    for line in sql.splitlines():
        if "CHANGED:" in line:
            assert line.lstrip().startswith("SELECT 'CHANGED:")
    # Both markers are guarded by a NOT EXISTS on the next line.
    marked = [i for i, l in enumerate(sql.splitlines()) if "CHANGED:" in l]
    assert len(marked) == 2
    lines = sql.splitlines()
    for i in marked:
        assert "WHERE NOT EXISTS" in lines[i + 1]


def test_multiple_bindings_share_one_script():
    sql = _render(
        [_binding("app"), _binding("worker")],
        {_vault_key("app"): "p1", _vault_key("worker"): "p2"},
    )
    assert sql.count("CHANGED: create database ") == 2
    assert sql.count("\\connect :\"bay_db\"") == 2


def test_the_task_file_is_one_exec_per_accessory():
    tasks = yaml.safe_load(_TASKS.read_text())
    block = next(t["block"] for t in tasks if "block" in t)
    execs = [
        t
        for t in block
        if "ansible.builtin.command" in t
        and t["ansible.builtin.command"]["argv"][:2] == ["docker", "exec"]
    ]
    assert len(execs) == 1
    task = execs[0]
    assert task["loop"] == "{{ _db_accessories }}"
    assert "provision-db.sql.j2" in task["ansible.builtin.command"]["stdin"]
    assert "CHANGED:" in task["changed_when"]
    assert task["changed_when"] is not True
    # The script carries every bound role's password.
    assert "no_log" in task
