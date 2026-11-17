"""`bay backup status`: compare the backup targets a box should have with what it has.

backup-status.yml writes one JSON document (``hosts``: every host of the env;
``results``: one entry per host that answered, with the targets the fleet
config gives it, ``expected`` and ``targets``, and what the box reported,
``probe``). This module turns it into rows and one exit code. It reads no
file and runs nothing, so it is tested on plain dicts.

Exit codes (the lowest non-zero one wins, a missing target is the worst news):

* ``0`` every expected target has a script, an enabled and active timer, and a
  good run within its ``max_age_hours`` (a target younger than that is fine).
* ``1`` an expected target has no script or no running timer: nothing backs it
  up. This is the state a box is in when the backup role never ran on it.
* ``2`` a target had no good run within ``max_age_hours``.
* ``3`` a box did not answer, so its targets are unknown.
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import asdict, dataclass
from typing import Any

OK, MISSING, STALE, NO_ANSWER = 0, 1, 2, 3
_PRIORITY = (MISSING, STALE, NO_ANSWER)


@dataclass
class Row:
    host: str
    target: str
    state: str  # ok | new | no_record | missing | stale | extra | disabled | no_answer
    detail: str = ""
    method: str = ""
    last_ok: int | None = None
    next_run: str = ""
    snapshots: int | None = None
    latest_snapshot: str | None = None
    deep_error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _missing_reason(seen: dict[str, Any]) -> str:
    why = []
    if not seen.get("script"):
        why.append("no script")
    if seen.get("timer_enabled") != "enabled":
        why.append("timer not enabled")
    elif seen.get("timer_active") != "active":
        why.append("timer not active")
    return ", ".join(why)


def _deep(row: Row, seen: dict[str, Any]) -> None:
    deep = seen.get("deep")
    if not isinstance(deep, dict):
        return
    if "error" in deep:
        row.deep_error = str(deep["error"])
        return
    if isinstance(deep.get("snapshots"), int):
        row.snapshots = deep["snapshots"]
    if deep.get("latest"):
        row.latest_snapshot = str(deep["latest"])


def evaluate(doc: dict[str, Any]) -> tuple[list[Row], int]:
    """Rows for every (host, target) and the exit code of the whole env."""
    rows: list[Row] = []
    codes: set[int] = set()
    results = doc.get("results") or {}
    hosts = list(doc.get("hosts") or []) or sorted(results)
    for host in hosts:
        res = results.get(host)
        if not isinstance(res, dict):
            rows.append(Row(host, "-", "no_answer", "the box did not answer"))
            codes.add(NO_ANSWER)
            continue
        probe = res.get("probe") or {}
        if "error" in probe:
            rows.append(Row(host, "-", "no_answer", str(probe["error"])))
            codes.add(NO_ANSWER)
            continue
        if not res.get("enabled", False):
            rows.append(Row(host, "-", "disabled", "backup_enabled is false for this host"))
            continue
        now = int(probe.get("now") or 0)
        config = res.get("targets") or {}
        seen_all = probe.get("targets") or {}
        expected = list(res.get("expected") or [])
        for name in expected:
            cfg = config.get(name, {})
            seen = seen_all.get(name, {})
            row = Row(host, name, "ok", method=str(cfg.get("method", "")))
            row.next_run = str(seen.get("next") or "")
            last = (seen.get("last_ok") or {}).get("at")
            row.last_ok = int(last) if isinstance(last, (int, float)) else None
            _deep(row, seen)
            if row.last_ok is None and row.latest_snapshot:
                # --deep on a box with no status file: the newest snapshot
                # in the repo is the last good run.
                row.last_ok = _epoch(row.latest_snapshot)
            why = _missing_reason(seen)
            if why:
                row.state, row.detail = "missing", why
                codes.add(MISSING)
            elif row.last_ok is None and probe.get("manifest") is None:
                # The backup role of this box predates 2.24.0: it writes no
                # status file, so the age of the last good run is unknown.
                row.state = "no_record"
                row.detail = "the box records good runs since 2.24.0; run bay up"
            else:
                max_age = int(cfg.get("max_age_hours") or 30) * 3600
                # With no status file the clock starts at the later of the
                # timer install and the first manifest (2.24.0 writes both;
                # a box upgraded from an older release has old timers and no
                # status file yet, and must not read as stale at once).
                since = row.last_ok if row.last_ok is not None else max(
                    int(seen.get("installed") or 0), int(probe.get("manifest_mtime") or 0)
                )
                if now and since and now - since >= max_age:
                    row.state = "stale"
                    row.detail = (
                        f"no good run for {(now - since) // 3600} hours (limit {max_age // 3600})"
                        if row.last_ok is not None
                        else f"no good run since the timer came {(now - since) // 3600} hours ago"
                    )
                    codes.add(STALE)
                elif row.last_ok is None:
                    row.state, row.detail = "new", "installed, no good run yet"
            rows.append(row)
        for name in sorted(set(seen_all) - set(expected)):
            row = Row(host, name, "extra", "on the box, not in the config")
            _deep(row, seen_all[name])
            rows.append(row)
    code = next((c for c in _PRIORITY if c in codes), OK)
    return rows, code


def _epoch(iso: str) -> int | None:
    """A restic snapshot time (RFC 3339, nanoseconds) as epoch seconds."""
    try:
        head = iso[:19]
        return int(_dt.datetime.strptime(head, "%Y-%m-%dT%H:%M:%S")
                   .replace(tzinfo=_dt.timezone.utc).timestamp()) - _offset(iso)
    except ValueError:
        return None


def _offset(iso: str) -> int:
    """Seconds east of UTC of the zone suffix (``Z``, ``+02:00``)."""
    tail = iso[19:].split(".", 1)[-1] if "." in iso[19:] else iso[19:]
    tail = tail.lstrip("0123456789")
    if tail in ("", "Z"):
        return 0
    sign = 1 if tail[0] == "+" else -1
    hh, _, mm = tail[1:].partition(":")
    return sign * (int(hh) * 3600 + int(mm or 0) * 60)


def when(epoch: int | None) -> str:
    if epoch is None:
        return "-"
    return _dt.datetime.fromtimestamp(epoch, _dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
