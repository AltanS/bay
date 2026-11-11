"""`bay host upgrade <env> --box <box>`: arguments, refusals, the one-host limit, JSON.

The playbook is faked (``ops._run_playbook``): these tests cover the CLI side.
The role itself runs in tests/test_host_upgrade_role.py. No network, no SSH.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_plan_up import (  # noqa: E402,F401  (fixtures are used by name)
    FAKE_HOSTS,
    FakeBox,
    _git_identity,
    box,
    cli,
    commit_all,
    do_up,
    git,
    world,
)

from bay_cli import fleetsync, lockfile, receipts  # noqa: E402
from bay_cli.commands import ops  # noqa: E402

NAMES = {
    "production": [FAKE_HOSTS["box-1"], FAKE_HOSTS["box-2"]],
    "one": [FAKE_HOSTS["box-1"]],
    "two": [FAKE_HOSTS["box-2"]],
    FAKE_HOSTS["box-1"]: [FAKE_HOSTS["box-1"]],
    FAKE_HOSTS["box-2"]: [FAKE_HOSTS["box-2"]],
}


@pytest.fixture
def fleet(world: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """The world fleet with two boxes in production, each on its own host."""
    path = world["fleet"] / "bay.fleet.toml"
    text = path.read_text().replace(
        '[boxes.box-1]\nenv = "production"\n',
        '[boxes.box-1]\nenv = "production"\ngroup = "one"\n\n'
        '[boxes.box-2]\nenv = "production"\ngroup = "two"\n\n'
        '[boxes.lab-1]\nenv = "staging"\n',
    )
    path.write_text(text)
    commit_all(world["fleet"], "two boxes")
    monkeypatch.setattr(receipts, "inventory_names", lambda *a, **k: NAMES)
    return world


class Playbook:
    """Stands in for ops._run_playbook and writes the role's result file."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.result: dict[str, Any] | None = {"ok": True, "reason": None}
        self.fail = False

    def __call__(self, cx: Any, playbook: str, env: str, tags: Any, extra: list[str], **kw: Any) -> None:
        variables = json.loads(extra[extra.index("-e") + 1])
        self.calls.append({"playbook": playbook, "env": env, "tags": tags, "extra": extra,
                           "vars": variables})
        if self.result is not None:
            doc = {
                "host": extra[extra.index("-l") + 1], "box": variables["host_upgrade_box"],
                "dry_run": "--check" in extra, "failed_task": None, "error": None,
                "packages": ["docker-ce", "tailscale"], "packages_upgraded": 2,
                "reboot_required": True, "rebooted": bool(variables["host_upgrade_reboot"]),
                "kernel": {"before": "6.1.0-25-amd64", "after": "6.1.0-26-amd64",
                           "newest_installed": "6.1.0-26-amd64"},
                "containers": {"before": ["api"], "after": ["api"], "missing": [], "unhealthy": []},
                "firewall": {"repaired": False, "why": []},
                "checks": [{"name": "docker", "ok": True, "detail": "docker is active and answers"}],
                "log": "/var/log/bay-host-upgrade.log",
                **self.result,
            }
            Path(variables["host_upgrade_result_path"]).write_text(json.dumps(doc))
        if self.fail:
            from bay_cli.errors import BayError

            raise BayError("Command failed with exit code 2")


@pytest.fixture
def playbook(monkeypatch: pytest.MonkeyPatch) -> Playbook:
    fake = Playbook()
    monkeypatch.setattr(ops, "_run_playbook", fake)
    return fake


def upgrade(fleet: dict[str, Path], *args: str) -> Any:
    return cli(fleet, "host", "upgrade", *args)


def test_box_is_required(fleet: dict[str, Path], playbook: Playbook) -> None:
    result = upgrade(fleet, "production")
    assert result.exit_code == 2
    assert "--box" in result.output
    assert playbook.calls == []


def test_one_box_runs_on_its_one_host(fleet: dict[str, Path], playbook: Playbook) -> None:
    result = upgrade(fleet, "production", "--box", "box-2", "--json")
    assert result.exit_code == 0, result.output
    [call] = playbook.calls
    assert call["playbook"] == "host_upgrade" and call["env"] == "production"
    assert call["tags"] is None
    extra = call["extra"]
    assert extra[:2] == ["-l", FAKE_HOSTS["box-2"]]
    assert extra.count("-l") == 1
    assert call["vars"]["host_upgrade_box"] == "box-2"
    assert call["vars"]["host_upgrade_reboot"] is False
    doc = json.loads(result.stdout)
    assert doc["host"] == FAKE_HOSTS["box-2"] and doc["boxes_on_host"] == ["box-2"]


def test_reboot_is_passed_only_when_asked(fleet: dict[str, Path], playbook: Playbook) -> None:
    result = upgrade(fleet, "production", "--box", "box-1", "--reboot", "--json")
    assert result.exit_code == 0, result.output
    assert playbook.calls[0]["vars"]["host_upgrade_reboot"] is True
    assert json.loads(result.stdout)["rebooted"] is True


def test_json_shape(fleet: dict[str, Path], playbook: Playbook) -> None:
    result = upgrade(fleet, "production", "--box", "box-1", "--json")
    assert result.exit_code == 0, result.output
    doc = json.loads(result.stdout)
    assert set(doc) >= {
        "result_version", "ok", "env", "box", "host", "boxes_on_host", "dry_run", "reason",
        "error", "failed_task", "box_touched", "packages_upgraded", "packages",
        "reboot_required", "rebooted", "kernel", "containers", "firewall", "checks", "log",
    }
    assert doc["result_version"] == 1
    assert doc["ok"] is True and doc["reason"] is None
    assert doc["packages_upgraded"] == 2
    assert doc["kernel"] == {"before": "6.1.0-25-amd64", "after": "6.1.0-26-amd64",
                             "newest_installed": "6.1.0-26-amd64"}
    assert doc["containers"]["before"] == ["api"] and doc["containers"]["after"] == ["api"]
    assert doc["checks"][0]["name"] == "docker"
    assert doc["box_touched"] is True


def test_no_receipt_and_no_lock_record_is_written(
    fleet: dict[str, Path], box: FakeBox, playbook: Playbook
) -> None:
    head = git(fleet["fleet"], "rev-parse", "HEAD")
    result = upgrade(fleet, "production", "--box", "box-1", "--json")
    assert result.exit_code == 0, result.output
    assert git(fleet["fleet"], "rev-parse", "HEAD") == head
    assert git(fleet["fleet"], "status", "--porcelain") == ""
    assert "receipt" not in json.dumps(playbook.calls[0]["vars"])


@pytest.mark.parametrize(
    ("box_name", "words"),
    [("nope", "no box 'nope'"), ("lab-1", "is not in production")],
)
def test_unknown_or_foreign_box_is_refused(
    fleet: dict[str, Path], playbook: Playbook, box_name: str, words: str
) -> None:
    result = upgrade(fleet, "production", "--box", box_name, "--json")
    assert result.exit_code != 0
    doc = json.loads(result.stdout)
    assert words in doc["error"] and doc["ok"] is False and doc["box_touched"] is False
    assert playbook.calls == []


def test_a_box_on_two_hosts_is_refused(
    fleet: dict[str, Path], playbook: Playbook, monkeypatch: pytest.MonkeyPatch
) -> None:
    names = {**NAMES, "one": [FAKE_HOSTS["box-1"], FAKE_HOSTS["box-2"]]}
    monkeypatch.setattr(receipts, "inventory_names", lambda *a, **k: names)
    result = upgrade(fleet, "production", "--box", "box-1", "--json")
    assert result.exit_code != 0
    assert "maps to 2 inventory hosts" in json.loads(result.stdout)["error"]
    assert playbook.calls == []


def _claim(fleet: dict[str, Path], **record: Any) -> None:
    path = lockfile.lock_path(fleet["fleet"], "webapp")
    raw = lockfile.read(path)
    assert raw is not None
    raw["envs"]["production"].update(record)
    lockfile.write(path, raw)
    commit_all(fleet["fleet"], "a claim")


def test_a_live_claim_on_the_box_env_refuses_with_43(
    fleet: dict[str, Path], box: FakeBox, playbook: Playbook
) -> None:
    do_up(fleet, force=True, reason="test setup")
    _claim(
        fleet, result="pending", plan_id="0" * 12, claimed_by="machine-b",
        claimed_at=fleetsync.now_iso(), claimed_run="0123456789ab:4026531836:4242:99",
    )
    result = upgrade(fleet, "production", "--box", "box-1", "--json")
    assert result.exit_code == 43, result.output
    doc = json.loads(result.stdout)
    assert doc["reason"] == "env_busy" and doc["box_touched"] is False
    assert doc["holder"]["claimed_by"] == "machine-b" and doc["holder"]["box_env"] == "production"
    assert "machine-b is deploying production since" in doc["error"]
    assert "--take-over" not in doc["error"]
    assert playbook.calls == []
    # Without --json the error reaches bay's entry point, which exits with its code.
    plain = upgrade(fleet, "production", "--box", "box-1")
    assert plain.exception.exit_code == 43
    assert plain.exception.reason == "env_busy"
    assert "machine-b is deploying production" in str(plain.exception)


def test_an_old_claim_does_not_block(fleet: dict[str, Path], box: FakeBox, playbook: Playbook) -> None:
    do_up(fleet, force=True, reason="test setup")
    _claim(
        fleet, result="pending", plan_id="0" * 12, claimed_by="machine-b",
        claimed_at="2026-01-01T00:00:00Z", claimed_run="0123456789ab:4026531836:4242:99",
    )
    result = upgrade(fleet, "production", "--box", "box-1", "--json")
    assert result.exit_code == 0, result.output
    assert len(playbook.calls) == 1


def test_a_claim_on_another_box_env_does_not_block(
    fleet: dict[str, Path], box: FakeBox, playbook: Playbook
) -> None:
    do_up(fleet, force=True, reason="test setup")
    _claim(
        fleet, result="pending", plan_id="0" * 12, claimed_by="machine-b", box="lab-1",
        claimed_at=fleetsync.now_iso(), claimed_run="0123456789ab:4026531836:4242:99",
    )
    result = upgrade(fleet, "production", "--box", "box-1", "--json")
    assert result.exit_code == 0, result.output


@pytest.mark.parametrize("reason", ["box_locked", "upgrade_running"])
def test_a_busy_box_exits_43_and_was_not_touched(
    fleet: dict[str, Path], playbook: Playbook, reason: str
) -> None:
    playbook.result = {"ok": False, "reason": reason, "error": "The deploy lock is held",
                       "failed_task": "Stop when the deploy lock is held", "packages_upgraded": 0}
    playbook.fail = True
    result = upgrade(fleet, "production", "--box", "box-1", "--json")
    assert result.exit_code == 43, result.output
    doc = json.loads(result.stdout)
    assert doc["reason"] == reason and doc["ok"] is False and doc["box_touched"] is False


def test_a_failed_check_exits_1_and_names_it(fleet: dict[str, Path], playbook: Playbook) -> None:
    playbook.result = {
        "ok": False, "reason": "failed", "failed_task": "Fail when a check failed",
        "error": "Check failed: containers.",
        "checks": [{"name": "containers", "ok": False, "detail": "missing: api"}],
    }
    playbook.fail = True
    result = upgrade(fleet, "production", "--box", "box-1")
    assert result.exit_code == 1
    assert "FAILED (failed)" in result.stdout
    assert "FAIL containers: missing: api" in result.stdout


def test_no_result_file_is_a_failure(fleet: dict[str, Path], playbook: Playbook) -> None:
    playbook.result = None
    playbook.fail = True
    result = upgrade(fleet, "production", "--box", "box-1", "--json")
    assert result.exit_code == 1
    doc = json.loads(result.stdout)
    assert doc["ok"] is False and doc["reason"] == "failed" and doc["box_touched"] is True


def test_check_mode_passes_through_and_is_a_dry_run(fleet: dict[str, Path], playbook: Playbook) -> None:
    result = upgrade(fleet, "production", "--box", "box-1", "--json", "--", "--check")
    assert result.exit_code == 0, result.output
    extra = playbook.calls[0]["extra"]
    assert "--check" in extra and "--" not in extra
    assert json.loads(result.stdout)["dry_run"] is True


@pytest.mark.parametrize("flag", ["-l", "--limit", "--tags", "--skip-tags"])
def test_limit_and_tags_cannot_pass_through(
    fleet: dict[str, Path], playbook: Playbook, flag: str
) -> None:
    result = upgrade(fleet, "production", "--box", "box-1", "--json", "--", flag, "x")
    assert result.exit_code != 0
    assert "cannot pass through" in json.loads(result.stdout)["error"]
    assert playbook.calls == []


def test_log_takes_the_progress_and_stdout_keeps_the_json(
    fleet: dict[str, Path], playbook: Playbook, tmp_path: Path
) -> None:
    log = tmp_path / "upgrade.log"
    result = upgrade(fleet, "production", "--box", "box-1", "--json", "--log", str(log))
    assert result.exit_code == 0, result.output
    json.loads(result.stdout)
    assert "host upgrade: box box-1 is host" in log.read_text()


def test_dirty_fleet_is_refused_unless_allowed(fleet: dict[str, Path], playbook: Playbook) -> None:
    (fleet["fleet"] / "loose.yml").write_text("x: 1\n")
    result = upgrade(fleet, "production", "--box", "box-1", "--json")
    assert result.exit_code == 41
    assert json.loads(result.stdout)["reason"] == "dirty"
    assert playbook.calls == []
    allowed = upgrade(fleet, "production", "--box", "box-1", "--json", "--allow-dirty")
    assert allowed.exit_code == 0, allowed.output


def test_the_verb_prints_the_fleet_line() -> None:
    from bay_cli import fleet_line

    assert "host upgrade" in fleet_line.MUTATING_VERBS
