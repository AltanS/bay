"""Database provisioning runs twice: before and after the container pass.

Since 2.27.0 `roles/deploy_stack/tasks/database_provision.yml` is included
twice from `tasks/main.yml`, with `_dbprov_pass`:

  before  ahead of "Deploy containers". Makes only a binding whose role does
          not exist yet, on an accessory that runs now. Skipped in check mode
          and plan-only mode. A failure fails the deploy.
  after   behind the container pass, for every binding, exactly as before.

The static tests pin the wiring and the Jinja expressions. The Ansible tests
run the real task file against a fake `docker` on PATH, so they need no
container runtime: the fake answers `docker ps`, the read step and the
provision script, and logs which calls came (never a password).

Every password here is an obvious test placeholder.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parent.parent
_TASKS = _REPO_ROOT / "roles" / "deploy_stack" / "tasks" / "database_provision.yml"
_MAIN = _REPO_ROOT / "roles" / "deploy_stack" / "tasks" / "main.yml"
_TEMPLATES = _REPO_ROOT / "roles" / "deploy_stack" / "templates"

_TESTS_DIR = Path(__file__).resolve().parent
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))
sys.path.insert(0, str(_REPO_ROOT / "filter_plugins"))

from bay_filters import bay_db_new_bindings  # noqa: E402
from helpers import make_ansible_env  # noqa: E402


# ── main.yml: where the two includes sit ─────────────────────────────────


def _main_tasks() -> list[dict]:
    tasks = yaml.safe_load(_MAIN.read_text())
    return next(t["block"] for t in tasks if t.get("name") == "Deploy stack")


def _includes() -> dict[str, tuple[int, dict]]:
    found = {}
    for i, task in enumerate(_main_tasks()):
        if task.get("ansible.builtin.include_tasks") == "database_provision.yml":
            found[task["vars"]["_dbprov_pass"]] = (i, task)
    return found


def _index(name: str) -> int:
    return next(i for i, t in enumerate(_main_tasks()) if t.get("name") == name)


def test_the_before_include_sits_ahead_of_the_container_pass():
    includes = _includes()
    assert set(includes) == {"before", "after"}, "one include per pass, no more"
    before, after = includes["before"][0], includes["after"][0]
    containers = _index("Deploy containers")
    assert before < containers < after
    # After the env files, the config files, the backup wait and the restore
    # check: a new app finds everything the container pass would find.
    for name in (
        "Generate service env files",
        "Generate accessory env files",
        "Deploy config files",
        "Wait for active backups to complete before deploy",
        "Refuse to deploy while a live restore holds clients stopped",
    ):
        assert _index(name) < before, name


def test_the_before_include_is_skipped_in_check_and_plan_only_mode():
    when = " ".join(str(c) for c in _includes()["before"][1]["when"])
    assert "not ansible_check_mode" in when
    assert "bay_reconciler_plan_only" in when


def test_the_after_include_has_no_new_condition():
    """The after pass runs as before: no `when`, so check mode predicts there."""
    _, task = _includes()["after"]
    assert "when" not in task


def test_both_includes_keep_the_deploy_stack_tag():
    for _, task in _includes().values():
        assert task["tags"] == ["deploy_stack"]


# ── the task file ────────────────────────────────────────────────────────


def _file_tasks() -> list[dict]:
    return yaml.safe_load(_TASKS.read_text())


def _block() -> dict:
    return next(t for t in _file_tasks() if "block" in t)


def _task(name: str) -> dict:
    return next(t for t in _block()["block"] if t.get("name") == name)


def test_the_pass_defaults_to_after_everywhere():
    """A caller that sets no pass (recovery, older tests) gets today's pass."""
    code = [ln for ln in _TASKS.read_text().splitlines()
            if not ln.lstrip().startswith("#")]
    uses = [ln for ln in code if "_dbprov_pass" in ln]
    assert uses, "fixture broken: no use of the pass variable"
    for line in uses:
        assert "_dbprov_pass | default('after')" in line, line


def test_the_whole_block_is_skipped_in_check_mode_for_the_before_pass():
    when = " ".join(str(c) for c in _block()["when"])
    assert "(_dbprov_pass | default('after')) == 'after'" in when
    assert "ansible_check_mode" in when
    assert "bay_reconciler_plan_only" in when
    assert "_db_bindings | length > 0" in when


def test_the_before_only_steps_are_gated_on_the_before_pass():
    for name in (
        "Find the accessories that run now",
        "Keep only the accessories that run now",
        "Keep only the bindings whose role does not exist yet",
    ):
        assert _task(name)["when"] == "(_dbprov_pass | default('after')) == 'before'", name


def test_the_before_steps_come_between_the_collect_and_the_provision():
    names = [t["name"] for t in _block()["block"]]
    order = [
        "Collect the accessories those bindings target",
        "Find the accessories that run now",
        "Keep only the accessories that run now",
        "Read the stored password form of each bound role",
        "Keep only the bindings whose role does not exist yet",
        "Provision databases, roles and grants",
    ]
    assert [names.index(n) for n in order] == sorted(names.index(n) for n in order)


def test_the_running_check_is_one_read_only_call_that_cannot_fail():
    task = _task("Find the accessories that run now")
    argv = task["ansible.builtin.command"]["argv"]
    assert argv[:2] == ["docker", "ps"]
    assert "status=running" in argv
    assert not {"rm", "stop", "start", "exec", "run"} & set(argv)
    assert task["failed_when"] is False
    assert task["changed_when"] is False
    assert task["check_mode"] is False


def test_the_provision_task_is_the_same_for_both_passes():
    task = _task("Provision databases, roles and grants")
    assert "when" not in task, "no pass condition: the narrowing is in the facts"
    assert "'_db_predict': false" in task["ansible.builtin.command"]["stdin"]
    assert "CHANGED:" in task["changed_when"]
    assert task["no_log"] == "{{ not (provision_db_debug | default(false) | bool) }}"
    assert "failed_when" not in task, "a failure must fail the deploy"


# ── the Jinja expressions, rendered with sample data ─────────────────────


def _render(expr: str, **ctx):
    env = make_ansible_env(_TEMPLATES)
    # Ansible ships the `skipped` test; a bare Jinja Environment does not.
    env.tests["skipped"] = lambda r: isinstance(r, dict) and bool(r.get("skipped"))
    return env.from_string(expr).render(**ctx)


def _binding(key, accessory="postgres", user=None):
    database = {"accessory": accessory}
    if user:
        database["user"] = user
    return {"key": key, "value": {"database": database}}


def _read(accessory, roles=("old",), rc=0, **extra):
    stdout = json.dumps({
        "password_encryption": "scram-sha-256",
        "scram_iterations": "4096",
        "roles": {r: {"kind": "scram", "params": "4096:W22ZaJ0SNY7soEsUEjb6gQ=="} for r in roles},
    })
    return {"item": accessory, "rc": rc, "stdout": stdout if rc == 0 else "", **extra}


def test_running_filter_expression_keeps_only_running_accessories():
    expr = _task("Keep only the accessories that run now")[
        "ansible.builtin.set_fact"]["_db_accessories"]
    running = {"stdout_lines": ["traefik", "pg-a", "other"], "rc": 0}
    for accessories, expected in (
        (["pg-a", "pg-b"], ["pg-a"]),  # pg-b is absent: not an error
        (["pg-b"], []),
        (["pg"], []),  # a name is matched whole, not as a prefix
    ):
        out = _render(expr, _db_accessories=accessories, _db_running=running)
        assert out.strip() == str(expected), (accessories, out)
    # A failed docker call: nothing runs, so the before pass does nothing.
    failed = {"stdout_lines": ["pg-a"], "rc": 1}
    assert _render(expr, _db_accessories=["pg-a"], _db_running=failed).strip() == "[]"
    assert _render(expr, _db_accessories=["pg-a"], _db_running={"skipped": True}).strip() == "[]"


def test_the_role_filter_expression_keeps_absent_roles_only():
    task = _task("Keep only the bindings whose role does not exist yet")
    assert "bay_db_new_bindings" in task["vars"]["_kept"]
    fact = task["ansible.builtin.set_fact"]
    # The two facts the provision task reads are both derived from the filter.
    assert set(fact) == {"_db_bindings", "_db_accessories"}
    assert fact["_db_bindings"] == "{{ _kept }}"
    assert "_kept" in fact["_db_accessories"]
    accessories = _render(
        fact["_db_accessories"].strip(),
        _kept=[_binding("new"), _binding("other", "pg-2")],
    )
    assert accessories.strip() == "['postgres', 'pg-2']"


def test_new_bindings_filter_keeps_a_binding_whose_role_is_absent():
    bindings = [_binding("new"), _binding("old")]
    kept = bay_db_new_bindings(bindings, [_read("postgres", roles=("old",))])
    assert [b["key"] for b in kept] == ["new"]
    assert kept[0] is bindings[0]


def test_new_bindings_filter_reads_the_user_before_the_key():
    bindings = [_binding("svc", user="svc_user"), _binding("svc2", user="old")]
    kept = bay_db_new_bindings(bindings, [_read("postgres", roles=("old", "svc"))])
    # `svc` exists, but the role of this binding is `svc_user`, which does not.
    assert [b["key"] for b in kept] == ["svc"]


def test_new_bindings_filter_proves_absence_per_accessory():
    bindings = [_binding("a", "pg-1"), _binding("b", "pg-2")]
    kept = bay_db_new_bindings(
        bindings, [_read("pg-1", roles=("a",)), _read("pg-2", roles=())]
    )
    assert [b["key"] for b in kept] == ["b"]


@pytest.mark.parametrize(
    "results",
    [
        [],
        [_read("postgres", rc=1)],
        # A failed run that still printed a valid document proves nothing.
        [{**_read("postgres", roles=()), "rc": 2}],
        [{**_read("postgres"), "skipped": True}],
        [{"item": "postgres", "rc": 0, "stdout": ""}],
        [{"item": "postgres", "rc": 0, "stdout": "not json"}],
        [{"item": "postgres", "rc": 0, "stdout": "{}"}],
        [{"item": "postgres", "rc": 0, "stdout": "[]"}],
        [_read("another-accessory", roles=())],
        None,
    ],
)
def test_new_bindings_filter_keeps_nothing_without_a_proof(results):
    """No usable read result: the role is left to the after pass."""
    assert bay_db_new_bindings([_binding("new")], results) == []


def test_the_filter_works_inside_jinja_with_ansible_style_input():
    expr = _task("Keep only the bindings whose role does not exist yet")["vars"]["_kept"]
    out = _render(
        expr,
        _db_bindings=[_binding("new"), _binding("old")],
        _db_read={"results": [_read("postgres", roles=("old",))]},
    )
    assert "'key': 'new'" in out and "'key': 'old'" not in out


# ── the SQL of a narrowed list ───────────────────────────────────────────


def _sql(bindings, observed=""):
    env = make_ansible_env(_TEMPLATES)
    secrets = {f"{b['key'].upper()}_POSTGRES_PASSWORD": f"bay-test-placeholder-{b['key']}"
               for b in bindings}
    return env.get_template("provision-db.sql.j2").render(
        ansible_managed="test", _acc_bindings=bindings, secrets=secrets,
        _acc_observed=observed, _db_predict=False,
    )


def _statements(sql):
    return [ln for ln in sql.splitlines() if ln.strip() and not ln.lstrip().startswith("--")]


def test_the_sql_of_a_narrowed_list_is_what_the_full_pass_makes_for_that_binding():
    new, old = _binding("newapp"), _binding("oldapp")
    observed = _read("postgres", roles=("oldapp",))["stdout"]
    kept = bay_db_new_bindings([new, old], [{"item": "postgres", "rc": 0, "stdout": observed}])
    assert kept == [new]

    before = _statements(_sql(kept, observed))
    full = _statements(_sql([new, old], observed))
    # Every line of the narrowed script is in the full script, in order...
    it = iter(full)
    assert all(line in it for line in before)
    # ...and it holds nothing about the role that exists.
    assert not any("oldapp" in line for line in before)
    assert any("CREATE ROLE %I" in line for line in before)
    assert any("CREATE DATABASE" in line for line in before)
    # A new role has no verifier to compare: the password is set as it is made.
    assert not any("bay_ver" in line for line in before)


def test_a_role_made_by_the_before_pass_prints_no_changed_in_the_after_pass():
    """Read the logic: every CHANGED line has a guard that the first run clears.

    The live proof is in tests/test_database_provision_idempotent.py
    (`test_db_provision_idempotent_postgres`: first run creates, second run
    prints nothing), which runs this very script and read step. Here: the
    three CHANGED guards of a binding, rendered for the state after the
    before pass.
    """
    from bay_filters import bay_scram_verifier

    salt = "W22ZaJ0SNY7soEsUEjb6gQ=="
    observed = _read("postgres", roles=("newapp",))["stdout"]
    sql = _sql([_binding("newapp")], observed)
    lines = sql.splitlines()
    guards = {
        "database": "WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'newapp')",
        "role": "WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'newapp')",
        "password": "rolpassword IS DISTINCT FROM :'bay_ver'",
    }
    for guard in guards.values():
        assert any(guard in ln for ln in lines), guard
    # The password guard compares with the verifier the controller computes
    # from the salt the server stored, so it is equal after a first run.
    ver = next(ln for ln in lines if ln.startswith("\\set bay_ver "))
    assert bay_scram_verifier("bay-test-placeholder-newapp", 4096, salt) in ver


# ── the real task file, with a fake docker ───────────────────────────────

_FAKE_DOCKER = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    import json, os, sys

    log = os.environ["FAKE_DOCKER_LOG"]
    state = json.loads(os.environ["FAKE_DOCKER_STATE"])
    args = sys.argv[1:]

    def note(kind, **kw):
        with open(log, "a") as f:
            f.write(json.dumps({"kind": kind, **kw}) + "\\n")

    if args[:1] == ["ps"]:
        note("ps")
        print("\\n".join(state["running"]))
        sys.exit(0)
    if args[:2] == ["exec", "-i"]:
        accessory = args[2]
        sql = sys.stdin.read()
        if "json_build_object" in sql:
            note("read", accessory=accessory)
            if state.get("read_rc"):
                sys.stderr.write("read failed\\n")
                sys.exit(state["read_rc"])
            print(json.dumps({
                "password_encryption": "scram-sha-256", "scram_iterations": "4096",
                "roles": {r: {"kind": "scram", "params": "4096:W22ZaJ0SNY7soEsUEjb6gQ=="}
                          for r in state["roles"]},
            }))
            sys.exit(0)
        roles = sorted(r for r in state["all_roles"] if "'" + r + "'" in sql)
        note("provision", accessory=accessory, roles=roles,
             predict="default_transaction_read_only" in sql)
        if state.get("provision_rc"):
            sys.stderr.write("provision failed with bay-test-placeholder-secret\\n")
            sys.exit(state["provision_rc"])
        for r in roles:
            if r not in state["roles"]:
                print("CHANGED: create role " + r)
        sys.exit(0)
    note("other", args=args)
    sys.exit(0)
    """
)

_SERVICES = {
    "newapp": {"database": {"accessory": "pg"}},
    "oldapp": {"database": {"accessory": "pg"}},
}


def _run(tmp_path: Path, *, passes, state, check=False, services=None, extra=None):
    """Run the real task file once per pass; return (proc, calls)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(_FAKE_DOCKER)
    docker.chmod(docker.stat().st_mode | stat.S_IXUSR)
    log = tmp_path / "calls.jsonl"
    state = {"all_roles": ["newapp", "oldapp"], "roles": ["oldapp"],
             "running": ["pg"], **state}
    tasks = []
    for pass_name in passes:
        task = {
            "name": f"pass {pass_name}",
            "ansible.builtin.include_role": {
                "name": "deploy_stack", "tasks_from": "database_provision",
            },
        }
        if pass_name != "default":
            task["vars"] = {"_dbprov_pass": pass_name}
        tasks.append(task)
    play = [{
        "name": "Provision through the real task file",
        "hosts": "localhost", "connection": "local", "gather_facts": False,
        "vars": {
            "active_services": services or _SERVICES,
            "active_accessories": {"pg": {"env": {"clear": {}}}},
        },
        "tasks": tasks,
    }]
    (tmp_path / "play.yml").write_text(yaml.safe_dump(play))
    (tmp_path / "vars.json").write_text(json.dumps({
        "secrets": {"NEWAPP_POSTGRES_PASSWORD": "bay-test-placeholder-new",
                    "OLDAPP_POSTGRES_PASSWORD": "bay-test-placeholder-old"},
        **(extra or {}),
    }))
    (tmp_path / "ansible.cfg").write_text(
        "[defaults]\n"
        f"roles_path = {_REPO_ROOT / 'roles'}\n"
        f"filter_plugins = {_REPO_ROOT / 'filter_plugins'}\n"
    )
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "FAKE_DOCKER_LOG": str(log),
        "FAKE_DOCKER_STATE": json.dumps(state),
        "ANSIBLE_CONFIG": str(tmp_path / "ansible.cfg"),
        "ANSIBLE_NOCOLOR": "1",
        "ANSIBLE_LOCALHOST_WARNING": "0",
        "ANSIBLE_INVENTORY_UNPARSED_WARNING": "0",
        "ANSIBLE_PYTHON_INTERPRETER": sys.executable,
    }
    proc = subprocess.run(
        [sys.executable, "-m", "ansible.cli.playbook", "-i", "localhost,",
         "-e", f"@{tmp_path / 'vars.json'}", str(tmp_path / "play.yml"),
         *(["--check"] if check else [])],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=300,
    )
    calls = []
    if log.exists():
        calls = [json.loads(line) for line in log.read_text().splitlines()]
    # no_log censors the task, so no password can reach the output.
    for secret in ("bay-test-placeholder-new", "bay-test-placeholder-old",
                   "bay-test-placeholder-secret"):
        assert secret not in proc.stdout and secret not in proc.stderr
    return proc, calls


def _kinds(calls):
    return [c["kind"] for c in calls]


def test_before_pass_makes_only_the_new_role_on_a_running_accessory(tmp_path):
    proc, calls = _run(tmp_path, passes=["before"], state={})
    assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-2000:]
    assert _kinds(calls) == ["ps", "read", "provision"]
    assert calls[2]["roles"] == ["newapp"], "the role that exists waits for the after pass"
    assert "changed:" in proc.stdout, "the CHANGED line makes the task changed"


def test_before_pass_leaves_a_stopped_accessory_alone_without_failing(tmp_path):
    proc, calls = _run(tmp_path, passes=["before"], state={"running": ["traefik"]})
    assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-2000:]
    assert _kinds(calls) == ["ps"], "no exec into a container that does not run"


def test_before_pass_does_nothing_when_every_role_exists(tmp_path):
    state = {"roles": ["newapp", "oldapp"]}
    proc, calls = _run(tmp_path, passes=["before"], state=state)
    assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-2000:]
    assert _kinds(calls) == ["ps", "read"]


def test_before_pass_does_nothing_when_the_read_fails(tmp_path):
    proc, calls = _run(tmp_path, passes=["before"], state={"read_rc": 1})
    assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-2000:]
    assert _kinds(calls) == ["ps", "read"], "an unproven role is left to the after pass"


def test_before_pass_is_skipped_completely_in_check_mode(tmp_path):
    proc, calls = _run(tmp_path, passes=["before"], state={}, check=True)
    assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-2000:]
    assert calls == [], "not even a read: the whole block is skipped"


def test_before_pass_is_skipped_in_plan_only_mode(tmp_path):
    proc, calls = _run(tmp_path, passes=["before"], state={},
                       extra={"bay_reconciler_plan_only": True})
    assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-2000:]
    assert calls == []


def test_a_failed_before_pass_fails_the_deploy(tmp_path):
    proc, calls = _run(tmp_path, passes=["before", "after"], state={"provision_rc": 3})
    assert proc.returncode != 0
    assert _kinds(calls) == ["ps", "read", "provision"], "nothing ran after the failure"
    assert "failed" in proc.stdout.lower()


def test_the_after_pass_is_unchanged_for_a_caller_without_a_pass(tmp_path):
    """No pass variable, as every caller before 2.27.0: read, then provision all."""
    proc, calls = _run(tmp_path, passes=["default"], state={})
    assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-2000:]
    assert _kinds(calls) == ["read", "provision"], "no docker ps in the after pass"
    assert calls[1]["roles"] == ["newapp", "oldapp"]
    assert calls[1]["predict"] is False


def test_the_after_pass_in_check_mode_still_predicts(tmp_path):
    proc, calls = _run(tmp_path, passes=["after"], state={}, check=True)
    assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-2000:]
    assert _kinds(calls) == ["read", "provision"]
    assert calls[1]["predict"] is True


def test_the_after_pass_sees_the_whole_fleet_after_a_before_pass(tmp_path):
    """The before pass leaves no narrowed fact behind for the after pass."""
    proc, calls = _run(tmp_path, passes=["before", "after"], state={})
    assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-2000:]
    assert _kinds(calls) == ["ps", "read", "provision", "read", "provision"]
    assert calls[2]["roles"] == ["newapp"]
    assert calls[4]["roles"] == ["newapp", "oldapp"]


def test_the_after_pass_runs_even_when_the_before_pass_had_nothing(tmp_path):
    proc, calls = _run(tmp_path, passes=["before", "after"], state={"running": []})
    assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-2000:]
    assert _kinds(calls) == ["ps", "read", "provision"]
    assert calls[2]["roles"] == ["newapp", "oldapp"]
