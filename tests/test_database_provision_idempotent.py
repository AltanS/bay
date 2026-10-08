"""Database provisioning compares role passwords instead of re-setting them.

Before, `ALTER ROLE ... PASSWORD` ran for every existing role on every deploy
and printed `CHANGED`, so "Provision databases, roles and grants" was
`changed` on every run. Now:

  * a read step brings back each role's verifier kind and, for SCRAM, only
    `<iter>:<salt>` (never StoredKey or ServerKey),
  * the controller computes the verifier the password in the encrypted
    secrets file would have with that salt (`bay_scram_verifier`, md5 too),
  * the server compares, and only a difference sets the password and prints
    `CHANGED: set password for role <r>`.

The filter is checked against the RFC 7677 test vector, a verifier Postgres
produced, and (where a container runtime exists) live Postgres servers of the
majors the fleets run. The end-to-end test runs read, provision and predict
against a throwaway container, exactly as the task file wires them.

Every password here is an obvious test placeholder.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parent.parent
_TEMPLATES = _REPO_ROOT / "roles" / "deploy_stack" / "templates"
_TASKS = _REPO_ROOT / "roles" / "deploy_stack" / "tasks" / "database_provision.yml"

_TESTS_DIR = Path(__file__).resolve().parent
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))
sys.path.insert(0, str(_REPO_ROOT / "filter_plugins"))

from bay_filters import (  # noqa: E402
    bay_db_password_verifier,
    bay_md5_verifier,
    bay_scram_verifier,
)
from helpers import make_ansible_env  # noqa: E402

_OLD_PW = "bay-test-placeholder-old"
_NEW_PW = "bay-test-placeholder-new 'quoted' $x"


# ── the filter ───────────────────────────────────────────────────────────

# Public test vectors, not secrets. Each is ASSEMBLED FROM FRAGMENTS, as
# tests/test_leak_scan.py asks: 32 bytes of base64 is the shape the leak
# scan's entropy tier exists to catch.
_RFC_SERVER_SIGNATURE = "6rriTRBi23WpRR/wtup" + "+mMhUZUn/dB5nLTJRsjl95G4="
_RFC_CLIENT_PROOF = "dHzbZapWIk4jUhN+Ute9" + "ytag9zjfMHgsqmmiz7AndVQ="


def test_scram_verifier_rfc7677_vector():
    """RFC 7677 section 3: user "user", password "pencil", 4096 iterations.

    The RFC prints the client proof and the server signature of one exchange,
    not the keys, so the keys are checked through them: ServerSignature =
    HMAC(ServerKey, AuthMessage), and ClientProof XOR HMAC(StoredKey,
    AuthMessage) is a ClientKey whose SHA-256 is StoredKey.
    """
    salt = "W22ZaJ0SNY7soEsUEjb6gQ=="
    verifier = bay_scram_verifier("pencil", 4096, salt)
    head, stored_b64, server_b64 = (
        verifier.split("$")[0],
        verifier.split("$")[2].split(":")[0],
        verifier.split("$")[2].split(":")[1],
    )
    assert head == "SCRAM-SHA-256"
    assert verifier.split("$")[1] == f"4096:{salt}"
    nonce = "rOprNGfwEbeRWgbNEkqO%hvYDpWUa2RaTCAfuxFIlj)hNlF$k0"
    auth = (
        f"n=user,r=rOprNGfwEbeRWgbNEkqO,r={nonce},s={salt},i=4096,"
        f"c=biws,r={nonce}"
    ).encode()
    server_sig = hmac.new(base64.b64decode(server_b64), auth, hashlib.sha256).digest()
    assert base64.b64encode(server_sig).decode() == _RFC_SERVER_SIGNATURE
    stored_key = base64.b64decode(stored_b64)
    client_sig = hmac.new(stored_key, auth, hashlib.sha256).digest()
    proof = base64.b64decode(_RFC_CLIENT_PROOF)
    client_key = bytes(a ^ b for a, b in zip(proof, client_sig, strict=True))
    assert hashlib.sha256(client_key).digest() == stored_key


# Produced by Postgres 18.4: `ALTER ROLE probe PASSWORD 'bay-test-placeholder-old'`,
# then `SELECT rolpassword FROM pg_authid WHERE rolname = 'probe'`.
_PG_VECTOR = {
    "password": _OLD_PW,
    "verifier": (
        "SCRAM-SHA-256$4096:eInE9kSf7xhTESjA0JKDVw=="
        "$" + "K5rkwwrmPuYGxIn9/O9w" + "w7hrkyeiNAXy5ZYpJ/fgrL8="
        ":" + "biqwqG53wckbUlYDp7vH" + "ORXXxBAxPoN2SUhhuhbED3M="
    ),
}


def test_scram_verifier_matches_postgres_known_vector():
    """A verifier a real Postgres stored, recomputed from its own salt."""
    stored = _PG_VECTOR["verifier"]
    iters, salt = stored.split("$")[1].split(":")
    assert bay_scram_verifier(_PG_VECTOR["password"], int(iters), salt) == stored


def test_scram_verifier_refuses_what_it_cannot_reproduce():
    with pytest.raises(ValueError):
        bay_scram_verifier("päss", 4096, "W22ZaJ0SNY7soEsUEjb6gQ==")
    with pytest.raises(ValueError):
        bay_scram_verifier("pencil", 4096, "not base64!")
    with pytest.raises(ValueError):
        bay_scram_verifier("pencil", 0, "W22ZaJ0SNY7soEsUEjb6gQ==")


def test_md5_verifier_is_md5_of_password_then_role():
    expected = "md5" + hashlib.md5(b"pencil" + b"app").hexdigest()
    assert bay_md5_verifier("pencil", "app") == expected


def _observed(kind="scram", params="4096:W22ZaJ0SNY7soEsUEjb6gQ==",
              encryption="scram-sha-256", iterations="4096", role="app"):
    return {
        "password_encryption": encryption,
        "scram_iterations": iterations,
        "roles": {role: {"kind": kind, "params": params}},
    }


def test_password_verifier_compares_only_when_a_set_would_match():
    want = bay_scram_verifier("pencil", 4096, "W22ZaJ0SNY7soEsUEjb6gQ==")
    # A dict and the read step's raw JSON line give the same answer.
    assert bay_db_password_verifier("pencil", "app", _observed()) == want
    assert bay_db_password_verifier("pencil", "app", json.dumps(_observed())) == want
    # Postgres before 16 has no scram_iterations; the count was fixed.
    assert bay_db_password_verifier("pencil", "app", _observed(iterations=None)) == want
    md5 = _observed(kind="md5", params=None, encryption="md5")
    assert bay_db_password_verifier("pencil", "app", md5) == bay_md5_verifier("pencil", "app")


@pytest.mark.parametrize(
    ("password", "observed"),
    [
        ("pencil", ""),  # no read result (failed or skipped)
        ("pencil", "not json"),
        ("pencil", {}),
        ("pencil", _observed(role="other")),  # role does not exist yet
        ("päss", _observed()),  # not ASCII: Postgres applies SASLprep
        ("", _observed()),  # Postgres stores no password for ''
        ("pencil", _observed(kind="null", params=None)),
        ("pencil", _observed(kind="unknown", params=None)),
        ("pencil", _observed(iterations="8192")),  # a set would re-hash
        ("pencil", _observed(encryption="md5")),  # a set would store md5
        ("pencil", _observed(kind="md5", params=None)),  # a set would store SCRAM
        ("pencil", _observed(params="4096")),
        ("pencil", _observed(params="x:W22ZaJ0SNY7soEsUEjb6gQ==")),
        ("pencil", _observed(params="4096:not base64!")),
    ],
)
def test_password_verifier_falls_back_to_setting(password, observed):
    """No safe comparison means "" and the template sets the password."""
    assert bay_db_password_verifier(password, "app", observed) == ""


# ── rendering ────────────────────────────────────────────────────────────


def _binding(key="app"):
    return {"key": key, "value": {"database": {"accessory": "postgres"}}}


def _secrets(password, user="app"):
    return {user.upper().replace("-", "_") + "_POSTGRES_PASSWORD": password}


def _render(template, **kw):
    env = make_ansible_env(_TEMPLATES)
    return env.get_template(template).render(ansible_managed="test", **kw)


def _render_provision(bindings, secrets, observed="", predict=False):
    return _render(
        "provision-db.sql.j2",
        _acc_bindings=bindings,
        secrets=secrets,
        _acc_observed=observed,
        _db_predict=predict,
    )


def _guard_literal(sql, user="app"):
    """The verifier the CHANGED line compares the stored one with."""
    lines = sql.splitlines()
    i = next(
        n for n, line in enumerate(lines) if "CHANGED: set password for role" in line
    )
    guard = lines[i + 1]
    marker = "rolpassword IS DISTINCT FROM '"
    assert f"rolname = '{user}'" in guard
    assert marker in guard, guard
    return guard.split(marker, 1)[1].split("'", 1)[0]


def _box(password, salt="W22ZaJ0SNY7soEsUEjb6gQ=="):
    """What the box holds after `ALTER ROLE app PASSWORD <password>`."""
    stored = bay_scram_verifier(password, 4096, salt)
    return stored, _observed(params=f"4096:{salt}")


def test_db_provision_idempotent_render_unchanged_password():
    """Same password: the guard compares with exactly the stored verifier.

    `stored IS DISTINCT FROM stored` is false, so the server prints no
    CHANGED line and runs no ALTER ROLE. Both sit behind the same guard.
    """
    stored, observed = _box(_OLD_PW)
    sql = _render_provision([_binding()], _secrets(_OLD_PW), observed)
    assert _guard_literal(sql) == stored
    body = sql.split("DO $bay$", 1)[1].split("$bay$;", 1)[0]
    alter = body.index("'ALTER ROLE '")
    guard = body.rindex("IF ", 0, alter)
    assert f"rolpassword IS DISTINCT FROM '{stored}'" in body[guard:alter]
    # The read step's salt is reused; the keys come from the controller.
    assert stored.split("$")[1] == observed["roles"]["app"]["params"]


def test_db_provision_idempotent_render_changed_password():
    """New password: the guard's verifier differs from the stored one."""
    stored, observed = _box(_OLD_PW)
    sql = _render_provision([_binding()], _secrets(_NEW_PW), observed)
    literal = _guard_literal(sql)
    assert literal != stored
    assert literal == bay_scram_verifier(_NEW_PW, 4096, "W22ZaJ0SNY7soEsUEjb6gQ==")
    assert "'ALTER ROLE '" in sql
    assert "CHANGED: set password for role" in sql


def test_db_provision_idempotent_render_falls_back_without_a_read():
    """No read result: the old guard, so the password is set as before."""
    sql = "\n".join(
        line
        for line in _render_provision([_binding()], _secrets(_OLD_PW)).splitlines()
        if not line.lstrip().startswith("--")
    )
    assert "IS DISTINCT FROM" not in sql
    assert "pg_authid" not in sql, "pg_authid is only used when the read worked"
    lines = sql.splitlines()
    i = next(n for n, line in enumerate(lines) if "CHANGED: set password" in line)
    assert "WHERE EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app')" in lines[i + 1]


def test_db_provision_idempotent_render_predict_mode_is_read_only():
    stored, observed = _box(_OLD_PW)
    sql = _render_provision([_binding()], _secrets(_NEW_PW), observed, predict=True)
    body = "\n".join(
        line for line in sql.splitlines() if not line.lstrip().startswith("--")
    )
    assert "SET default_transaction_read_only = on;" in body
    assert body.index("default_transaction_read_only") < body.index("SELECT")
    for word in ("DO $bay$", "\\gexec", "\\connect", "ALTER ", "CREATE ",
                 "GRANT ", "format('%L'"):
        assert word not in body, word
    # The password itself is not in the predict script, only the verifier.
    assert _NEW_PW.replace("'", "''") not in body
    assert body.count("SELECT 'CHANGED:") == 3


def test_db_provision_idempotent_read_step_returns_no_keys():
    sql = _render("read-db-roles.sql.j2", _acc_bindings=[_binding(), _binding("w")])
    body = "\n".join(
        line for line in sql.splitlines() if not line.lstrip().startswith("--")
    )
    assert "SET default_transaction_read_only = on;" in body
    assert "split_part(rolpassword, '$', 2)" in body
    # rolpassword is only ever tested or split, never selected whole.
    for line in body.splitlines():
        if "rolpassword" in line:
            assert ("IS NULL" in line or "LIKE" in line or "~" in line
                    or "split_part(rolpassword, '$', 2)" in line), line
    assert "'app'" in body and "'w'" in body


# ── the task file ────────────────────────────────────────────────────────


def _block_tasks():
    tasks = yaml.safe_load(_TASKS.read_text())
    return next(t["block"] for t in tasks if "block" in t)


def _task(name):
    return next(t for t in _block_tasks() if t.get("name") == name)


def test_db_provision_idempotent_task_wiring():
    read = _task("Read the stored password form of each bound role")
    assert "read-db-roles.sql.j2" in read["ansible.builtin.command"]["stdin"]
    assert read["check_mode"] is False, "the read must run in check mode too"
    assert read["changed_when"] is False
    assert read["failed_when"] is False
    assert read["register"] == "_db_read"

    predict = _task("Predict database, role and password changes")
    assert predict["when"] == "ansible_check_mode"
    assert predict["check_mode"] is False
    assert "'_db_predict': true" in predict["ansible.builtin.command"]["stdin"]

    provision = _task("Provision databases, roles and grants")
    assert "check_mode" not in provision, "the real write stays skipped in check mode"
    assert "'_db_predict': false" in provision["ansible.builtin.command"]["stdin"]

    for task in (predict, provision):
        # A command skipped by check mode registers rc 0 and an empty stdout;
        # `is not skipped` is the guard that holds.
        assert "is not skipped" in task["vars"]["_acc_observed"]
        assert "_acc_observed" in task["ansible.builtin.command"]["stdin"]
    for task in (read, predict, provision):
        assert task["no_log"] == "{{ not (provision_db_debug | default(false) | bool) }}"


# ── real Postgres ────────────────────────────────────────────────────────
#
# A throwaway container per image, no published port and no network: every
# call goes through `<runtime> exec`, as the deploy does. Removed at the end,
# also on failure. Skipped when no container runtime answers.
# BAY_TEST_PG_IMAGES overrides the images (comma separated).

_IMAGES = [
    i.strip()
    for i in os.environ.get(
        "BAY_TEST_PG_IMAGES", "postgres:17-alpine,postgres:18-alpine"
    ).split(",")
    if i.strip()
]


def _runtime() -> str | None:
    for name in ("docker", "podman"):
        if shutil.which(name) is None:
            continue
        try:
            r = subprocess.run([name, "info"], capture_output=True, timeout=10, check=False)
        except (subprocess.TimeoutExpired, OSError):
            continue
        if r.returncode == 0:
            return name
    return None


_RUNTIME = _runtime()
_needs_runtime = pytest.mark.skipif(
    _RUNTIME is None, reason="no container runtime (docker or podman) on this host"
)


class _Pg:
    def __init__(self, runtime: str, name: str):
        self.runtime = runtime
        self.name = name

    def psql(self, sql: str, *args: str, user: str = "postgres",
             env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
        cmd = [self.runtime, "exec", "-i"]
        for key, value in (env or {}).items():
            cmd += ["-e", f"{key}={value}"]
        cmd += [self.name, "psql", "-q", *args, "-U", user]
        return subprocess.run(cmd, input=sql, capture_output=True, text=True,
                              timeout=60, check=False)

    def scalar(self, sql: str) -> str:
        r = self.psql(sql, "-At")
        assert r.returncode == 0, r.stderr
        return r.stdout.strip()

    def login(self, user: str, password: str, db: str) -> bool:
        """Password login over TCP inside the container (scram in pg_hba)."""
        r = self.psql("SELECT 1;", "-At", "-h", "127.0.0.1", "-d", db, user=user,
                      env={"PGPASSWORD": password})
        return r.returncode == 0 and r.stdout.strip() == "1"


@pytest.fixture(scope="module", params=_IMAGES)
def pg(request):
    if _RUNTIME is None:
        pytest.skip("no container runtime (docker or podman) on this host")
    image = request.param
    name = f"bay-m120-pg-{uuid.uuid4().hex[:10]}"
    run = subprocess.run(
        [_RUNTIME, "run", "-d", "--name", name, "--network", "none",
         "-e", "POSTGRES_PASSWORD=bay-test-placeholder-admin", image],
        capture_output=True, text=True, timeout=600, check=False,
    )
    try:
        if run.returncode != 0:
            pytest.skip(f"cannot start {image}: {run.stderr.strip()[:200]}")
        handle = _Pg(_RUNTIME, name)
        deadline = time.time() + 90
        while time.time() < deadline:
            # The image's init server listens on the socket only; TCP up
            # means the real server runs.
            ready = subprocess.run(
                [_RUNTIME, "exec", name, "pg_isready", "-h", "127.0.0.1", "-U", "postgres"],
                capture_output=True, timeout=10, check=False,
            )
            if ready.returncode == 0:
                break
            time.sleep(0.5)
        else:
            pytest.fail(f"{image} did not become ready")
        # The image trusts loopback TCP, which would make every login test
        # pass. Put a scram rule first; admin calls use the socket.
        hba = handle.scalar("SHOW hba_file;")
        subprocess.run(
            [_RUNTIME, "exec", name, "sh", "-c",
             'sed -i "1i host all all 127.0.0.1/32 scram-sha-256" "$1"', "sh", hba],
            capture_output=True, timeout=30, check=True,
        )
        handle.scalar("SELECT pg_reload_conf();")
        time.sleep(0.5)
        yield handle
    finally:
        subprocess.run([_RUNTIME, "rm", "-f", "-v", name], capture_output=True,
                       timeout=60, check=False)


@_needs_runtime
def test_scram_verifier_matches_postgres(pg):
    pg.scalar("DROP ROLE IF EXISTS probe; CREATE ROLE probe;")
    for password in (_OLD_PW, _NEW_PW, "x"):
        quoted = password.replace("'", "''")
        pg.scalar(f"ALTER ROLE probe PASSWORD '{quoted}';")
        stored = pg.scalar("SELECT rolpassword FROM pg_authid WHERE rolname = 'probe';")
        assert stored.startswith("SCRAM-SHA-256$"), stored
        iters, salt = stored.split("$")[1].split(":")
        assert bay_scram_verifier(password, int(iters), salt) == stored
    # md5, where the server still writes it (deprecated, not removed, in 18).
    pg.scalar(f"SET password_encryption = 'md5'; ALTER ROLE probe PASSWORD '{_OLD_PW}';")
    stored = pg.scalar("SELECT rolpassword FROM pg_authid WHERE rolname = 'probe';")
    assert stored == bay_md5_verifier(_OLD_PW, "probe")
    pg.scalar("DROP ROLE probe;")


def _deploy(pg: _Pg, secrets: dict, *, predict=False, read=True) -> str:
    """One run of the task file's steps for one accessory, as Ansible wires them."""
    bindings = [_binding("app")]
    observed = ""
    if read:
        r = pg.psql(_render("read-db-roles.sql.j2", _acc_bindings=bindings))
        assert r.returncode == 0, r.stderr
        observed = r.stdout.strip()
        parsed = json.loads(observed)
        for entry in parsed["roles"].values():
            # Only `<iter>:<salt>` crosses; never StoredKey or ServerKey.
            if entry["params"] is not None:
                assert "$" not in entry["params"]
                assert entry["params"].count(":") == 1
    sql = _render_provision(bindings, secrets, observed, predict=predict)
    r = pg.psql(sql)
    assert r.returncode == 0, r.stderr
    return r.stdout


@_needs_runtime
def test_db_provision_idempotent_postgres(pg):
    stored = "SELECT rolpassword FROM pg_authid WHERE rolname = 'app';"
    pg.scalar("DROP DATABASE IF EXISTS app; DROP ROLE IF EXISTS app;")

    first = _deploy(pg, _secrets(_OLD_PW))
    assert "CHANGED: create database app" in first
    assert "CHANGED: create role app" in first
    assert "CHANGED: set password" not in first
    assert pg.login("app", _OLD_PW, "app")
    assert not pg.login("app", "bay-test-placeholder-wrong", "app"), (
        "fixture broken: the server does not check passwords"
    )
    before = pg.scalar(stored)

    # Second run, same password: nothing to report, and nothing was written
    # (a set would have drawn a new random salt).
    assert "CHANGED" not in _deploy(pg, _secrets(_OLD_PW))
    assert pg.scalar(stored) == before
    assert "CHANGED" not in _deploy(pg, _secrets(_OLD_PW), predict=True)

    # A rotation: predict sees it and changes nothing...
    predicted = _deploy(pg, _secrets(_NEW_PW), predict=True)
    assert predicted.strip() == "CHANGED: set password for role app"
    assert pg.scalar(stored) == before
    assert pg.login("app", _OLD_PW, "app")

    # ...the third run sets it, and the new password logs in.
    third = _deploy(pg, _secrets(_NEW_PW))
    assert third.strip() == "CHANGED: set password for role app"
    assert pg.scalar(stored) != before
    assert pg.login("app", _NEW_PW, "app")
    assert not pg.login("app", _OLD_PW, "app")
    assert "CHANGED" not in _deploy(pg, _secrets(_NEW_PW))

    # A failed read falls back to the old behaviour: set and report.
    fallback = _deploy(pg, _secrets(_NEW_PW), read=False)
    assert fallback.strip() == "CHANGED: set password for role app"
    assert pg.login("app", _NEW_PW, "app")

    pg.scalar("DROP DATABASE app; DROP OWNED BY app; DROP ROLE app;")


def _ansible_statuses(tmp_path: Path, pg: _Pg, password: str, check: bool) -> dict[str, str]:
    """Run the real task file through ansible-playbook; first status per task."""
    play = [{
        "name": "Provision through the real task file",
        "hosts": "localhost", "connection": "local", "gather_facts": False,
        "vars": {
            "active_services": {"svc": {"database": {"accessory": pg.name}}},
            "active_accessories": {pg.name: {"env": {"clear": {}}}},
        },
        "tasks": [{
            "name": "Run the provisioning tasks",
            "ansible.builtin.include_role": {
                "name": "deploy_stack", "tasks_from": "database_provision",
            },
        }],
    }]
    (tmp_path / "play.yml").write_text(yaml.safe_dump(play))
    (tmp_path / "vars.json").write_text(
        json.dumps({"secrets": {"SVC_POSTGRES_PASSWORD": password}})
    )
    (tmp_path / "ansible.cfg").write_text(
        "[defaults]\n"
        f"roles_path = {_REPO_ROOT / 'roles'}\n"
        f"filter_plugins = {_REPO_ROOT / 'filter_plugins'}\n"
    )
    env = {
        **os.environ,
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
    assert proc.returncode == 0, proc.stdout[-3000:] + proc.stderr[-3000:]
    # no_log censors every item, so the output cannot carry a password.
    assert password not in proc.stdout and password not in proc.stderr
    statuses: dict[str, str] = {}
    task = None
    for line in proc.stdout.splitlines():
        if line.startswith("TASK [deploy_stack : "):
            task = line.split(" : ", 1)[1].split("]", 1)[0]
        elif task and task not in statuses:
            for word in ("changed", "ok", "skipping"):
                if line.startswith(f"{word}:"):
                    statuses[task] = word
    return statuses


@_needs_runtime
def test_db_provision_idempotent_through_ansible(pg, tmp_path):
    """The task file as Ansible runs it, check mode included."""
    if pg.runtime != "docker":
        pytest.skip("the task file calls docker")
    read = "Read the stored password form of each bound role"
    predict = "Predict database, role and password changes"
    provision = "Provision databases, roles and grants"
    pg.scalar("DROP DATABASE IF EXISTS svc; DROP ROLE IF EXISTS svc;")

    def run(name, password, check=False):
        d = tmp_path / name
        d.mkdir()
        return _ansible_statuses(d, pg, password, check)

    s = run("check-new", _OLD_PW, check=True)
    assert (s[read], s[predict], s[provision]) == ("ok", "changed", "skipping")
    assert pg.scalar("SELECT count(*) FROM pg_roles WHERE rolname = 'svc';") == "0"

    s = run("first", _OLD_PW)
    assert (s[read], s[predict], s[provision]) == ("ok", "skipping", "changed")
    s = run("second", _OLD_PW)
    assert (s[read], s[predict], s[provision]) == ("ok", "skipping", "ok")
    s = run("check-same", _OLD_PW, check=True)
    assert (s[predict], s[provision]) == ("ok", "skipping")
    s = run("check-rotate", _NEW_PW, check=True)
    assert (s[predict], s[provision]) == ("changed", "skipping")
    assert pg.login("svc", _OLD_PW, "svc"), "check mode changed the password"
    s = run("rotate", _NEW_PW)
    assert s[provision] == "changed"
    assert pg.login("svc", _NEW_PW, "svc")
    s = run("after-rotate", _NEW_PW)
    assert s[provision] == "ok"

    pg.scalar("DROP DATABASE svc; DROP ROLE svc;")
