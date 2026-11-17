"""The verdict of a restore test (2.24.1): exact counts against exact counts.

2.24.0 compared the restored EXACT row counts with the live database's planner
ESTIMATES. That caught an empty or missing table and nothing else, and it
flagged estimate noise (a partition of 4,771,855 rows against an estimate of
4,175,470). `bay_restore_report` takes the exact leaf counts of both sides and
decides per table.

Input lines are what the two psql reads print, tab separated:
  T <table> <count>     an exact count of a leaf table
  P <parent> <leaf>     a partition of a partitioned parent
  L <table>             (live only) a leaf table of the live database
  E <table> <estimate>  (live only) the planner estimate of a leaf table
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _load():
    spec = importlib.util.spec_from_file_location(
        "bay_filters_for_restore_verdict", ROOT / "filter_plugins" / "bay_filters.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


report_of = _load().bay_restore_report


def _t(name: str, n: int) -> str:
    return f"T\t{name}\t{n}"


def _live(*tables: tuple[str, int], estimates: dict[str, int] | None = None,
          timed_out: tuple[str, ...] = (), parents: tuple[tuple[str, str], ...] = ()) -> list[str]:
    lines = [f"L\t{n}" for n, _ in tables]
    lines += [f"L\t{n}" for n in timed_out]
    lines += [_t(n, c) for n, c in tables]
    lines += [f"E\t{n}\t{e}" for n, e in (estimates or {}).items()]
    lines += [f"P\t{p}\t{leaf}" for p, leaf in parents]
    return lines


def _by_table(report: dict) -> dict[str, dict]:
    return {row["table"]: row for row in report["non_ok"]}


def test_a_table_that_matches_is_ok_and_not_listed():
    r = report_of([_t("public.notes", 300)], _live(("public.notes", 300)))
    assert r["verdict"] == "ok" and r["ok"] is True
    assert r["failures"] == [] and r["non_ok"] == []
    assert r["counts"] == {"public.notes": 300}
    assert r["tables"] == 1 and r["rows"] == 300 and r["live_rows"] == 300


def test_an_empty_restored_table_fails_when_live_has_rows():
    r = report_of([_t("public.notes", 0)], _live(("public.notes", 5)))
    assert r["verdict"] == "fail" and r["failures"] == ["public.notes"]
    row = _by_table(r)["public.notes"]
    assert (row["restored"], row["live"], row["delta"]) == (0, 5, -5)
    assert row["method"] == "exact" and row["verdict"] == "fail"
    assert "empty" in row["reason"]


def test_a_table_missing_from_the_restore_fails():
    r = report_of([_t("public.tags", 2)], _live(("public.tags", 2), ("public.notes", 9)))
    assert r["failures"] == ["public.notes"]
    row = _by_table(r)["public.notes"]
    assert row["restored"] is None and row["live"] == 9
    assert "missing" in row["reason"]


def test_a_table_empty_on_both_sides_is_ok():
    r = report_of([_t("public.audit", 0)], _live(("public.audit", 0)))
    assert r["verdict"] == "ok" and r["non_ok"] == []


def test_a_small_gap_passes_when_either_limit_is_not_passed():
    # 82M rows, 300 lost since the snapshot: under both limits.
    r = report_of([_t("public.big", 82_000_000)], _live(("public.big", 82_000_300)))
    assert r["verdict"] == "ok" and r["non_ok"] == []
    # 5% of 200 rows is over the percent limit but only 10 rows: under the row limit.
    r = report_of([_t("public.small", 190)], _live(("public.small", 200)))
    assert r["verdict"] == "ok"
    # 5,000 rows of 82M: over the row limit, far under the percent limit.
    r = report_of([_t("public.big", 82_000_000 - 5_000)], _live(("public.big", 82_000_000)))
    assert r["verdict"] == "ok"


def test_a_large_gap_fails_only_when_both_limits_are_passed():
    r = report_of([_t("public.orders", 9_000)], _live(("public.orders", 10_000)))
    assert r["verdict"] == "ok"  # 10% but exactly 1,000 rows: not more than the row limit
    r = report_of([_t("public.orders", 8_999)], _live(("public.orders", 10_000)))
    assert r["verdict"] == "fail" and r["failures"] == ["public.orders"]
    row = _by_table(r)["public.orders"]
    assert row["delta"] == -1_001 and "lost" in row["reason"]


def test_the_two_limits_are_arguments():
    restored, live = [_t("public.orders", 99)], _live(("public.orders", 100))
    assert report_of(restored, live)["verdict"] == "ok"
    strict = report_of(restored, live, max_loss_pct=0.5, max_loss_rows=0)
    assert strict["verdict"] == "fail"
    # Only one limit lowered: still ok.
    assert report_of(restored, live, max_loss_pct=0.5, max_loss_rows=1000)["verdict"] == "ok"
    assert report_of(restored, live, max_loss_pct=50, max_loss_rows=0)["verdict"] == "ok"
    assert strict["thresholds"] == {"max_loss_pct": 0.5, "max_loss_rows": 0}


def test_more_rows_restored_than_live_is_info_not_failure():
    r = report_of([_t("public.notes", 300)], _live(("public.notes", 10)))
    assert r["verdict"] == "ok" and r["failures"] == []
    row = _by_table(r)["public.notes"]
    assert row["verdict"] == "info" and row["delta"] == 290


def test_a_table_only_in_the_restore_is_info():
    r = report_of([_t("public.old", 4), _t("public.notes", 1)], _live(("public.notes", 1)))
    assert r["verdict"] == "ok"
    row = _by_table(r)["public.old"]
    assert row["verdict"] == "info" and row["live"] is None and "not in the live" in row["reason"]


def test_a_count_that_timed_out_falls_back_to_the_estimate_and_says_so():
    live = _live(("public.small", 5), timed_out=("public.big",), estimates={"public.big": 1_000_000})
    r = report_of([_t("public.small", 5), _t("public.big", 1_200_000)], live)
    assert r["verdict"] == "ok"  # an estimate is never a failure by itself
    row = _by_table(r)["public.big"]
    assert row["method"] == "estimate" and row["live"] == 1_000_000
    assert r["estimated"] == ["public.big"]


def test_an_estimate_still_catches_an_empty_or_missing_table():
    live = _live(timed_out=("public.big", "public.gone"),
                 estimates={"public.big": 1_000_000, "public.gone": 50})
    r = report_of([_t("public.big", 0)], live)
    assert r["verdict"] == "fail"
    assert sorted(r["failures"]) == ["public.big", "public.gone"]


def test_an_estimate_never_fails_a_partial_loss():
    live = _live(timed_out=("public.big",), estimates={"public.big": 1_000_000})
    r = report_of([_t("public.big", 400_000)], live)
    assert r["verdict"] == "ok" and r["failures"] == []
    assert _by_table(r)["public.big"]["method"] == "estimate"


def test_a_partitioned_parent_is_the_sum_of_its_partitions_and_not_in_the_total():
    parents = (("public.events", "public.events_2026_10"), ("public.events", "public.events_2026_11"))
    restored = [_t("public.events_2026_10", 40), _t("public.events_2026_11", 60),
                _t("public.notes", 7),
                "P\tpublic.events\tpublic.events_2026_10", "P\tpublic.events\tpublic.events_2026_11"]
    live = _live(("public.events_2026_10", 40), ("public.events_2026_11", 62), ("public.notes", 7),
                 parents=parents)
    r = report_of(restored, live)
    assert r["verdict"] == "ok"
    assert r["parents"] == {"public.events": {"restored": 100, "live": 102,
                                              "partitions": ["public.events_2026_10",
                                                             "public.events_2026_11"]}}
    assert r["tables"] == 3, "leaf tables only"
    assert r["rows"] == 107 and r["live_rows"] == 109, "no double count of the parent"
    assert "public.events" not in r["counts"]


def test_a_parent_has_no_verdict_of_its_own_but_its_partition_fails():
    restored = [_t("public.events_2026_11", 0), "P\tpublic.events\tpublic.events_2026_11"]
    live = _live(("public.events_2026_11", 82_000_000), parents=(("public.events", "public.events_2026_11"),))
    r = report_of(restored, live)
    assert r["failures"] == ["public.events_2026_11"], "the partition, not the parent"
    assert r["parents"]["public.events"]["restored"] == 0


def test_a_nested_parent_sums_every_leaf_below_it():
    restored = [_t("public.a_x_1", 1), _t("public.a_x_2", 2), _t("public.a_y", 4),
                "P\tpublic.a\tpublic.a_x_1", "P\tpublic.a\tpublic.a_x_2", "P\tpublic.a\tpublic.a_y",
                "P\tpublic.a_x\tpublic.a_x_1", "P\tpublic.a_x\tpublic.a_x_2"]
    r = report_of(restored, _live(("public.a_x_1", 1), ("public.a_x_2", 2), ("public.a_y", 4)))
    assert r["parents"]["public.a"]["restored"] == 7
    assert r["parents"]["public.a_x"]["restored"] == 3
    assert r["rows"] == 7


def test_a_live_database_that_could_not_be_read_is_unverified_not_failed():
    r = report_of([_t("public.notes", 3)], [])
    assert r["verdict"] == "unverified" and r["compared"] is False
    assert r["failures"] == [] and r["ok"] is True


def test_no_restored_table_is_never_ok():
    r = report_of([], _live(("public.notes", 3)))
    assert r["tables"] == 0 and r["ok"] is False
    assert r["failures"] == ["public.notes"]


def test_the_report_carries_the_times():
    r = report_of([_t("public.notes", 3)], _live(("public.notes", 3)),
                  live_started=1_800_000_000, live_finished=1_800_000_007)
    assert r["live_counted_at"] == "2027-01-15T08:00:00Z"
    assert r["live_count_seconds"] == 7


def test_table_names_with_a_dot_or_space_survive():
    name = 'public.My Table'
    r = report_of([_t(name, 1)], _live((name, 1)))
    assert r["counts"] == {name: 1}


@pytest.mark.parametrize("bad", ["x\ty", "T\tonly", ""])
def test_junk_lines_are_ignored(bad):
    r = report_of([bad, _t("public.notes", 2)], [bad] + _live(("public.notes", 2)))
    assert r["verdict"] == "ok" and r["tables"] == 1
