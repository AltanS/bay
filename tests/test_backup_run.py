"""`bay backup run` (2.24.0): run a backup the way its timer does.

Before 2.24.0 the command ran ``backup-<target>.sh`` over ``ansible --become``,
so as root. A first run made a root-owned restic cache, and the timer (the app
user) then ran every backup with no cache: "permission denied" in each log and
the whole repository index read from S3 again. Now the command starts
``bay-backup@<target>.service``, and the backup role gives a root-owned cache
back to the app user.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import yaml
from typer.testing import CliRunner

from bay_cli.commands import backup
from bay_cli.context import Context

ROOT = Path(__file__).resolve().parent.parent


def _cx(tmp_path: Path) -> Context:
    fleet = tmp_path / "fleet"
    (fleet / "group_vars" / "all").mkdir(parents=True)
    (fleet / "group_vars" / "all" / "main.yml").write_text("---\nstack_name: shop\n")
    return Context(fleet_root=fleet, framework_root=Path("/x/bay"), source="flag")


def _capture(monkeypatch) -> list[dict]:
    calls: list[dict] = []

    def fake_run_on_host(env, cmd, *, bay_dir, check=True, **kwargs):
        calls.append({"env": env, "cmd": cmd, **kwargs})
        return subprocess.CompletedProcess(args=["ansible"], returncode=0, stdout="", stderr="")

    monkeypatch.setattr(backup, "_run_on_host", fake_run_on_host)
    return calls


def test_run_one_target_starts_its_unit_on_its_hosts(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(backup, "_hosts_with_target", lambda env, target, **kw: ["eu-app"])
    calls = _capture(monkeypatch)

    out = CliRunner().invoke(backup.app, ["run", "postgres"], obj=_cx(tmp_path))

    assert out.exit_code == 0, out.output
    (call,) = calls
    assert call["limit"] == "eu-app"
    assert "systemctl start bay-backup@postgres.service" in call["cmd"]
    assert "backup-postgres.sh" not in call["cmd"]


def test_run_all_starts_the_unit_of_each_script(monkeypatch, tmp_path) -> None:
    calls = _capture(monkeypatch)

    out = CliRunner().invoke(backup.app, ["run"], obj=_cx(tmp_path))

    assert out.exit_code == 0, out.output
    (call,) = calls
    assert "/opt/shop/backup/backup-*.sh" in call["cmd"]
    assert 'systemctl start "bay-backup@""$t"".service"' in call["cmd"]
    assert 'bash "$script"' not in call["cmd"]


def _fake_bin(tmp_path: Path, failing: str) -> tuple[Path, Path]:
    """Fake systemctl/journalctl: log each call; ``failing`` fails to start."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls.log"
    (bin_dir / "systemctl").write_text(
        "#!/bin/sh\n"
        f'echo "systemctl $*" >> {log}\n'
        'if [ "$1" = show ]; then echo "inv-$5"; exit 0; fi\n'
        f'[ "$2" = "bay-backup@{failing}.service" ] && exit 3\n'
        "exit 0\n"
    )
    (bin_dir / "journalctl").write_text(
        "#!/bin/sh\n"
        f'echo "journalctl $*" >> {log}\n'
        'echo "log of $4"\n'
    )
    for f in bin_dir.iterdir():
        f.chmod(0o755)
    return bin_dir, log


def test_run_all_shell_runs_every_unit_and_fails_when_one_fails(monkeypatch, tmp_path) -> None:
    """The real shell text: every target runs, one failure fails the command."""
    calls = _capture(monkeypatch)
    CliRunner().invoke(backup.app, ["run"], obj=_cx(tmp_path))
    scripts = tmp_path / "opt" / "shop" / "backup"
    scripts.mkdir(parents=True)
    for name in ("postgres", "shop_uploads"):
        (scripts / f"backup-{name}.sh").write_text("#!/bin/sh\n")
    cmd = calls[0]["cmd"].replace("/opt/shop/", f"{tmp_path}/opt/shop/")
    bin_dir, log = _fake_bin(tmp_path, failing="postgres")

    done = subprocess.run(
        ["sh", "-c", cmd], capture_output=True, text=True,
        env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"},
    )

    assert done.returncode == 1
    lines = log.read_text().splitlines()
    assert "systemctl start bay-backup@postgres.service" in lines
    assert "systemctl start bay-backup@shop_uploads.service" in lines
    assert any("_SYSTEMD_INVOCATION_ID=inv-bay-backup@shop_uploads.service" in ln for ln in lines)
    assert "==> Running shop_uploads" in done.stdout


def test_role_gives_the_restic_cache_back_to_the_app_user() -> None:
    tasks = yaml.safe_load((ROOT / "roles" / "backup" / "tasks" / "main.yml").read_text())
    (task,) = [t for t in tasks if t.get("name") == "Give the restic cache back to the app user"]
    spec = task["ansible.builtin.file"]
    assert spec["path"] == "{{ backup_scripts_dir }}/cache"
    assert spec["owner"] == "{{ app_user }}"
    assert spec["recurse"] is True
    assert "mode" not in spec
    # The play runs as the app user; only root can chown a root-owned file.
    assert task["become_user"] == "root"
