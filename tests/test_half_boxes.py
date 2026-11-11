"""A HALF record names the boxes that still need a deploy (2.18.1).

A later ``bay up`` drops a box from ``half_boxes`` only when its scope covered
that box and the box passed. A box outside the scope stays marked, and the
project stays HALF until no box is left. Uses the fake box and the two-box
fleet of test_plan_up (status in the fleet on box-1, webapp on box-2).
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
    _edit_in_fleet,
    _git_identity,
    _two_boxes_status_on_one,
    _up_status,
    box,
    cli,
    commit_all,
    cx_of,
    do_up,
    world,
)

from bay_cli import apply as applymod  # noqa: E402
from bay_cli import fleetsync, lockfile  # noqa: E402
from bay_cli import plan as planmod  # noqa: E402

ALL_BOXES = "bay up production --project status --all-boxes"


def _record(world: dict[str, Path]) -> dict[str, Any]:
    raw = lockfile.read(lockfile.lock_path(world["fleet"], "status"))
    assert raw is not None
    return dict(raw["envs"]["production"])


def _status_proj(world: dict[str, Path]) -> planmod.ProjectRef:
    return planmod.load_project(cx_of(world), "status", cwd=world["fleet"])


def _start(world: dict[str, Path]) -> Path:
    """Both projects deployed and confirmed; returns status's bay.toml."""
    status = _two_boxes_status_on_one(world, "box-2")
    do_up(world)
    _up_status(world)
    assert _record(world)["result"] == "ok"
    return status


def _fail_on(world: dict[str, Path], box: FakeBox, status: Path, boxes: set[str]) -> None:
    """A status run over both boxes (--all-boxes) that fails on ``boxes``."""
    _edit_in_fleet(status, world, 'MODE = "one"', 'MODE = "two"')
    box.fail_boxes = set(boxes)
    with pytest.raises(applymod.DeployFailed):
        _up_status(world, all_boxes=True)
    box.fail_boxes = set()


def _show_row(world: dict[str, Path]) -> dict[str, Any]:
    shown = json.loads(cli(world, "show", "status", "--json", cwd=world["fleet"]).stdout)
    return shown["envs"][0]


# ── (a) a later run without the failed box keeps it ─────────────────────────


def test_a_later_run_without_the_failed_box_keeps_the_project_half(
    world: dict[str, Path], box: FakeBox
) -> None:
    status = _start(world)
    _fail_on(world, box, status, {"box-2"})
    record = _record(world)
    assert (record["result"], record["half_boxes"]) == ("failed", ["box-2"])

    # The next plan covers box-1 only, and says that box-2 stays HALF.
    plan = planmod.make_plan(_status_proj(world), planmod.PlanOptions())
    assert plan["scope"]["boxes"] == ["box-1"]
    note = next(n for n in plan["notes"] if "is HALF" in n)
    assert "box box-2" in note and note.endswith(ALL_BOXES)

    result = _up_status(world)
    assert result["result"] == "ok"  # the run itself passed on box-1
    record = _record(world)
    assert (record["result"], record["half_boxes"]) == ("failed", ["box-2"])
    row = next(r for r in result["pinned"] if r["project"] == "status")
    assert (row["result"], row["half_boxes"]) == ("failed", ["box-2"])
    assert any("status production stays HALF" in n and ALL_BOXES in n for n in result["notes"])

    shown = _show_row(world)
    assert shown["status"] == "HALF"
    assert (shown["half_boxes"], shown["next"]) == (["box-2"], ALL_BOXES)
    assert f"box box-2 still needs a deploy: run {ALL_BOXES}" in shown["reason"]


def test_another_projects_run_without_the_failed_box_keeps_the_project_half(
    world: dict[str, Path], box: FakeBox
) -> None:
    status = _start(world)
    _fail_on(world, box, status, {"box-2"})
    do_up(world)  # webapp: box-2 only, status is not part of it
    assert _record(world)["half_boxes"] == ["box-2"]


# ── (b) a later run with that box passing clears it ─────────────────────────


def test_a_later_run_that_covers_the_box_and_passes_clears_it(
    world: dict[str, Path], box: FakeBox
) -> None:
    status = _start(world)
    _fail_on(world, box, status, {"box-2"})
    _up_status(world)
    assert _record(world)["result"] == "failed"

    result = _up_status(world, all_boxes=True)
    assert result["result"] == "ok"
    record = _record(world)
    assert record["result"] == "ok" and "half_boxes" not in record
    assert not any("stays HALF" in n for n in result["notes"])
    shown = _show_row(world)
    assert shown["status"] != "HALF" and shown["half_boxes"] == [] and shown["next"] is None


def test_a_covered_box_that_fails_again_stays_marked(
    world: dict[str, Path], box: FakeBox
) -> None:
    status = _start(world)
    _fail_on(world, box, status, {"box-2"})
    box.fail_boxes = {"box-2"}
    with pytest.raises(applymod.DeployFailed):
        _up_status(world, all_boxes=True)
    assert _record(world)["half_boxes"] == ["box-2"]


# ── (c) two failed boxes, one fixed ─────────────────────────────────────────


def test_two_failed_boxes_one_fixed_one_still_marked(
    world: dict[str, Path], box: FakeBox
) -> None:
    status = _start(world)
    _fail_on(world, box, status, {"box-1", "box-2"})
    record = _record(world)
    assert (record["result"], record["half_boxes"]) == ("failed", ["box-1", "box-2"])

    _up_status(world)  # box-1 passes, box-2 is outside the scope
    record = _record(world)
    assert (record["result"], record["half_boxes"]) == ("failed", ["box-2"])

    _up_status(world, all_boxes=True)
    assert _record(world)["result"] == "ok"


# ── (d) a lock written before 2.18.1 ────────────────────────────────────────


def _legacy_failed(world: dict[str, Path]) -> None:
    """status failed, as 2.18.0 wrote it: no ``half_boxes``."""
    path = lockfile.lock_path(world["fleet"], "status")
    raw = lockfile.read(path)
    assert raw is not None
    raw["envs"]["production"]["result"] = "failed"
    raw["envs"]["production"].pop("half_boxes", None)
    lockfile.write(path, raw)
    commit_all(world["fleet"], "a failed record from 2.18.0")


def test_an_old_lock_with_no_box_list_waits_for_every_box(
    world: dict[str, Path], box: FakeBox
) -> None:
    _start(world)
    _legacy_failed(world)
    assert lockfile.half_boxes(_record(world), ["box-1", "box-2"]) == ["box-1", "box-2"]
    shown = _show_row(world)
    assert (shown["status"], shown["half_boxes"]) == ("HALF", ["box-1", "box-2"])

    _up_status(world)  # box-1 only
    record = _record(world)
    assert (record["result"], record["half_boxes"]) == ("failed", ["box-2"])

    _up_status(world, all_boxes=True)
    assert _record(world)["result"] == "ok"


def test_half_boxes_of_a_record() -> None:
    env = ["eu", "na"]
    assert lockfile.half_boxes({"result": "ok", "half_boxes": ["na"]}, env) == []
    assert lockfile.half_boxes({"result": "failed", "half_boxes": ["na"]}, env) == ["na"]
    assert lockfile.half_boxes({"result": "pending", "box": "eu"}, []) == ["eu"]
    assert lockfile.half_boxes({"result": "failed", "box": "eu"}, env) == ["eu", "na"]
    record: dict[str, Any] = {"result": "failed", "half_boxes": ["na"]}
    lockfile.settle_half(record, [])
    assert record == {"result": "ok"}
    lockfile.settle_half(record, ["na", "eu"])
    assert record == {"result": "failed", "half_boxes": ["eu", "na"]}


def test_the_lock_schema_takes_half_boxes() -> None:
    raw = lockfile.new_lock("status", repo=None)
    raw["envs"]["production"] = {"box": "eu", "result": "failed", "half_boxes": ["na"]}
    assert lockfile.problems(raw) == []
    raw["envs"]["production"]["half_boxes"] = []
    assert lockfile.problems(raw) != []


# ── rollback and take-over do not clear it ──────────────────────────────────


def test_rollback_does_not_clear_a_box_it_did_not_deploy(
    world: dict[str, Path], box: FakeBox
) -> None:
    status = _start(world)
    _fail_on(world, box, status, {"box-2"})
    result = applymod.rollback(_status_proj(world), planmod.PlanOptions())
    assert result["scope"]["boxes"] == ["box-1"]
    record = _record(world)
    assert (record["result"], record["half_boxes"]) == ("failed", ["box-2"])


@pytest.mark.parametrize("listed", [True, False], ids=["half_boxes", "claim_before_2_18_1"])
def test_take_over_keeps_the_boxes_of_the_claim_it_replaces(
    world: dict[str, Path], box: FakeBox, listed: bool
) -> None:
    """The claim of another machine waits for box-2 (named, or every box when it
    names none). A take-over run on box-1 leaves box-2 marked."""
    _start(world)
    path = lockfile.lock_path(world["fleet"], "status")
    raw = lockfile.read(path)
    assert raw is not None
    raw["envs"]["production"].update(
        {
            "result": "pending",
            "claimed_by": "machine-z",
            "claimed_at": fleetsync.now_iso(),
            "claimed_run": "",
            **({"half_boxes": ["box-2"]} if listed else {}),
        }
    )
    lockfile.write(path, raw)
    commit_all(world["fleet"], "a fresh claim of another machine")
    result = applymod.up(_status_proj(world), planmod.PlanOptions(), take_over=True)
    assert result["took_over"] is not None and result["result"] == "ok"
    record = _record(world)
    assert (record["result"], record["half_boxes"]) == ("failed", ["box-2"])
    assert "claimed_by" not in record


# ── no host map at receipt time ─────────────────────────────────────────────


def test_a_run_with_no_host_map_gives_no_result_to_a_skipped_box(
    world: dict[str, Path], box: FakeBox, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The env-wide rule (no host map) names every box of the box env. A scoped
    run still gives a result only to the boxes of its scope."""
    from test_plan_up import edit_app

    _start(world)
    edit_app(world, 'LOG_LEVEL = "info"', 'LOG_LEVEL = "debug"')
    box.fail_boxes = {"box-2"}
    with pytest.raises(applymod.DeployFailed):
        do_up(world)  # webapp on box-2
    box.fail_boxes = set()
    webapp = lockfile.lock_path(world["fleet"], "webapp")
    before = webapp.read_bytes()
    monkeypatch.setattr(applymod, "box_names", lambda cx, env: (None, "no inventory"))
    result = _up_status(world)  # box-1 only
    assert result["scope"]["boxes"] == ["box-1"]
    assert webapp.read_bytes() == before
