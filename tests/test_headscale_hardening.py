"""Headscale hardening: IP allocation, container spec, file modes, DB backup.

Four changes, each pinned here:

1. `headscale_ip_allocation` feeds `prefixes.allocation` (default `random`,
   only `random` or `sequential` accepted). With `sequential`, Headscale gives
   a deleted node's IP, and with it every ACL grant on that IP, to the next
   node. `tailscale_register` stops a deploy that registers a host whose IP is
   not `headscale_server_tailnet_ip`, because random allocation no longer
   gives a fresh control host 100.64.0.1.
2. The Headscale backup no longer copies the live WAL-mode DB. A root helper
   runs `sqlite3 .backup`, checks the copy with `PRAGMA integrity_check`, and
   prints a tar of the copy plus the noise/DERP keys. The helper is run for
   real against a WAL database here (skipped without a sqlite3 binary).
3. The container is read-only with a tmpfs for its unix socket, config `:ro`,
   every capability dropped except DAC_OVERRIDE, and no-new-privileges. The
   reconciler carries the three new keys (`read_only`, `tmpfs`, `cap_add`)
   from the spec to the Docker SDK call.
4. Data folder 0700, config folder 0750, DB files and copies 0600, fixed on
   existing installs, never deleted.

The container behaviour itself (that Headscale starts and its CLI works with
these flags) needs a Docker daemon and was proven by hand against
headscale/headscale:0.29.7; see the CHANGELOG entry.
"""

from __future__ import annotations

import fnmatch
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import tarfile
from pathlib import Path
from typing import Any

import pytest
import yaml
from helpers import make_ansible_env
from jinja2 import Environment

from bay_reconcile.bundle import spec_from_dict
from bay_reconcile.models import ContainerSpec, tmpfs_to_sdk
from bay_reconcile.sdk_client import SdkDockerClient

REPO = Path(__file__).resolve().parent.parent
HEADSCALE = REPO / "roles" / "headscale"
BACKUP = REPO / "roles" / "backup"
BUILD_SPECS = REPO / "roles" / "container_lifecycle" / "tasks" / "build_specs.yml"
RECONCILE = REPO / "roles" / "container_lifecycle" / "tasks" / "reconcile.yml"
COMPOSE = REPO / "roles" / "deploy_stack" / "templates" / "_headscale.j2"
REGISTER = REPO / "roles" / "tailscale_register" / "tasks" / "main.yml"

_filter_dir = str(REPO / "filter_plugins")
if _filter_dir not in sys.path:
    sys.path.insert(0, _filter_dir)

from bay_filters import bay_spec_hash  # noqa: E402


def _yaml(path: Path) -> Any:
    return yaml.safe_load(path.read_text())


def _task(tasks: list[dict], name: str) -> dict:
    for task in tasks:
        if task.get("name") == name:
            return task
    raise AssertionError(f"no task named {name!r}")


# ── 1. IP allocation ─────────────────────────────────────────────────────


def _config_ctx(**overrides: Any) -> dict:
    base = {
        "ansible_managed": "test",
        "headscale_domain": "hs.example.com",
        "headscale_tailnet_cidr": ["100.64.0.0/10"],
        "headscale_magic_dns_domain": "test.tailnet.internal",
        "headscale_server_tailnet_ip": "100.64.0.1",
        "groups": {"all": ["control"]},
        "inventory_hostname": "control",
        "hostvars": {"control": {"region": "", "services": {}}},
        "tailnet_proxies": {},
        "headscale_extra_dns_records": [],
        "headscale_oidc_issuer": "",
    }
    base.update(overrides)
    return base


def _allocation(**overrides: Any) -> str:
    env = make_ansible_env(HEADSCALE / "templates")
    rendered = env.get_template("config.yaml.j2").render(**_config_ctx(**overrides))
    return yaml.safe_load(rendered)["prefixes"]["allocation"]


def test_role_default_is_random():
    assert _yaml(HEADSCALE / "defaults" / "main.yml")["headscale_ip_allocation"] == "random"


@pytest.mark.parametrize("value", ["random", "sequential"])
def test_config_renders_the_variable(value):
    assert _allocation(headscale_ip_allocation=value) == value


def test_config_without_the_variable_renders_random():
    assert _allocation() == "random"


def _assert_holds(task: dict, **variables: Any) -> bool:
    env = Environment()
    return all(
        env.compile_expression(expr)(**variables) for expr in task["ansible.builtin.assert"]["that"]
    )


@pytest.mark.parametrize(
    ("value", "ok"),
    [("random", True), ("sequential", True), ("Random", False), ("", False), ("linear", False)],
)
def test_validation_accepts_only_the_two_strategies(value, ok):
    task = _task(_yaml(HEADSCALE / "tasks" / "main.yml"), "Validate headscale_ip_allocation")
    assert "headscale" in task["tags"]
    assert _assert_holds(task, headscale_ip_allocation=value) is ok


def test_validation_runs_before_the_config_is_rendered():
    names = [t.get("name") for t in _yaml(HEADSCALE / "tasks" / "main.yml")]
    validate = names.index("Validate headscale_ip_allocation")
    assert validate < names.index("Template headscale config")


def test_register_check_runs_on_both_paths():
    """`headscale_server_tailnet_ip` is THIS host's own tailnet IP, set per region.

    It feeds this host's gateway_bind_ip and, on the control host, the split-DNS
    record of each service (hostvars[h].headscale_server_tailnet_ip of the host
    that runs it). So a remote box (headscale_server false) is checked against
    its own value too; nothing in the guard may depend on headscale_server.
    """
    tasks = _yaml(REGISTER)
    for name in ("Read this host's tailnet IPv4 after registration",
                 "Check headscale_server_tailnet_ip against the registered IP"):
        assert "headscale_server" not in str(_task(tasks, name)["when"]).replace(
            "headscale_server_tailnet_ip", ""
        )
    config = (HEADSCALE / "templates" / "config.yaml.j2").read_text()
    assert "h['headscale_server_tailnet_ip']" in config
    gateway = (REPO / "roles" / "access_gateway" / "defaults" / "main.yml").read_text()
    assert "{{ (headscale_server_tailnet_ip | default(''))" in gateway


def test_register_checks_the_ip_only_after_a_registration():
    tasks = _yaml(REGISTER)
    read = _task(tasks, "Read this host's tailnet IPv4 after registration")
    check = _task(tasks, "Check headscale_server_tailnet_ip against the registered IP")
    assert "_needs_registration" in str(read["when"])
    assert "_needs_registration" in str(check["when"])
    assert "is not skipped" in str(check["when"])
    names = [t.get("name") for t in tasks]
    assert names.index("Connect tailscale to headscale (API path)") < names.index(
        "Read this host's tailnet IPv4 after registration"
    )
    that = check["ansible.builtin.assert"]["that"][0]
    env = Environment()
    expr = env.compile_expression(that.replace(" | string", "").replace(" | trim", "")
                                  .replace(" | default('100.64.0.1')", ""))
    assert expr(_tailscale_ip_after_register={"stdout": "100.64.0.1"},
                headscale_server_tailnet_ip="100.64.0.1")
    assert not expr(_tailscale_ip_after_register={"stdout": "100.97.12.3"},
                    headscale_server_tailnet_ip="100.64.0.1")


# ── 3. container spec ────────────────────────────────────────────────────


def _headscale_spec() -> dict:
    task = _task(_yaml(BUILD_SPECS), "Build headscale container spec")
    return task["vars"]["_headscale_spec"]


def test_spec_is_hardened():
    spec = _headscale_spec()
    assert spec["read_only"] is True
    assert spec["tmpfs"] == ["/var/run/headscale"]
    assert spec["cap_drop"] == ["ALL"]
    # The one capability Headscale needs: root in the container must read and
    # write the app_user-owned config and data folders.
    assert spec["cap_add"] == ["DAC_OVERRIDE"]
    assert spec["security_opt"] == ["no-new-privileges:true"]
    assert "user" not in spec  # still root, as the image's default user


def test_spec_mounts_config_read_only_and_data_writable():
    assert _headscale_spec()["volumes"] == [
        "/opt/headscale/config:/etc/headscale:ro",
        "/opt/headscale/data:/var/lib/headscale",
    ]


def test_compose_documentation_matches_the_spec():
    text = COMPOSE.read_text()
    for line in (
        "read_only: true",
        "- /var/run/headscale",
        "- ALL",
        "- DAC_OVERRIDE",
        "- no-new-privileges:true",
        "- /opt/headscale/config:/etc/headscale:ro",
        "- /opt/headscale/data:/var/lib/headscale\n",
    ):
        assert line in text, line


def test_reconcile_bundle_dict_carries_the_new_keys():
    text = RECONCILE.read_text()
    for key in ("cap_add", "read_only", "tmpfs"):
        assert f"'{key}': _spec.{key} | default(None)" in text


# The 2.13.0 golden of test_egress.py: a spec without the new keys hashes as before,
# so no other container is recreated by this change.
_SPEC_PLAIN = {
    "name": "steel",
    "type": "service",
    "image": "ghcr.io/steel-dev/steel-browser:latest",
    "restart_policy": "unless-stopped",
    "networks": ["services"],
    "labels": {"a": "b"},
    "mem_limit": "512m",
    "memswap_limit": "512m",
    "env_file": "/opt/x/env/steel.env",
}
_GOLDEN_PLAIN = "62aad7dfe5cf0c086841392e"


def test_hash_of_a_spec_without_the_new_keys_is_unchanged():
    assert bay_spec_hash(_SPEC_PLAIN, env_digest="abc").startswith(_GOLDEN_PLAIN)


def test_hash_changes_with_each_new_key():
    plain = bay_spec_hash(_SPEC_PLAIN, env_digest="abc")
    variants = [
        {**_SPEC_PLAIN, "read_only": True},
        {**_SPEC_PLAIN, "tmpfs": ["/var/run/headscale"]},
        {**_SPEC_PLAIN, "cap_add": ["DAC_OVERRIDE"]},
    ]
    hashes = {bay_spec_hash(v, env_digest="abc") for v in variants}
    assert plain not in hashes and len(hashes) == len(variants)


# Literals computed on main (v2.17.3, before this change) with the same calls.
# 24-hex prefixes, as in test_egress.py: the leak scan refuses a full digest.
# The config hash is `_spec | bay_spec_hash` over the RAW spec of build_specs.yml
# (reconcile.yml), never over the bundle entry, whose new keys default to None.
# A spec that does not set a key does not have it, so it hashes as before.
_MAIN_WATCHTOWER = "9ab579884738482b4271611b"
_MAIN_PLAIN = "62aad7dfe5cf0c086841392e"
_MAIN_HEADSCALE = "395acdb536bddf5e516f1156"


def _raw_spec(task: str, key: str) -> dict:
    return _task(_yaml(BUILD_SPECS), task)["vars"][key]


def test_hash_is_computed_from_the_raw_spec():
    assert "'config_hash': (_spec | bay_spec_hash(env_digest=_env_digest))" in RECONCILE.read_text()


def test_unchanged_specs_hash_as_on_main():
    assert bay_spec_hash(_SPEC_PLAIN, env_digest="abc").startswith(_MAIN_PLAIN)
    watchtower = _raw_spec("Build watchtower container spec", "_watchtower_spec")
    assert bay_spec_hash(watchtower).startswith(_MAIN_WATCHTOWER)


def test_only_the_headscale_spec_hash_moves():
    """The one intended recreate of the upgrade."""
    headscale = _raw_spec("Build headscale container spec", "_headscale_spec")
    assert not bay_spec_hash(headscale).startswith(_MAIN_HEADSCALE)


def _entry(**over: Any) -> dict[str, Any]:
    return {"name": "headscale", "image": "headscale/headscale:0.29.7", "type": "infra",
            "config_hash": "abc", **over}


def test_bundle_parses_the_new_keys():
    spec = spec_from_dict(_entry(
        read_only=True, tmpfs=["/var/run/headscale", "/tmp:size=1m"],
        cap_drop=["ALL"], cap_add=["DAC_OVERRIDE"],
    ))
    assert spec.read_only is True
    assert spec.tmpfs == ("/var/run/headscale", "/tmp:size=1m")
    assert spec.cap_add == ("DAC_OVERRIDE",)


def test_bundle_without_the_keys_is_unhardened():
    spec = spec_from_dict(_entry(read_only=None, tmpfs=None, cap_add=None))
    assert spec.read_only is False and spec.tmpfs == () and spec.cap_add == ()


@pytest.mark.parametrize(
    "bad",
    [
        {"read_only": "false"},
        {"read_only": 1},
        {"tmpfs": "/var/run/headscale"},
        {"tmpfs": ["var/run/headscale"]},
        {"cap_add": "DAC_OVERRIDE"},
    ],
)
def test_bundle_refuses_malformed_keys(bad):
    with pytest.raises(ValueError):
        spec_from_dict(_entry(**bad))


def test_tmpfs_to_sdk():
    assert tmpfs_to_sdk(("/var/run/headscale", "/tmp:size=1m,mode=1777")) == {
        "/var/run/headscale": "",
        "/tmp": "size=1m,mode=1777",
    }


class _Containers:
    def __init__(self) -> None:
        self.kwargs: dict[str, Any] = {}

    def run(self, **kwargs: Any) -> None:
        self.kwargs = kwargs


class _Fake:
    def __init__(self) -> None:
        self.containers = _Containers()


def _client() -> tuple[SdkDockerClient, _Fake]:
    client = SdkDockerClient.__new__(SdkDockerClient)
    fake = _Fake()
    client._c = fake
    client._managed_label = "bay.managed"
    client._stack_label = "bay.stack"
    client._stack = "test"
    return client, fake


def test_sdk_create_passes_the_hardening():
    client, fake = _client()
    client.create(ContainerSpec(
        name="headscale", image="headscale/headscale:0.29.7", type="infra", config_hash="h",
        networks=("services",), volumes=("/opt/headscale/config:/etc/headscale:ro",),
        read_only=True, tmpfs=("/var/run/headscale",), cap_drop=("ALL",),
        cap_add=("DAC_OVERRIDE",), security_opt=("no-new-privileges:true",),
    ))
    kw = fake.containers.kwargs
    assert kw["read_only"] is True
    assert kw["tmpfs"] == {"/var/run/headscale": ""}
    assert kw["cap_drop"] == ["ALL"]
    assert kw["cap_add"] == ["DAC_OVERRIDE"]
    assert kw["security_opt"] == ["no-new-privileges:true"]
    assert kw["volumes"] == ["/opt/headscale/config:/etc/headscale:ro"]


def test_sdk_create_of_a_plain_spec_has_none_of_it():
    """Control: every other container is created exactly as before."""
    client, fake = _client()
    client.create(ContainerSpec(name="x", image="x:1", type="service", config_hash="h",
                                networks=("services",)))
    assert not {"read_only", "tmpfs", "cap_add", "cap_drop", "security_opt"} & set(
        fake.containers.kwargs
    )


# ── 4. file modes ────────────────────────────────────────────────────────


def test_folder_modes():
    tasks = _yaml(HEADSCALE / "tasks" / "main.yml")
    config = _task(tasks, "Create headscale config directory")["ansible.builtin.file"]
    data = _task(tasks, "Create headscale data directory")["ansible.builtin.file"]
    assert config["mode"] == "0750"
    assert data["mode"] == "0700"
    # Ownership is unchanged: the role's template tasks run as app_user.
    assert config["owner"] == data["owner"] == "{{ app_user }}"


def test_modes_and_config_are_in_place_before_the_hardened_container_starts():
    """The role recreates the container last, so it starts on the new config and modes."""
    names = [t.get("name") for t in _yaml(HEADSCALE / "tasks" / "main.yml")]
    reconcile = names.index("Bring headscale onto the pinned image")
    for before in ("Validate headscale_ip_allocation", "Create headscale config directory",
                   "Create headscale data directory", "Find headscale database files and copies",
                   "Restrict headscale database files to root (0600)", "Template headscale config",
                   "Install validated headscale ACL policy"):
        assert names.index(before) < reconcile, before


def test_config_file_stays_0640():
    task = _task(_yaml(HEADSCALE / "tasks" / "main.yml"), "Template headscale config")
    assert task["ansible.builtin.template"]["mode"] == "0640"


def test_db_files_and_copies_are_made_0600_and_never_deleted():
    tasks = _yaml(HEADSCALE / "tasks" / "main.yml")
    find = _task(tasks, "Find headscale database files and copies")["ansible.builtin.find"]
    assert set(find["paths"]) == {"/opt/headscale/data", "/opt/headscale/backups"}
    names = ["db.sqlite", "db.sqlite-wal", "db.sqlite-shm", "db.sqlite.bak-20261104T001132Z",
             "db.sqlite.pre-v0292-20260726"]
    for name in names:
        assert any(fnmatch.fnmatch(name, p) for p in find["patterns"]), name
    for name in ("noise_private.key", "derp_server_private.key", "config.yaml"):
        assert not any(fnmatch.fnmatch(name, p) for p in find["patterns"]), name
    fix = _task(tasks, "Restrict headscale database files to root (0600)")
    assert fix["ansible.builtin.file"]["mode"] == "0600"
    assert "state" not in fix["ansible.builtin.file"]
    assert fix["become_user"] == "root"
    # No task of the role removes anything under /opt/headscale.
    for task in tasks:
        mod = task.get("ansible.builtin.file") or {}
        if mod.get("state") == "absent":
            assert ".policy.hujson.staged" in str(mod.get("path")), task["name"]


def test_sqlite_identity_source_is_refused():
    task = _task(_yaml(HEADSCALE / "tasks" / "main.yml"),
                 "Refuse the tailnet-identity sqlite source on a hardened data folder")
    env = Environment()
    expr = env.compile_expression(task["ansible.builtin.assert"]["that"][0]
                                  .replace(" | default('api')", ""))
    assert expr(tailnet_identity_source="api")
    assert not expr(tailnet_identity_source="sqlite")


# ── 2. backup ────────────────────────────────────────────────────────────


def test_backup_entry_asks_for_the_snapshot():
    task = _task(_yaml(BACKUP / "tasks" / "main.yml"),
                 "Register Headscale control-plane state as a backup target (control region only)")
    entry = task["vars"]["_headscale_backup_entry"]
    assert entry["method"] == "file"
    assert entry["config"]["backup"] == {"headscale_snapshot": True}


def _render_backup_script(config: dict) -> str:
    env = make_ansible_env(BACKUP / "templates")
    return env.get_template("backup.sh.j2").render(
        ansible_managed="managed", accessory_name="headscale", method="file", retain=30,
        repo="r", accessory_config=config, inventory_hostname="eu-1",
        backup_restic_password="p", backup_s3_access_key_id="i",
        backup_s3_secret_access_key="k", backup_scripts_dir="/b", backup_lock_dir="/b/l",
        backup_restic_bin="/r", docker_monitor_alert_header="", docker_monitor_alert_footer="",
        backup_headscale_snapshot_bin="/usr/local/sbin/bay-headscale-db-snapshot",
    )


def test_backup_script_runs_the_helper_not_docker_cp(tmp_path):
    script = _render_backup_script({"backup": {"headscale_snapshot": True}})
    path = tmp_path / "backup.sh"
    path.write_text(script)
    assert subprocess.run(["bash", "-n", str(path)]).returncode == 0
    helper_call = (
        'backup_from "${ACCESSORY}.tar" \\\n'
        "  /usr/bin/sudo -n /usr/local/sbin/bay-headscale-db-snapshot\n"
    )
    assert helper_call in script
    assert "docker cp" not in script.split("# ── Backup ──")[1].split("# ── Error Checking")[0]
    # Everything after the dump is the shared flow: alert, size check, retention.
    for kept in ("bay_notify backup.failed", "MIN_SNAPSHOT_SIZE", "--keep-within 30d --prune"):
        assert kept in script


def test_a_plain_file_backup_still_uses_docker_cp():
    script = _render_backup_script({"backup": {"source_path": "/data"}})
    assert 'docker cp "${CONTAINER}:${SOURCE_PATH}" -' in script
    assert "sudo" not in script


def test_helper_tasks():
    tasks = _yaml(BACKUP / "tasks" / "headscale.yml")
    apt = _task(tasks, "Install sqlite3 for the Headscale snapshot")["ansible.builtin.apt"]
    assert apt == {"name": "sqlite3", "state": "present"}
    folder = _task(tasks, "Create the root-only Headscale snapshot folder")["ansible.builtin.file"]
    assert (folder["owner"], folder["group"], folder["mode"]) == ("root", "root", "0700")
    helper = _task(tasks, "Install the Headscale snapshot helper")["ansible.builtin.template"]
    assert (helper["owner"], helper["mode"]) == ("root", "0755")
    sudoers = _task(tasks, "Allow the backup unit to run the Headscale snapshot helper as root")
    tpl = sudoers["ansible.builtin.template"]
    assert tpl["mode"] == "0440"
    assert tpl["validate"] == "/usr/sbin/visudo -cf %s"
    for task in tasks:
        assert task["become_user"] == "root"


def test_sudoers_rule_allows_no_arguments():
    env = make_ansible_env(BACKUP / "templates")
    text = env.get_template("headscale-backup.sudoers.j2").render(
        ansible_managed="m", app_user="deploy",
        backup_headscale_snapshot_bin="/usr/local/sbin/bay-headscale-db-snapshot",
    )
    rules = [line for line in text.splitlines() if line and not line.startswith("#")]
    assert rules == ['deploy ALL=(root) NOPASSWD: /usr/local/sbin/bay-headscale-db-snapshot ""']


def test_stale_sudoers_rule_is_removed_where_unused():
    task = _task(_yaml(BACKUP / "tasks" / "main.yml"),
                 "Remove the Headscale snapshot sudoers rule where it is not used")
    assert task["ansible.builtin.file"] == {"path": "/etc/sudoers.d/bay-headscale-backup",
                                            "state": "absent"}
    assert "not (_backup_headscale_enabled | bool)" in task["when"]


# The helper, run for real against a WAL database.

_SQLITE3 = shutil.which("sqlite3")
needs_sqlite3 = pytest.mark.skipif(_SQLITE3 is None, reason="no sqlite3 binary on this machine")


def _helper(tmp_path: Path, data: Path) -> Path:
    work = tmp_path / "work"
    work.mkdir(mode=0o700, exist_ok=True)
    env = make_ansible_env(BACKUP / "templates")
    text = env.get_template("headscale-db-snapshot.sh.j2").render(
        ansible_managed="m", backup_headscale_data_dir=str(data),
        backup_headscale_snapshot_dir=str(work),
    )
    assert "SQLITE3=/usr/bin/sqlite3\n" in text
    text = text.replace("SQLITE3=/usr/bin/sqlite3\n", f"SQLITE3={_SQLITE3}\n")
    path = tmp_path / "snapshot.sh"
    path.write_text(text)
    return path


def _wal_db(data: Path) -> sqlite3.Connection:
    """A WAL database whose newest rows live only in the -wal file (no checkpoint)."""
    data.mkdir(exist_ok=True)
    conn = sqlite3.connect(data / "db.sqlite", isolation_level=None)
    assert conn.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    conn.execute("PRAGMA wal_autocheckpoint=0")
    conn.execute("CREATE TABLE nodes (id INTEGER PRIMARY KEY, name TEXT)")
    conn.executemany("INSERT INTO nodes (name) VALUES (?)", [(f"n{i}",) for i in range(50)])
    (data / "noise_private.key").write_text("privkey:placeholder-noise\n")
    (data / "derp_server_private.key").write_text("privkey:placeholder-derp\n")
    return conn


@needs_sqlite3
def test_helper_snapshots_wal_rows_and_the_keys(tmp_path):
    data = tmp_path / "data"
    conn = _wal_db(data)
    try:
        assert (data / "db.sqlite-wal").stat().st_size > 0
        helper = _helper(tmp_path, data)
        result = subprocess.run(["bash", str(helper)], capture_output=True)
        assert result.returncode == 0, result.stderr.decode()
    finally:
        conn.close()
    archive = tmp_path / "out.tar"
    archive.write_bytes(result.stdout)
    with tarfile.open(archive) as tar:
        members = {m.name: m for m in tar.getmembers()}
        assert set(members) == {"headscale/db.sqlite", "headscale/noise_private.key",
                                "headscale/derp_server_private.key"}
        assert all(stat.S_IMODE(m.mode) == 0o600 for m in members.values())
        out = tmp_path / "restored"
        tar.extractall(out, filter="data")
    restored = sqlite3.connect(out / "headscale" / "db.sqlite")
    assert restored.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert restored.execute("select count(*) from nodes").fetchone()[0] == 50
    restored.close()
    # The temp folder is gone on success.
    assert os.listdir(tmp_path / "work") == []


@needs_sqlite3
def test_helper_fails_on_a_corrupt_db_and_prints_nothing(tmp_path):
    data = tmp_path / "data"
    _wal_db(data).close()  # clean close: checkpoint, no -wal left
    with open(data / "db.sqlite", "r+b") as fh:
        fh.seek(4096 + 8)
        fh.write(b"\x00garbage-garbage-garbage\xff" * 4)
    result = subprocess.run(["bash", str(_helper(tmp_path, data))], capture_output=True)
    assert result.returncode == 1
    assert result.stdout == b""
    assert b"integrity_check" in result.stderr
    assert os.listdir(tmp_path / "work") == []


@needs_sqlite3
def test_helper_fails_without_a_db(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    result = subprocess.run(["bash", str(_helper(tmp_path, data))], capture_output=True)
    assert result.returncode == 1
    assert result.stdout == b""
    assert b"no Headscale database" in result.stderr
