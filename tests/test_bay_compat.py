"""The Bay version floor of a fleet (2.16): ``bay.compat.json``.

An older Bay must not deploy from a fleet that a newer Bay deployed last. The
fleet records the version (``deployed_with``); ``bay up`` writes it in its
claim commit; every verb that deploys checks it. Two clones of one fleet
share a bare remote and the fake box of test_plan_up. No network, no SSH,
no Ansible.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from typing import Any

import jsonschema
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_fleet_sync import (  # noqa: E402,F401  (fixtures are used by name)
    _op,
    machines,
    playbooks,
    proj_at,
    snapshot,
    up_at,
)
from test_plan_up import (  # noqa: E402,F401  (fixtures are used by name)
    PLAN_SCHEMA,
    FakeBox,
    _git_identity,
    box,
    cli,
    commit_all,
    edit_app,
    git,
    world,
)

from bay_cli import apply as applymod  # noqa: E402
from bay_cli import compat, fleetsync, lockfile  # noqa: E402
from bay_cli import plan as planmod  # noqa: E402
from bay_cli.context import Context  # noqa: E402
from bay_cli.errors import BayError  # noqa: E402
from bay_cli.fleet import FleetError, LockTooNew, load_lock  # noqa: E402

FILE = compat.COMPAT_FILE


def runs(monkeypatch: pytest.MonkeyPatch, version: str | None) -> None:
    """This machine runs Bay ``version`` (None: version.yml cannot tell)."""
    monkeypatch.setattr(compat, "installed_version", lambda _root: version)


def floor_of(fleet: Path) -> dict[str, Any]:
    return json.loads((fleet / FILE).read_text())


def set_floor(fleet: Path, version: str, *, push: bool = True) -> str:
    """Commit (and push) a bay.compat.json that names ``version``."""
    (fleet / FILE).write_text(json.dumps({"compat_version": 1, "deployed_with": version}) + "\n")
    head = commit_all(fleet, f"a Bay {version} run deployed")
    if push:
        git(fleet, "push", "-q")
    return head


def without_plans(snap: dict[str, Any]) -> dict[str, Any]:
    """A refused plan keeps its record under plans/, as every refused plan does."""
    files = {k: v for k, v in snap["files"].items() if not k.startswith("plans/")}
    status = "\n".join(
        line for line in snap["status"].splitlines() if not line[3:].startswith("plans/")
    )
    return {**snap, "files": files, "status": status}


# ── the compare ─────────────────────────────────────────────────────────────


def _write(root: Path, text: str) -> Path:
    (root / FILE).write_text(text)
    return root


@pytest.mark.parametrize(
    ("deployed_with", "installed", "state"),
    [
        ("2.16.0", "2.16.0", compat.SAME),
        ("2.16.0", "2.17.0", compat.NEWER),
        ("2.16.0", "3.0.0", compat.NEWER),
        ("2.16.1", "2.16.0", compat.OLDER),  # older by a patch
        ("2.16.0", "2.15.9", compat.OLDER),  # older by a minor
        ("2.10.0", "2.9.12", compat.OLDER),  # numbers, not strings
    ],
)
def test_the_compare(tmp_path: Path, deployed_with: str, installed: str, state: str) -> None:
    _write(tmp_path, json.dumps({"compat_version": 1, "deployed_with": deployed_with}))
    result = compat.check(tmp_path, installed)
    assert result.state == state
    assert result.deployed_with == deployed_with and result.installed == installed
    assert result.blocks is (state == compat.OLDER)
    if state == compat.OLDER:
        assert result.reason == "bay_older"
        assert result.message == (
            f"This fleet was last deployed with Bay v{deployed_with}. This machine runs "
            f"v{installed}. An older Bay can undo what the newer one wrote. Run "
            f"`bay self update --to v{deployed_with}`. To deploy with the older version on "
            "purpose (a downgrade), pass --allow-older-bay."
        )
        assert result.next_cmd == f"bay self update --to v{deployed_with}"
    # Only a change is written: the same version never is.
    assert compat.needs_write(result) is (state != compat.SAME)


def test_a_missing_file_does_not_block_and_is_written(tmp_path: Path) -> None:
    result = compat.check(tmp_path, "2.16.0")
    assert result.state == compat.MISSING and not result.blocks
    assert compat.needs_write(result)
    compat.write(tmp_path, "2.16.0")
    assert floor_of(tmp_path) == {"compat_version": 1, "deployed_with": "2.16.0"}
    assert compat.check(tmp_path, "2.16.0").state == compat.SAME


@pytest.mark.parametrize(
    ("text", "said"),
    [
        ("{not json", "not valid JSON"),
        ("[]", "not a JSON object"),
        ('{"compat_version": 1, "deployed_with": "v2.16"}', "deployed_with: must be a version"),
        ('{"compat_version": 1, "deployed_with": "2.16.0-rc1"}', "deployed_with: must be"),
        ('{"compat_version": 1}', "deployed_with: is required"),
        ('{"compat_version": "1", "deployed_with": "2.16.0"}', "compat_version"),
        ('{"compat_version": 0, "deployed_with": "2.16.0"}', "compat_version"),
    ],
)
def test_a_file_bay_cannot_read_blocks(tmp_path: Path, text: str, said: str) -> None:
    result = compat.check(_write(tmp_path, text), "2.16.0")
    assert result.state == compat.INVALID and result.reason == "compat_invalid"
    assert said in str(result.message)
    assert str(result.message).startswith(f"{FILE}: ")
    assert f"git checkout HEAD -- {FILE}" in str(result.message)
    assert not compat.needs_write(result)


def test_a_higher_compat_version_blocks_and_names_self_update(tmp_path: Path) -> None:
    text = json.dumps({"compat_version": 2, "deployed_with": "3.0.0", "more": {}})
    result = compat.check(_write(tmp_path, text), "9.9.9")
    assert result.state == compat.NEWER_FORMAT and result.reason == "compat_newer"
    assert result.message == (
        f"{FILE}: compat_version: is 2, but this Bay knows up to 1. A newer Bay wrote it. "
        "Run `bay self update`."
    )
    assert result.next_cmd == "bay self update"
    assert not compat.needs_write(result)


@pytest.mark.parametrize("installed", [None, "dev", "2.16"])
def test_an_unknown_installed_version_neither_blocks_nor_writes(
    tmp_path: Path, installed: str | None
) -> None:
    for text in (None, json.dumps({"compat_version": 1, "deployed_with": "9.0.0"})):
        if text is not None:
            _write(tmp_path, text)
        result = compat.check(tmp_path, installed)
        assert result.state == compat.UNKNOWN and not result.blocks
        assert not compat.needs_write(result)
        assert str(result.message).startswith("warning: this Bay does not know its own version")


def test_the_installed_version_comes_from_version_yml(tmp_path: Path) -> None:
    (tmp_path / "version.yml").write_text('---\nbay_version: "2.16.0"\n')
    assert compat.installed_version(tmp_path) == "2.16.0"
    (tmp_path / "version.yml").write_text("bay_version: [unclosed\n")
    assert compat.installed_version(tmp_path) is None  # a broken file: unknown
    assert compat.installed_version(tmp_path / "nowhere") is None


def test_the_schema_file_accepts_what_bay_writes(tmp_path: Path) -> None:
    compat.write(tmp_path, "2.16.0")
    jsonschema.validate(floor_of(tmp_path), json.loads(compat.SCHEMA_PATH.read_text()))
    with pytest.raises(BayError):
        compat.write(tmp_path, "unknown")


# ── bay up ──────────────────────────────────────────────────────────────────


def test_the_first_up_writes_the_file_in_its_claim_commit(
    machines: dict[str, Path], box: FakeBox, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    m = machines
    runs(monkeypatch, "2.16.0")
    assert not (m["a"] / FILE).exists()
    first = up_at(m, "a", "webapp", echo=print)
    assert first["result"] == "ok" and first["fleet_sync"]["claimed"] is True
    assert floor_of(m["a"]) == {"compat_version": 1, "deployed_with": "2.16.0"}
    # In the claim commit itself, so the claim carries it to the remote first.
    in_claim = git(m["a"], "show", "--name-only", "--format=", first["fleet_commit"]).splitlines()
    assert FILE in in_claim
    committed = git(m["a"], "show", f"{first['fleet_commit']}:{FILE}")
    assert committed == (m["a"] / FILE).read_text().rstrip("\n")
    assert git(m["a"], "status", "--porcelain=v1") == ""
    assert "bay.compat.json records Bay v2.16.0 (was no file)" in capsys.readouterr().out

    # A steady fleet: the second run does not touch the file.
    second = up_at(m, "a", "webapp")
    assert second["result"] == "ok"
    in_second = git(m["a"], "show", "--name-only", "--format=", second["fleet_commit"])
    assert FILE not in in_second.splitlines()
    assert git(m["a"], "log", "--format=%H", "--", FILE).splitlines() == [first["fleet_commit"]]

    # Machine B pulls it; a newer Bay there moves the floor up in its own claim.
    runs(monkeypatch, "2.16.1")
    b = up_at(m, "b", "status")
    assert floor_of(m["b"])["deployed_with"] == "2.16.1"
    assert FILE in git(m["b"], "show", "--name-only", "--format=", b["fleet_commit"]).splitlines()


def test_an_older_bay_is_refused_before_anything_is_written(
    machines: dict[str, Path], box: FakeBox, monkeypatch: pytest.MonkeyPatch
) -> None:
    m = machines
    set_floor(m["a"], "2.16.0")
    runs(monkeypatch, "2.15.0")
    before = without_plans(snapshot(m["a"]))
    remote = git(m["remote"], "rev-parse", "main")
    with pytest.raises(applymod.Refused) as err:
        up_at(m, "a", "webapp")
    assert err.value.reason == "bay_older" and err.value.exit_code == 45
    assert "This fleet was last deployed with Bay v2.16.0." in str(err.value)
    gate = next(g for g in err.value.plan["run_blockers"] if g["reason"] == "bay_older")
    assert (gate["deployed_with"], gate["installed"]) == ("2.16.0", "2.15.0")
    assert box.deploys == []
    assert without_plans(snapshot(m["a"])) == before  # no lock, no compile, no commit
    assert git(m["remote"], "rev-parse", "main") == remote

    # bay up --json: exit 45, the reason, box_touched false and what to run.
    result = cli({"fleet": m["a"], "app": m["app"]}, "up", "--json")
    assert result.exit_code == 45, result.output
    doc = json.loads(result.stdout)
    assert doc["reason"] == "bay_older" and doc["box_touched"] is False
    assert doc["next"] == "bay self update --to v2.16.0"
    assert doc["bay_compat"]["state"] == "older"
    assert box.deploys == []
    assert without_plans(snapshot(m["a"])) == before


def test_rollback_is_refused_by_an_older_bay(
    machines: dict[str, Path], box: FakeBox, monkeypatch: pytest.MonkeyPatch
) -> None:
    m = machines
    runs(monkeypatch, "2.16.0")
    up_at(m, "a", "webapp")
    edit_app(m, 'LOG_LEVEL = "info"', 'LOG_LEVEL = "debug"')
    up_at(m, "a", "webapp")
    runs(monkeypatch, "2.15.0")
    result = cli({"fleet": m["a"], "app": m["app"]}, "rollback", "--json")
    assert result.exit_code == 45, result.output
    doc = json.loads(result.stdout)
    assert doc["reason"] == "bay_older" and doc["box_touched"] is False
    assert box.deploys == ["production", "production"]
    rolled = applymod.rollback(
        proj_at(m["a"], "webapp", m["app"]), planmod.PlanOptions(), allow_older_bay=True
    )
    assert rolled["action"] == "rollback" and rolled["result"] == "ok"
    assert floor_of(m["a"])["deployed_with"] == "2.15.0"


def test_a_patch_older_bay_is_refused_too(
    machines: dict[str, Path], box: FakeBox, monkeypatch: pytest.MonkeyPatch
) -> None:
    m = machines
    set_floor(m["a"], "2.16.1")
    runs(monkeypatch, "2.16.0")
    with pytest.raises(applymod.Refused) as err:
        up_at(m, "a", "webapp")
    assert err.value.reason == "bay_older" and err.value.exit_code == 45


def test_allow_older_bay_deploys_and_moves_the_floor_down(
    machines: dict[str, Path], box: FakeBox, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    m = machines
    set_floor(m["a"], "2.16.0")
    runs(monkeypatch, "2.15.0")
    result = up_at(m, "a", "webapp", allow_older_bay=True)
    assert result["result"] == "ok" and box.deploys == ["production"]
    assert result["allowed_older_bay"] == {"deployed_with": "2.16.0", "installed": "2.15.0"}
    assert floor_of(m["a"])["deployed_with"] == "2.15.0"
    claim = git(m["a"], "show", "--name-only", "--format=", result["fleet_commit"]).splitlines()
    assert FILE in claim
    assert git(m["remote"], "show", f"main:{FILE}").find('"2.15.0"') > 0
    err = capsys.readouterr().err
    assert "WARNING: --allow-older-bay: this fleet was last deployed with Bay v2.16.0" in err
    # The floor is down: the next plain run on 2.15.0 goes through.
    assert up_at(m, "a", "webapp")["result"] == "ok"


def test_allow_older_bay_on_the_command_line(
    machines: dict[str, Path], box: FakeBox, monkeypatch: pytest.MonkeyPatch
) -> None:
    m = machines
    set_floor(m["a"], "2.16.0")
    runs(monkeypatch, "2.15.0")
    result = cli({"fleet": m["a"], "app": m["app"]}, "up", "--json", "--allow-older-bay")
    assert result.exit_code == 0, result.output
    doc = json.loads(result.stdout)
    assert doc["allowed_older_bay"]["installed"] == "2.15.0" and doc["reason"] is None


def test_a_newer_bay_moves_the_floor_up(
    machines: dict[str, Path], box: FakeBox, monkeypatch: pytest.MonkeyPatch
) -> None:
    m = machines
    set_floor(m["a"], "2.16.0")
    runs(monkeypatch, "2.17.0")
    result = up_at(m, "a", "webapp")
    assert floor_of(m["a"])["deployed_with"] == "2.17.0"
    assert FILE in git(m["a"], "show", "--name-only", "--format=", result["fleet_commit"])


@pytest.mark.parametrize("had", [None, "2.16.0"])
def test_a_stop_before_the_deploy_puts_the_file_back(
    machines: dict[str, Path], box: FakeBox, monkeypatch: pytest.MonkeyPatch, had: str | None
) -> None:
    """Ctrl-C at the claim push: the unwind removes a file the run created and
    restores one it changed (here 2.16.0 -> 2.17.0)."""
    m = machines
    if had:
        set_floor(m["a"], had)
    runs(monkeypatch, "2.17.0")
    before = snapshot(m["a"])

    def interrupted(repo: Path) -> tuple[bool, str | None]:
        raise KeyboardInterrupt

    monkeypatch.setattr(fleetsync, "push_upstream", interrupted)
    with pytest.raises(KeyboardInterrupt):
        up_at(m, "a", "webapp")
    assert snapshot(m["a"]) == before
    assert (m["a"] / FILE).exists() is bool(had)
    assert box.deploys == []


def test_a_refused_claim_puts_the_file_back(
    machines: dict[str, Path], box: FakeBox, monkeypatch: pytest.MonkeyPatch
) -> None:
    m = machines
    runs(monkeypatch, "2.16.0")
    before = snapshot(m["a"])
    hook = m["remote"] / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\necho refused by test >&2\nexit 1\n")
    hook.chmod(0o755)
    with pytest.raises(fleetsync.SyncError) as err:
        up_at(m, "a", "webapp")
    assert err.value.reason == "claim_refused"
    after = snapshot(m["a"])
    new_plans = set(after["files"]) - set(before["files"])
    assert all(p.startswith("plans/") for p in new_plans)
    assert not (m["a"] / FILE).exists()
    assert (after["head"], after["index"]) == (before["head"], before["index"])


def test_a_fleet_inside_a_bigger_repo_gets_the_file_committed(
    world: dict[str, Path], tmp_path: Path, box: FakeBox, monkeypatch: pytest.MonkeyPatch
) -> None:
    runs(monkeypatch, "2.16.0")
    outer = tmp_path / "workspace"
    outer.mkdir()
    fleet = outer / "fleet"
    shutil.move(str(world["fleet"]), str(fleet))
    shutil.rmtree(fleet / ".git")
    git(outer, "init", "-q")
    commit_all(outer, "workspace")
    result = applymod.up(
        planmod.load_project(Context.for_fleet_root(fleet), "webapp", cwd=world["app"]),
        planmod.PlanOptions(),
    )
    assert result["result"] == "ok" and "inside a larger repo" in result["push_skipped"]
    assert floor_of(fleet)["deployed_with"] == "2.16.0"
    shown = git(outer, "show", "--name-only", "--format=", result["fleet_commit"]).splitlines()
    assert f"fleet/{FILE}" in shown
    assert git(outer, "status", "--porcelain=v1") == ""


def test_an_unreadable_file_refuses_also_with_the_flag(
    machines: dict[str, Path], box: FakeBox, monkeypatch: pytest.MonkeyPatch
) -> None:
    m = machines
    (m["a"] / FILE).write_text("{broken\n")
    commit_all(m["a"], "a broken floor")
    git(m["a"], "push", "-q")
    runs(monkeypatch, "2.16.0")
    for flags in ({}, {"allow_older_bay": True}):
        with pytest.raises(applymod.Refused) as err:
            up_at(m, "a", "webapp", **flags)
        assert err.value.reason == "compat_invalid" and err.value.exit_code == 45
        assert f"{FILE}: not valid JSON" in str(err.value)
    assert (m["a"] / FILE).read_text() == "{broken\n"
    assert box.deploys == []


def test_a_higher_compat_version_refuses_up(
    machines: dict[str, Path], box: FakeBox, monkeypatch: pytest.MonkeyPatch
) -> None:
    m = machines
    (m["a"] / FILE).write_text(json.dumps({"compat_version": 2, "deployed_with": "3.0.0"}))
    commit_all(m["a"], "a floor from the future")
    git(m["a"], "push", "-q")
    runs(monkeypatch, "2.16.0")
    result = cli({"fleet": m["a"], "app": m["app"]}, "up", "--json", "--allow-older-bay")
    assert result.exit_code == 45, result.output
    doc = json.loads(result.stdout)
    assert doc["reason"] == "compat_newer" and doc["next"] == "bay self update"
    assert box.deploys == []


def test_an_unknown_version_deploys_and_writes_nothing(
    machines: dict[str, Path], box: FakeBox, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    m = machines
    runs(monkeypatch, None)
    result = up_at(m, "a", "webapp")
    assert result["result"] == "ok" and not (m["a"] / FILE).exists()
    err = capsys.readouterr().err
    assert err.count("warning: this Bay does not know its own version") == 1


def test_plan_shows_the_older_bay_as_a_blocker(
    machines: dict[str, Path], box: FakeBox, monkeypatch: pytest.MonkeyPatch
) -> None:
    m = machines
    set_floor(m["a"], "2.16.0")
    runs(monkeypatch, "2.15.0")
    result = cli({"fleet": m["a"], "app": m["app"]}, "plan", "--json")
    assert result.exit_code == 20, result.output
    plan = json.loads(result.stdout)
    assert plan["reason"] == "bay_older" and plan["verdict"] == "blocked"
    assert plan["bay_compat"] == {
        "state": "older",
        "deployed_with": "2.16.0",
        "installed": "2.15.0",
        "message": compat.older_message("2.16.0", "2.15.0"),
    }
    jsonschema.validate(plan, PLAN_SCHEMA)
    # The floor is not hashed: the same plan on the newer Bay has the same id.
    older = planmod.make_plan(proj_at(m["a"], "webapp", m["app"]), planmod.PlanOptions())
    runs(monkeypatch, "2.16.0")
    same = planmod.make_plan(proj_at(m["a"], "webapp", m["app"]), planmod.PlanOptions())
    assert older["plan_id"] == same["plan_id"] and same["reason"] is None


def test_a_remove_is_refused_by_an_older_bay(
    machines: dict[str, Path], box: FakeBox, monkeypatch: pytest.MonkeyPatch
) -> None:
    m = machines
    w = {"fleet": m["a"], "app": m["app"]}
    runs(monkeypatch, "2.16.0")
    up_at(m, "a", "webapp")
    plan = json.loads(cli(w, "remove", "webapp", "--json").stdout)
    assert cli(w, "approve", plan["plan_id"], "--reason", "retired").exit_code == 0
    runs(monkeypatch, "2.15.0")
    result = cli(w, "up", "production", "--plan-id", plan["plan_id"], "--json")
    assert result.exit_code == 45, result.output
    assert json.loads(result.stdout)["reason"] == "bay_older"
    assert box.deploys == ["production"]  # only the first up
    assert floor_of(m["a"])["deployed_with"] == "2.16.0"
    # With the flag it goes on and records the older Bay.
    done = cli(w, "up", "production", "--plan-id", plan["plan_id"], "--json", "--allow-older-bay")
    assert done.exit_code == 0, done.output
    assert floor_of(m["a"])["deployed_with"] == "2.15.0"


# ── bay deploy and bay provision ────────────────────────────────────────────


@pytest.mark.parametrize("verb", ["deploy", "provision"])
def test_playbook_verbs_refuse_an_older_bay_and_never_write(
    machines: dict[str, Path], playbooks: list[tuple[str, str]], monkeypatch: pytest.MonkeyPatch,
    verb: str,
) -> None:
    m = machines
    head = set_floor(m["a"], "2.16.0")
    runs(monkeypatch, "2.15.0")
    refused = _op(m, verb)
    exc = refused.exception
    assert getattr(exc, "reason", None) == "bay_older", refused.output
    assert exc.exit_code == 45 and exc.next_cmd == "bay self update --to v2.16.0"
    assert "This machine runs v2.15.0." in str(exc)
    assert playbooks == []
    allowed = _op(m, verb, "--allow-older-bay")
    assert allowed.exit_code == 0, allowed.output
    assert "WARNING: --allow-older-bay" in allowed.stderr
    assert playbooks == [(verb, "production")]
    # Neither verb writes the file, also not with the flag.
    assert floor_of(m["a"])["deployed_with"] == "2.16.0"
    assert git(m["a"], "rev-parse", "HEAD") == head
    assert git(m["a"], "status", "--porcelain=v1") == ""


def test_the_flag_after_the_environment_is_rescued(
    machines: dict[str, Path], playbooks: list[tuple[str, str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    m = machines
    set_floor(m["a"], "2.16.0")
    runs(monkeypatch, "2.15.0")
    result = cli({"fleet": m["a"], "app": m["app"]}, "provision", "production", "--allow-older-bay")
    assert result.exit_code == 0, result.output
    assert playbooks == [("provision", "production")]


@pytest.mark.parametrize("verb", ["deploy", "provision"])
def test_playbook_verbs_with_no_file_or_a_newer_bay_go_on_and_write_nothing(
    machines: dict[str, Path], playbooks: list[tuple[str, str]], monkeypatch: pytest.MonkeyPatch,
    verb: str,
) -> None:
    m = machines
    runs(monkeypatch, "2.16.0")
    assert _op(m, verb).exit_code == 0
    assert not (m["a"] / FILE).exists()
    set_floor(m["a"], "2.15.0")
    assert _op(m, verb).exit_code == 0
    assert floor_of(m["a"])["deployed_with"] == "2.15.0"
    assert playbooks == [(verb, "production")] * 2


def test_playbook_verbs_refuse_an_unreadable_file(
    machines: dict[str, Path], playbooks: list[tuple[str, str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    m = machines
    (m["a"] / FILE).write_text("{broken\n")
    commit_all(m["a"], "a broken floor")
    git(m["a"], "push", "-q")
    runs(monkeypatch, "2.16.0")
    refused = _op(m, "deploy", "--allow-older-bay")
    assert getattr(refused.exception, "reason", None) == "compat_invalid", refused.output
    assert refused.exception.exit_code == 45
    assert playbooks == []


# ── bay fleet status ────────────────────────────────────────────────────────


def test_fleet_status_names_the_last_bay(
    machines: dict[str, Path], box: FakeBox, monkeypatch: pytest.MonkeyPatch
) -> None:
    m = machines
    set_floor(m["a"], "2.16.0")
    runs(monkeypatch, "2.15.0")
    w = {"fleet": m["a"], "app": m["app"]}
    text = cli(w, "fleet", "status")
    assert "last deployed with Bay v2.16.0 (this machine: v2.15.0)" in text.stdout
    doc = json.loads(cli(w, "fleet", "status", "--json").stdout)
    assert doc["bay_compat"]["state"] == "older"


# ── a lock from a newer Bay ─────────────────────────────────────────────────


def test_a_lock_from_a_newer_bay_names_self_update(world: dict[str, Path]) -> None:
    fleet = world["fleet"]
    path = lockfile.lock_path(fleet, "webapp")
    raw = json.loads(path.read_text())
    raw["lock_version"] = 3
    raw["new_key"] = {"from": "the future"}
    path.write_text(json.dumps(raw))
    want = (
        "projects/webapp/bay.lock: lock_version: is 3, but this Bay knows up to 2; "
        "run `bay self update`"
    )
    with pytest.raises(FleetError) as err:
        load_lock(path, fleet)
    assert err.value.lines == [want]
    with pytest.raises(LockTooNew) as too_new:
        lockfile.read(path)
    assert too_new.value.found == 3 and "run `bay self update`" in str(too_new.value)
    with pytest.raises(BayError) as bay_err:
        planmod.load_project(Context.for_fleet_root(fleet), "webapp", cwd=world["app"])
    assert str(bay_err.value) == want
    result = cli(world, "plan", "--json")
    assert want in result.stdout, result.output
