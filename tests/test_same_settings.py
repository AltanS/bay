"""Since 2.27.0: bay show judges a pin that is older than WANTED by the settings.

A push that changes code only leaves the pin where it is. The box builds and
deploys it, so nothing waits for ``bay up``: ``bay show`` says ``building`` and
then ``ok``, never ``behind`` or ``ahead``. A settings change (the ``bay.toml``
or a file its mounts read) still waits for ``bay up``.

The pure rows of ``env_status`` run on plain values. The rest run on throwaway
git repos with the fake box of ``test_plan_up``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_plan_up import (  # noqa: E402,F401  (fixtures are used by name)
    FakeBox,
    _build_app,
    _code_deploys,
    _git_identity,
    _in_fleet,
    _push_code,
    _stamp,
    box,
    cli,
    commit_all,
    do_up,
    edit_app,
    git,
    make,
    project,
    world,
)

from bay_cli import apply as applymod  # noqa: E402
from bay_cli import samesettings  # noqa: E402

PIN = "a" * 40
WANTED = "b" * 40
W12, P12 = WANTED[:12], PIN[:12]


# ── env_status: every row ───────────────────────────────────────────────────


def _status(
    same: bool | None,
    commits: dict[str, str | None] | None,
    *,
    track: str = "branch",
    frozen: bool = False,
    in_fleet: bool = False,
    record: dict[str, Any] | None = None,
    wanted: str | None = WANTED,
) -> tuple[str, str]:
    rec: dict[str, Any] = {"commit": PIN, "result": "ok", "last_receipt_sha256": "r"}
    rec.update(record or {})
    if frozen:
        rec["frozen"] = True
    return applymod.env_status(
        rec,
        PIN,
        wanted,
        {"checked": True, "receipt_sha256": "r"},
        commits,
        in_fleet=in_fleet,
        same_settings=same,
        track=track,
    )


def test_building_is_a_status_word() -> None:
    assert "building" in applymod.STATUS_WORDS


def test_same_settings_and_the_box_runs_wanted_is_ok() -> None:
    word, why = _status(True, {"web": W12})
    assert word == "ok"
    assert why == (
        f"the box runs {W12} (a push deployed it); the settings are the pinned ones "
        f"({P12}); no bay up is needed"
    )


def test_same_settings_with_no_build_container_is_ok() -> None:
    word, why = _status(True, {})
    assert word == "ok" and "no bay up is needed" in why
    assert _status(True, None)[0] == "ok"


def test_same_settings_in_the_fleet_is_ok() -> None:
    word, why = _status(True, {"web": None}, in_fleet=True)
    assert word == "ok"
    assert why == f"the settings at {W12} are the pinned ones ({P12}); no bay up is needed"


@pytest.mark.parametrize("commits", [{"web": "c" * 12}, {"web": None}, {"web": W12, "db": None}])
def test_same_settings_and_the_box_is_not_there_yet_is_building(
    commits: dict[str, str | None],
) -> None:
    word, why = _status(True, commits)
    assert word == "building"
    assert why == (
        f"the push {W12} is not on the box yet (its build runs or failed: "
        "bay build status); no bay up is needed"
    )


@pytest.mark.parametrize("kwargs", [{"track": "pin"}, {"frozen": True}])
def test_same_settings_with_track_pin_or_frozen_is_behind(kwargs: dict[str, Any]) -> None:
    word, why = _status(True, {"web": "c" * 12}, **kwargs)
    assert word == "behind"
    assert why == (
        "only bay up deploys code here (track pin, or frozen after a rollback): run bay plan"
    )
    # When the box already runs WANTED nothing waits, whatever the track.
    assert _status(True, {"web": W12}, **kwargs)[0] == "ok"


def test_other_settings_and_the_box_runs_wanted_is_ahead() -> None:
    word, why = _status(False, {"web": W12})
    assert word == "ahead"
    assert why == (
        f"the box runs {W12} (a push deployed it); the settings at {W12} differ from the "
        f"pinned ones ({P12}): run bay plan"
    )


@pytest.mark.parametrize("commits", [{"web": "c" * 12}, {"web": None}, {}, None])
def test_other_settings_and_the_box_is_not_there_is_behind(
    commits: dict[str, str | None] | None,
) -> None:
    word, why = _status(False, commits)
    assert word == "behind"
    assert why == (
        f"a settings change waits for bay up: bay.toml (or a file it mounts) at {W12} "
        f"differs from the pin {P12}: run bay plan"
    )
    assert _status(False, commits, in_fleet=True)[0] == "behind"


def test_other_settings_in_the_fleet_never_say_ahead() -> None:
    # No container runs a fleet commit, whatever the receipt names.
    assert _status(False, {"web": W12}, in_fleet=True)[0] == "behind"


def test_unreadable_settings_fall_back_to_the_commit_ids() -> None:
    word, why = _status(None, {"web": W12})
    assert (word, why) == (
        "ahead",
        f"the box runs {W12} (a push deployed it); the fleet pins {P12}: "
        "run bay up to pin it",
    )
    word, why = _status(None, {"web": None})
    assert word == "behind" and why.endswith("and the box does not run it yet")
    assert _status(None, {}, in_fleet=True) == (
        "behind",
        f"the project is at {W12}, the fleet pins {P12}",
    )


def test_the_checks_before_the_settings_stay_first() -> None:
    assert _status(True, {"web": W12}, record={"result": "failed"})[0] == "HALF"
    assert _status(True, {"web": W12}, record={"last_receipt_sha256": "x"})[0] == "drift"
    assert _status(True, {"web": W12}, record={"commit": "d" * 40})[0] == "drift"
    assert _status(True, {"web": W12}, record={"commit": None})[0] == "unknown"
    # WANTED is the pin: the settings do not matter.
    assert _status(True, {"web": W12}, wanted=PIN)[0] == "ok"
    assert _status(False, {"web": W12}, wanted=PIN)[0] == "ok"


# ── same_settings: real repos ───────────────────────────────────────────────

MOUNT_TOML = (
    '\n[[mounts]]\npath = "/etc/app/app.yaml"\nfrom = "conf/app.yaml"\n'
    '\n[[mounts]]\npath = "/etc/app/rules"\nfrom = "rules/"\n'
)


def _app_with_mounts(world: dict[str, Path]) -> str:
    """The webapp builds from source and mounts conf/app.yaml and the folder rules/."""
    _build_app(world)
    path = world["app"] / "bay.toml"
    # Mounts are top-level tables: put them before [build].
    text = path.read_text().replace("\n[build]\n", MOUNT_TOML + "\n[build]\n", 1)
    path.write_text(text)
    (world["app"] / "conf").mkdir()
    (world["app"] / "conf" / "app.yaml").write_text("a: 1\n")
    (world["app"] / "rules").mkdir()
    (world["app"] / "rules" / "one.yaml").write_text("one: 1\n")
    sha = commit_all(world["app"], "mounts")
    git(world["app"], "push", "-q", "origin", "main")
    return sha


def _same(world: dict[str, Path], wanted: str, pin: str) -> bool | None:
    return samesettings.same_settings(project(world), wanted, pin)


def test_a_code_only_push_has_the_same_settings(world: dict[str, Path], box: FakeBox) -> None:
    pin = _build_app(world)
    new = _push_code(world, "v2")
    assert _same(world, new, pin) is True
    assert _same(world, new, new) is None  # one commit: nothing to compare
    assert _same(world, new, None) is None


def test_a_comment_only_edit_is_the_same_settings(world: dict[str, Path], box: FakeBox) -> None:
    pin = _build_app(world)
    path = world["app"] / "bay.toml"
    path.write_text("# a new comment\n" + path.read_text().replace("port = 3000", "port   =   3000"))
    new = commit_all(world["app"], "comment only")
    assert _same(world, new, pin) is True


def test_a_value_change_is_other_settings(world: dict[str, Path], box: FakeBox) -> None:
    pin = _build_app(world)
    new = edit_app(world, 'LOG_LEVEL = "info"', 'LOG_LEVEL = "debug"')
    assert _same(world, new, pin) is False


def test_a_mount_file_change_is_other_settings(world: dict[str, Path], box: FakeBox) -> None:
    pin = _app_with_mounts(world)
    other = _push_code(world, "v2")
    assert _same(world, other, pin) is True  # code only: the mounts did not change
    (world["app"] / "conf" / "app.yaml").write_text("a: 2\n")
    changed = commit_all(world["app"], "conf")
    assert _same(world, changed, pin) is False


def test_a_file_under_a_mounted_directory_counts(world: dict[str, Path], box: FakeBox) -> None:
    pin = _app_with_mounts(world)
    (world["app"] / "rules" / "two.yaml").write_text("two: 1\n")
    added = commit_all(world["app"], "a new rule")
    assert _same(world, added, pin) is False


def test_a_mount_that_wanted_dropped_is_other_settings(
    world: dict[str, Path], box: FakeBox
) -> None:
    """A mount that WANTED dropped is a settings change (the toml differs)."""
    pin = _app_with_mounts(world)
    path = world["app"] / "bay.toml"
    path.write_text(path.read_text().replace(MOUNT_TOML, "\n"))
    dropped = commit_all(world["app"], "no mounts")
    # The mounted files are identical at both commits; the toml differs.
    assert _same(world, dropped, pin) is False


def test_an_unreadable_commit_gives_none(world: dict[str, Path], box: FakeBox) -> None:
    pin = _build_app(world)
    new = _push_code(world, "v2")
    assert _same(world, new, "0" * 40) is None
    assert _same(world, "0" * 40, pin) is None


def test_a_toml_that_is_not_there_or_not_valid_gives_none(
    world: dict[str, Path], box: FakeBox
) -> None:
    pin = _build_app(world)
    (world["app"] / "bay.toml").write_text("not = [valid\n")
    broken = commit_all(world["app"], "broken")
    assert _same(world, broken, pin) is None
    (world["app"] / "bay.toml").unlink()
    gone = commit_all(world["app"], "no toml")
    assert _same(world, gone, pin) is None


# ── bay show ────────────────────────────────────────────────────────────────


def _row(world: dict[str, Path]) -> dict[str, Any]:
    result = cli(world, "show", "--json")
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)["envs"][0]


def test_show_after_a_code_push_says_building_then_ok(
    world: dict[str, Path], box: FakeBox, monkeypatch: pytest.MonkeyPatch
) -> None:
    _code_deploys(monkeypatch, box)
    pin = _build_app(world)
    assert do_up(world)["result"] == "ok"
    _stamp(box, "webapp", pin)
    row = _row(world)
    assert row["status"] == "ok" and row["same_settings"] is None  # WANTED is the pin

    wanted = _push_code(world, "v2")
    row = _row(world)
    assert (row["status"], row["same_settings"]) == ("building", True), row["reason"]
    assert "bay build status" in row["reason"] and "no bay up is needed" in row["reason"]
    assert cli(world, "show").output.count("building") >= 1

    _stamp(box, "webapp", wanted)
    row = _row(world)
    assert (row["status"], row["same_settings"]) == ("ok", True)
    assert row["reason"] == (
        f"the box runs {wanted[:12]} (a push deployed it); the settings are the pinned "
        f"ones ({pin[:12]}); no bay up is needed"
    )
    # The pin did not move, and no bay up ran.
    assert row["pinned"]["commit"] == pin


def test_show_after_a_code_push_in_track_pin_says_behind(
    world: dict[str, Path], box: FakeBox, monkeypatch: pytest.MonkeyPatch
) -> None:
    _code_deploys(monkeypatch, box)
    pin = _build_app(world, track="pin")
    assert do_up(world)["result"] == "ok"
    _stamp(box, "webapp", pin)
    _push_code(world, "v2")
    row = _row(world)
    assert (row["status"], row["same_settings"]) == ("behind", True)
    assert row["reason"].startswith("only bay up deploys code here")


def test_show_after_a_code_push_in_a_frozen_env_says_behind(
    world: dict[str, Path], box: FakeBox, monkeypatch: pytest.MonkeyPatch
) -> None:
    _code_deploys(monkeypatch, box)
    pin = _build_app(world)
    assert do_up(world)["result"] == "ok"
    _stamp(box, "webapp", pin)
    from bay_cli import lockfile

    lock_path = lockfile.lock_path(world["fleet"], "webapp")
    raw = lockfile.read(lock_path)
    assert raw is not None
    raw["envs"]["production"]["frozen"] = True
    raw["envs"]["production"]["frozen_commit"] = pin
    lockfile.write(lock_path, raw)
    _push_code(world, "v2")
    row = _row(world)
    assert (row["status"], row["same_settings"]) == ("behind", True)
    assert "frozen after a rollback" in row["reason"]


def test_show_after_a_settings_push_says_behind_then_ahead(
    world: dict[str, Path], box: FakeBox, monkeypatch: pytest.MonkeyPatch
) -> None:
    _code_deploys(monkeypatch, box)
    pin = _build_app(world)
    assert do_up(world)["result"] == "ok"
    _stamp(box, "webapp", pin)
    wanted = edit_app(world, 'LOG_LEVEL = "info"', 'LOG_LEVEL = "debug"')
    row = _row(world)
    assert (row["status"], row["same_settings"]) == ("behind", False)
    assert row["reason"] == (
        f"a settings change waits for bay up: bay.toml (or a file it mounts) at "
        f"{wanted[:12]} differs from the pin {pin[:12]}: run bay plan"
    )
    _stamp(box, "webapp", wanted)
    row = _row(world)
    assert (row["status"], row["same_settings"]) == ("ahead", False)
    assert f"the settings at {wanted[:12]} differ from the pinned ones" in row["reason"]
    # bay up pins it: ok again, and the key is null.
    assert do_up(world)["result"] == "ok"
    _stamp(box, "webapp", wanted)
    row = _row(world)
    assert (row["status"], row["same_settings"]) == ("ok", None)


def test_show_after_a_mount_file_push_says_behind(
    world: dict[str, Path], box: FakeBox, monkeypatch: pytest.MonkeyPatch
) -> None:
    _code_deploys(monkeypatch, box)
    pin = _app_with_mounts(world)
    assert do_up(world)["result"] == "ok"
    _stamp(box, "webapp", pin)
    (world["app"] / "conf" / "app.yaml").write_text("a: 2\n")
    commit_all(world["app"], "conf")
    git(world["app"], "push", "-q", "origin", "main")
    row = _row(world)
    assert (row["status"], row["same_settings"]) == ("behind", False)


def test_show_after_a_comment_only_push_is_not_behind(
    world: dict[str, Path], box: FakeBox, monkeypatch: pytest.MonkeyPatch
) -> None:
    _code_deploys(monkeypatch, box)
    pin = _build_app(world)
    assert do_up(world)["result"] == "ok"
    _stamp(box, "webapp", pin)
    path = world["app"] / "bay.toml"
    path.write_text("# only a comment\n" + path.read_text())
    wanted = commit_all(world["app"], "comment")
    git(world["app"], "push", "-q", "origin", "main")
    _stamp(box, "webapp", wanted)
    row = _row(world)
    assert (row["status"], row["same_settings"]) == ("ok", True)


def test_show_without_a_build_container_is_ok_after_a_code_push(
    world: dict[str, Path], box: FakeBox
) -> None:
    """An image-only project has no build container: a code commit changes nothing."""
    assert do_up(world)["result"] == "ok"
    _push_code(world, "v2")
    row = _row(world)
    assert (row["status"], row["same_settings"]) == ("ok", True)


def test_show_falls_back_to_the_commit_ids_when_the_settings_cannot_be_read(
    world: dict[str, Path], box: FakeBox, monkeypatch: pytest.MonkeyPatch
) -> None:
    _code_deploys(monkeypatch, box)
    pin = _build_app(world)
    assert do_up(world)["result"] == "ok"
    _stamp(box, "webapp", pin)
    wanted = _push_code(world, "v2")
    monkeypatch.setattr(samesettings, "_settings_at", lambda *a, **k: None)
    row = _row(world)
    assert (row["status"], row["same_settings"]) == ("behind", None)
    assert row["reason"].endswith("and the box does not run it yet")
    _stamp(box, "webapp", wanted)
    row = _row(world)
    assert (row["status"], row["same_settings"]) == ("ahead", None)
    assert row["reason"].endswith("run bay up to pin it")


def test_show_keeps_every_old_key(world: dict[str, Path], box: FakeBox) -> None:
    assert do_up(world)["result"] == "ok"
    row = _row(world)
    for key in (
        "env", "box", "box_env", "status", "reason", "half_boxes", "next", "pinned",
        "running", "fleet_drift", "same_settings",
    ):
        assert key in row


# ── a project in the fleet ──────────────────────────────────────────────────


def _status_row(world: dict[str, Path]) -> dict[str, Any]:
    result = cli(world, "show", "status", "--json", cwd=world["fleet"])
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)["envs"][0]


def test_in_fleet_a_comment_edit_is_ok_and_a_value_edit_is_behind(
    world: dict[str, Path], box: FakeBox
) -> None:
    path = _in_fleet(world)
    assert cli(world, "up", "--project", "status", "--json", cwd=world["fleet"]).exit_code == 0
    assert _status_row(world)["same_settings"] is None

    path.write_text("# only a comment\n" + path.read_text())
    commit_all(world["fleet"], "status comment")
    row = _status_row(world)
    assert (row["status"], row["same_settings"]) == ("ok", True), row["reason"]
    assert row["reason"].endswith("are the pinned ones (" + row["pinned"]["commit"][:12] + "); no bay up is needed")

    path.write_text(path.read_text().replace('MODE = "one"', 'MODE = "two"'))
    wanted = commit_all(world["fleet"], "status two")
    row = _status_row(world)
    assert (row["status"], row["same_settings"]) == ("behind", False)
    assert f"at {wanted[:12]} differs from the pin" in row["reason"]


def test_in_fleet_a_mount_file_edit_is_behind(world: dict[str, Path], box: FakeBox) -> None:
    folder = world["fleet"] / "projects" / "status"
    path = _in_fleet(world)
    mount = '\n[[mounts]]\npath = "/etc/app/app.yaml"\nfrom = "conf/app.yaml"\n'
    path.write_text(path.read_text().replace("\n[deploy", mount + "\n[deploy", 1))
    (folder / "conf").mkdir()
    (folder / "conf" / "app.yaml").write_text("a: 1\n")
    commit_all(world["fleet"], "status mount")
    assert cli(world, "up", "--project", "status", "--json", cwd=world["fleet"]).exit_code == 0
    (folder / "conf" / "app.yaml").write_text("a: 2\n")
    commit_all(world["fleet"], "status conf")
    row = _status_row(world)
    assert (row["status"], row["same_settings"]) == ("behind", False)


# ── bay plan ────────────────────────────────────────────────────────────────

NOTE = "no settings change: bay up is not needed (it would only move the pin)"


def test_plan_notes_that_a_code_only_push_needs_no_bay_up(
    world: dict[str, Path], box: FakeBox, monkeypatch: pytest.MonkeyPatch
) -> None:
    _code_deploys(monkeypatch, box)
    pin = _build_app(world)
    assert do_up(world)["result"] == "ok"
    _stamp(box, "webapp", pin)
    assert NOTE not in make(world)["notes"]  # WANTED is the pin

    wanted = _push_code(world, "v2")
    _stamp(box, "webapp", wanted)
    plan = make(world)
    assert plan["steps"] == [] and plan["exit_code"] == 0
    assert NOTE in plan["notes"]
    assert f"code at {wanted[:12]}, config pinned at {pin[:12]}" in plan["notes"]
    result = cli(world, "plan", "--json")
    assert result.exit_code == 0, result.output
    assert NOTE in json.loads(result.stdout)["notes"]


def test_plan_has_no_such_note_when_the_settings_change(
    world: dict[str, Path], box: FakeBox, monkeypatch: pytest.MonkeyPatch
) -> None:
    _code_deploys(monkeypatch, box)
    pin = _build_app(world)
    assert do_up(world)["result"] == "ok"
    _stamp(box, "webapp", pin)
    edit_app(world, 'LOG_LEVEL = "info"', 'LOG_LEVEL = "debug"')
    plan = make(world)
    assert plan["steps"] and NOTE not in plan["notes"]


def test_plan_has_no_such_note_while_a_step_waits(
    world: dict[str, Path], box: FakeBox, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Track pin: the same settings, but bay up deploys the code, so it is not a no-op."""
    _code_deploys(monkeypatch, box)
    pin = _build_app(world, track="pin")
    assert do_up(world)["result"] == "ok"
    _stamp(box, "webapp", pin)
    _push_code(world, "v2")
    plan = make(world)
    assert [s["kind"] for s in plan["steps"]] == ["image"]
    assert NOTE not in plan["notes"]


def test_the_note_does_not_change_the_plan_id(
    world: dict[str, Path], box: FakeBox, monkeypatch: pytest.MonkeyPatch
) -> None:
    _code_deploys(monkeypatch, box)
    pin = _build_app(world)
    assert do_up(world)["result"] == "ok"
    _stamp(box, "webapp", pin)
    wanted = _push_code(world, "v2")
    _stamp(box, "webapp", wanted)
    before = make(world)
    monkeypatch.setattr(samesettings, "no_change_note", lambda *a, **k: None)
    assert make(world)["plan_id"] == before["plan_id"]
