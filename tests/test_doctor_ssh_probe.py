"""`bin/bay doctor` must probe SSH as a user the server actually accepts.

The probe first ran `ssh <host> true` with no user, so it authenticated as
the local account name. The fix for that put `root` first, which was worse:
every hardened host has root login off, so every doctor run spent a
guaranteed failed authentication against an sshd that CrowdSec is watching,
and the inventory's own `ansible_user` was never consulted at all.

The order is now: inventory `ansible_user`, then `admin_user`, then no user
(the ssh default, which honours ~/.ssh/config), then `root` last.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from bay_cli.commands.doctor import (
    DEFAULT_ADMIN_USER,
    _describe_user,
    _inventory_ansible_user,
    _probe_ssh,
    _ssh_users,
)

HOST = "203.0.113.10"


class _Proc:
    def __init__(self, returncode: int, stderr: str = "") -> None:
        self.returncode = returncode
        self.stderr = stderr
        self.stdout = ""


def _write_main(root: Path, body: str) -> None:
    (root / "group_vars" / "all").mkdir(parents=True, exist_ok=True)
    (root / "group_vars" / "all" / "main.yml").write_text(body)


def _write_inventory(root: Path, body: str) -> Path:
    (root / "hosts").mkdir(parents=True, exist_ok=True)
    path = root / "hosts" / "production"
    path.write_text(body)
    return path


# ── Which users get tried ────────────────────────────────────────────────


def test_admin_user_first_then_default_then_root(tmp_path: Path) -> None:
    _write_main(tmp_path, "---\nadmin_user: bay-admin\n")
    assert _ssh_users(tmp_path) == ["bay-admin", None, "root"]


def test_root_is_always_last(tmp_path: Path) -> None:
    _write_main(tmp_path, "---\nadmin_user: ops\n")
    assert _ssh_users(tmp_path)[-1] == "root"


def test_admin_user_override_is_honoured(tmp_path: Path) -> None:
    _write_main(tmp_path, "---\nadmin_user: ops\n")
    assert _ssh_users(tmp_path) == ["ops", None, "root"]


def test_missing_main_yml_falls_back_to_the_scaffold_default(tmp_path: Path) -> None:
    assert _ssh_users(tmp_path) == [DEFAULT_ADMIN_USER, None, "root"]


def test_admin_user_root_is_not_probed_twice(tmp_path: Path) -> None:
    _write_main(tmp_path, "---\nadmin_user: root\n")
    assert _ssh_users(tmp_path) == ["root", None]


def test_inventory_ansible_user_wins(tmp_path: Path) -> None:
    _write_main(tmp_path, "---\nadmin_user: bay-admin\n")
    inv = _write_inventory(tmp_path, f"[production]\n{HOST} ansible_user=deploy\n")
    assert _ssh_users(tmp_path, host=HOST, inventory_file=inv) == [
        "deploy", "bay-admin", None, "root",
    ]


def test_inventory_user_equal_to_admin_user_is_not_duplicated(tmp_path: Path) -> None:
    _write_main(tmp_path, "---\nadmin_user: deploy\n")
    inv = _write_inventory(tmp_path, f"[production]\n{HOST} ansible_user=deploy\n")
    assert _ssh_users(tmp_path, host=HOST, inventory_file=inv) == ["deploy", None, "root"]


def test_inventory_without_ansible_user_changes_nothing(tmp_path: Path) -> None:
    _write_main(tmp_path, "---\nadmin_user: bay-admin\n")
    inv = _write_inventory(tmp_path, f"[production]\n{HOST}\n")
    assert _ssh_users(tmp_path, host=HOST, inventory_file=inv) == ["bay-admin", None, "root"]


# ── Reading ansible_user out of the inventory ────────────────────────────


def test_host_line_var(tmp_path: Path) -> None:
    inv = _write_inventory(tmp_path, f"[production]\n{HOST} ansible_user=deploy ansible_port=22\n")
    assert _inventory_ansible_user(inv, HOST) == "deploy"


def test_group_vars_block(tmp_path: Path) -> None:
    inv = _write_inventory(
        tmp_path,
        f"[production]\n{HOST}\n\n[production:vars]\nansible_user = ops\n",
    )
    assert _inventory_ansible_user(inv, HOST) == "ops"


def test_all_vars_block_is_the_last_resort(tmp_path: Path) -> None:
    inv = _write_inventory(tmp_path, f"[production]\n{HOST}\n\n[all:vars]\nansible_user=fleet\n")
    assert _inventory_ansible_user(inv, HOST) == "fleet"


def test_host_line_beats_the_group_block(tmp_path: Path) -> None:
    inv = _write_inventory(
        tmp_path,
        f"[production]\n{HOST} ansible_user=deploy\n\n[production:vars]\nansible_user=ops\n",
    )
    assert _inventory_ansible_user(inv, HOST) == "deploy"


def test_another_hosts_var_is_not_borrowed(tmp_path: Path) -> None:
    inv = _write_inventory(
        tmp_path,
        f"[production]\n198.51.100.5 ansible_user=other\n{HOST}\n",
    )
    assert _inventory_ansible_user(inv, HOST) is None


def test_comments_and_children_sections_are_ignored(tmp_path: Path) -> None:
    inv = _write_inventory(
        tmp_path,
        "# ansible_user=commented\n"
        f"[eu]\n{HOST}\n\n[production:children]\neu\n",
    )
    assert _inventory_ansible_user(inv, HOST) is None


def test_missing_inventory_is_not_an_error(tmp_path: Path) -> None:
    assert _inventory_ansible_user(None, HOST) is None
    assert _inventory_ansible_user(tmp_path / "nope", HOST) is None


# ── The probe itself ─────────────────────────────────────────────────────


def test_first_candidate_wins(monkeypatch) -> None:
    seen: list[str] = []

    def fake_run(cmd, **kwargs):
        seen.append(cmd[-2])
        return _Proc(0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    connected, user, reason = _probe_ssh(HOST, ["deploy", "bay-admin", None, "root"])
    assert (connected, user, reason) == (True, "deploy", "")
    # Nothing after the first success is tried, and root is never touched.
    assert seen == [f"deploy@{HOST}"]


def test_root_is_only_reached_after_everything_else(monkeypatch) -> None:
    seen: list[str] = []

    def fake_run(cmd, **kwargs):
        target = cmd[-2]
        seen.append(target)
        return _Proc(0) if target == f"root@{HOST}" else _Proc(255, "Permission denied")

    monkeypatch.setattr(subprocess, "run", fake_run)
    connected, user, _reason = _probe_ssh(HOST, ["bay-admin", None, "root"])
    assert (connected, user) == (True, "root")
    assert seen == [f"bay-admin@{HOST}", HOST, f"root@{HOST}"]


def test_no_user_candidate_omits_the_user_from_the_target(monkeypatch) -> None:
    seen: list[str] = []

    def fake_run(cmd, **kwargs):
        seen.append(cmd[-2])
        return _Proc(0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    connected, user, _reason = _probe_ssh(HOST, [None, "root"])
    assert (connected, user) == (True, None)
    assert seen == [HOST]


def test_all_failing_reports_a_reason(monkeypatch) -> None:
    monkeypatch.setattr(
        subprocess, "run", lambda cmd, **kw: _Proc(255, "Permission denied (publickey).")
    )
    connected, user, reason = _probe_ssh(HOST, ["bay-admin", None, "root"])
    assert connected is False
    assert user is None
    assert "Permission denied" in reason


def test_timeout_moves_on_to_the_next_user(monkeypatch) -> None:
    def fake_run(cmd, **kwargs):
        if cmd[-2].startswith("bay-admin@"):
            raise subprocess.TimeoutExpired(cmd, 10)
        return _Proc(0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    connected, user, _reason = _probe_ssh(HOST, ["bay-admin", "root"])
    assert (connected, user) == (True, "root")


def test_missing_ssh_binary_stops_immediately(monkeypatch) -> None:
    def fake_run(cmd, **kwargs):
        raise FileNotFoundError("ssh")

    monkeypatch.setattr(subprocess, "run", fake_run)
    connected, _user, reason = _probe_ssh(HOST, ["bay-admin", "root"])
    assert connected is False
    assert "ssh command not found" in reason


# ── Reporting ────────────────────────────────────────────────────────────


def test_describe_user() -> None:
    assert _describe_user("ops", HOST) == f"ops@{HOST}"
    assert _describe_user(None, HOST) == f"{HOST} (ssh default user)"
