"""`bin/bay doctor` must probe SSH as a user the server actually accepts.

The probe used to run `ssh <host> true` with no user, so it authenticated as
the local account name. A fresh Ubuntu server only accepts `root`; after
`bin/bay provision` root login is disabled and only `admin_user` works. The
probe therefore failed on both sides of the one transition it exists to cover.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from bay_cli.commands.doctor import DEFAULT_ADMIN_USER, _probe_ssh, _ssh_users


class _Proc:
    def __init__(self, returncode: int, stderr: str = "") -> None:
        self.returncode = returncode
        self.stderr = stderr
        self.stdout = ""


def _write_main(root: Path, body: str) -> None:
    (root / "group_vars" / "all").mkdir(parents=True, exist_ok=True)
    (root / "group_vars" / "all" / "main.yml").write_text(body)


# ── Which users get tried ────────────────────────────────────────────────


def test_root_first_then_admin_user(tmp_path: Path) -> None:
    _write_main(tmp_path, "---\nadmin_user: bay-admin\n")
    assert _ssh_users(tmp_path) == ["root", "bay-admin"]


def test_admin_user_override_is_honoured(tmp_path: Path) -> None:
    _write_main(tmp_path, "---\nadmin_user: ops\n")
    assert _ssh_users(tmp_path) == ["root", "ops"]


def test_missing_main_yml_falls_back_to_the_scaffold_default(tmp_path: Path) -> None:
    assert _ssh_users(tmp_path) == ["root", DEFAULT_ADMIN_USER]


def test_admin_user_root_is_not_probed_twice(tmp_path: Path) -> None:
    _write_main(tmp_path, "---\nadmin_user: root\n")
    assert _ssh_users(tmp_path) == ["root"]


# ── The probe itself ─────────────────────────────────────────────────────


def test_root_wins_on_a_fresh_server(monkeypatch) -> None:
    seen: list[str] = []

    def fake_run(cmd, **kwargs):
        seen.append(cmd[-2])
        return _Proc(0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    user, reason = _probe_ssh("203.0.113.10", ["root", "bay-admin"])
    assert user == "root"
    assert reason == ""
    # admin_user is never tried once root connects
    assert seen == ["root@203.0.113.10"]


def test_admin_user_wins_after_provisioning(monkeypatch) -> None:
    seen: list[str] = []

    def fake_run(cmd, **kwargs):
        target = cmd[-2]
        seen.append(target)
        return _Proc(0) if target.startswith("bay-admin@") else _Proc(255, "Permission denied")

    monkeypatch.setattr(subprocess, "run", fake_run)
    user, _reason = _probe_ssh("203.0.113.10", ["root", "bay-admin"])
    assert user == "bay-admin"
    assert seen == ["root@203.0.113.10", "bay-admin@203.0.113.10"]


def test_both_failing_reports_a_reason(monkeypatch) -> None:
    monkeypatch.setattr(
        subprocess, "run", lambda cmd, **kw: _Proc(255, "Permission denied (publickey).")
    )
    user, reason = _probe_ssh("203.0.113.10", ["root", "bay-admin"])
    assert user is None
    assert "Permission denied" in reason


def test_timeout_moves_on_to_the_next_user(monkeypatch) -> None:
    def fake_run(cmd, **kwargs):
        if cmd[-2].startswith("root@"):
            raise subprocess.TimeoutExpired(cmd, 10)
        return _Proc(0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    user, _reason = _probe_ssh("203.0.113.10", ["root", "bay-admin"])
    assert user == "bay-admin"


def test_missing_ssh_binary_stops_immediately(monkeypatch) -> None:
    def fake_run(cmd, **kwargs):
        raise FileNotFoundError("ssh")

    monkeypatch.setattr(subprocess, "run", fake_run)
    user, reason = _probe_ssh("203.0.113.10", ["root", "bay-admin"])
    assert user is None
    assert "ssh command not found" in reason
