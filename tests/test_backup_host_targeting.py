"""Unit tests for backup host-targeting.

The ``backup run|list|check <target>`` commands must act only on the host(s)
where ``backup-<target>.sh`` actually exists, instead of failing on every host
in the inventory group that lacks it. These tests exercise the per-host parse
helper (presence / absence / multi-host) and ``_hosts_with_target`` with a
monkeypatched ``_run_on_host`` so no real ansible is ever invoked.

The discovery probe is crafted to exit 0 on EVERY host (an ``if test -f ...;
then echo MARKER; fi``) so absent hosts are not FAILED — otherwise the ad-hoc
exits non-zero and ``runner.run`` dumps the per-host failures to the console
regardless of ``check=False``. Presence is therefore signalled by the marker
echoed into a host's output block, not by the rc/status.
"""

from __future__ import annotations

import json

import subprocess
from pathlib import Path

from bay_cli.commands import backup
from bay_cli.errors import BayError


MARKER = backup._PRESENT_MARKER


# ── _parse_present_hosts: marker-based ansible ad-hoc parsing ─────────────


def test_parse_single_present_host() -> None:
    """A SUCCESS host whose body contains the marker is present."""
    stdout = f"eu-control | SUCCESS | rc=0 >>\n{MARKER}\n"
    assert backup._parse_present_hosts(stdout) == ["eu-control"]


def test_parse_absent_hosts_excluded() -> None:
    """SUCCESS hosts WITHOUT the marker (empty body) are treated as absent."""
    stdout = (
        f"eu-control | SUCCESS | rc=0 >>\n{MARKER}\n"
        "\n"
        "na-edge | SUCCESS | rc=0 >>\n"
        "\n"
        "infra | SUCCESS | rc=0 >>\n"
    )
    assert backup._parse_present_hosts(stdout) == ["eu-control"]


def test_parse_multi_host_present() -> None:
    """An accessory present on several hosts returns all of them."""
    stdout = (
        f"eu-control | SUCCESS | rc=0 >>\n{MARKER}\n"
        "\n"
        f"na-edge | SUCCESS | rc=0 >>\n{MARKER}\n"
        "\n"
        "infra | SUCCESS | rc=0 >>\n"
    )
    assert backup._parse_present_hosts(stdout) == ["eu-control", "na-edge"]


def test_parse_no_host_present() -> None:
    """A target on no host yields an empty list (all SUCCESS, no marker)."""
    stdout = (
        "na-edge | SUCCESS | rc=0 >>\n"
        "\n"
        "infra | SUCCESS | rc=0 >>\n"
    )
    assert backup._parse_present_hosts(stdout) == []


def test_parse_unreachable_excluded() -> None:
    """UNREACHABLE hosts emit no marker and are not counted as present."""
    stdout = (
        f"eu-control | SUCCESS | rc=0 >>\n{MARKER}\n"
        "\n"
        "na-edge | UNREACHABLE! => {\n"
        '    "changed": false,\n'
        '    "msg": "Failed to connect to the host via ssh"\n'
        "}\n"
    )
    assert backup._parse_present_hosts(stdout) == ["eu-control"]


def test_parse_robust_to_ansi() -> None:
    """ANSI color codes around the header/marker are stripped before parsing."""
    stdout = (
        f"\x1b[0;32meu-control | SUCCESS | rc=0 >>\x1b[0m\n{MARKER}\n"
        "\x1b[0;32mna-edge | SUCCESS | rc=0 >>\x1b[0m\n"
    )
    assert backup._parse_present_hosts(stdout) == ["eu-control"]


def test_parse_marker_does_not_leak_across_blocks() -> None:
    """A marker in one host's block does not mark a later marker-less host."""
    stdout = (
        f"eu-control | SUCCESS | rc=0 >>\n{MARKER}\n"
        "na-edge | SUCCESS | rc=0 >>\n"
    )
    assert backup._parse_present_hosts(stdout) == ["eu-control"]


# ── _hosts_with_target: integration with a faked _run_on_host ─────────────


def _fake_completed(stdout: str, rc: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["ansible"], returncode=rc, stdout=stdout, stderr=""
    )


def test_hosts_with_target_single(monkeypatch) -> None:
    """Only the host that emits the marker is returned; probe is noiseless."""
    captured: dict[str, object] = {}

    def fake_run_on_host(env, cmd, *, bay_dir, check=True, **kwargs):
        captured["env"] = env
        captured["cmd"] = cmd
        captured["check"] = check
        captured["kwargs"] = kwargs
        return _fake_completed(
            f"eu-control | SUCCESS | rc=0 >>\n{MARKER}\n"
            "na-edge | SUCCESS | rc=0 >>\n"
        )

    monkeypatch.setattr(backup, "_run_on_host", fake_run_on_host)

    hosts = backup._hosts_with_target(
        "production", "headscale", bay_dir=Path("/x/.bay"), stack="bay"
    )

    assert hosts == ["eu-control"]
    assert captured["env"] == "production"
    # check=False so the raise is suppressed (the `if`-guard already keeps the
    # ad-hoc at rc=0, but the probe must never raise on discovery).
    assert captured["check"] is False
    # The presence probe must scan the WHOLE group — never --limit-ed, or it
    # could never discover hosts it didn't already know about.
    assert captured["kwargs"].get("limit") is None
    # The probe is the noiseless marker form against the expected script path.
    assert "backup-headscale.sh" in captured["cmd"]
    assert "/opt/bay/backup/" in captured["cmd"]
    assert f"echo {MARKER}" in captured["cmd"]
    assert captured["cmd"].startswith("if test -f")


def test_hosts_with_target_multi(monkeypatch) -> None:
    """A multi-host accessory (postgres on eu+na) returns every host that has it."""

    def fake_run_on_host(env, cmd, *, bay_dir, check=True, **kwargs):
        return _fake_completed(
            f"eu-control | SUCCESS | rc=0 >>\n{MARKER}\n"
            f"na-edge | SUCCESS | rc=0 >>\n{MARKER}\n"
            "infra | SUCCESS | rc=0 >>\n"
        )

    monkeypatch.setattr(backup, "_run_on_host", fake_run_on_host)

    hosts = backup._hosts_with_target(
        "production", "postgres", bay_dir=Path("/x/.bay"), stack="myapp"
    )

    assert hosts == ["eu-control", "na-edge"]


def test_hosts_with_target_none(monkeypatch) -> None:
    """A target configured on no host returns an empty list (clean, no raise)."""

    def fake_run_on_host(env, cmd, *, bay_dir, check=True, **kwargs):
        return _fake_completed(
            "na-edge | SUCCESS | rc=0 >>\n"
            "infra | SUCCESS | rc=0 >>\n"
        )

    monkeypatch.setattr(backup, "_run_on_host", fake_run_on_host)

    hosts = backup._hosts_with_target(
        "production", "nonexistent", bay_dir=Path("/x/.bay"), stack="bay"
    )

    assert hosts == []


def test_hosts_with_target_custom_stack_path(monkeypatch) -> None:
    """The probe uses the resolved stack name in the script path."""
    captured: dict[str, object] = {}

    def fake_run_on_host(env, cmd, *, bay_dir, check=True, **kwargs):
        captured["cmd"] = cmd
        return _fake_completed(f"eu-control | SUCCESS | rc=0 >>\n{MARKER}\n")

    monkeypatch.setattr(backup, "_run_on_host", fake_run_on_host)

    backup._hosts_with_target(
        "production", "postgres", bay_dir=Path("/x/.bay"), stack="myapp"
    )

    assert "/opt/myapp/backup/backup-postgres.sh" in captured["cmd"]


# ── _extract_host_json_arrays: multi-host snapshot aggregation ────────────


def test_extract_json_single_host() -> None:
    """A single host block yields its parsed JSON array."""
    stdout = 'eu | SUCCESS | rc=0 >>\n[{"short_id": "aaa", "time": "t1"}]\n'
    blocks = backup._extract_host_json_arrays(stdout)
    assert [h for h, _ in blocks] == ["eu"]
    assert blocks[0][1][0]["short_id"] == "aaa"


def test_extract_json_multi_host_aggregated() -> None:
    """Each host's own repo block is parsed independently — no 'Extra data' crash."""
    stdout = (
        'eu | SUCCESS | rc=0 >>\n[{"short_id": "aaa"}]\n'
        'na | SUCCESS | rc=0 >>\n[{"short_id": "bbb"}, {"short_id": "ccc"}]\n'
    )
    blocks = backup._extract_host_json_arrays(stdout)
    assert [h for h, _ in blocks] == ["eu", "na"]
    flat = [s["short_id"] for _h, arr in blocks for s in arr]
    assert flat == ["aaa", "bbb", "ccc"]


def test_extract_json_skips_empty_and_malformed_blocks() -> None:
    """Empty-repo and malformed-JSON host blocks are skipped, not raised."""
    stdout = (
        'eu | SUCCESS | rc=0 >>\n[{"short_id": "aaa"}]\n'
        "na | SUCCESS | rc=0 >>\n\n"                # empty body — no JSON array
        "infra | SUCCESS | rc=0 >>\n[broken json\n"  # malformed — skipped silently
    )
    blocks = backup._extract_host_json_arrays(stdout)
    assert [h for h, _ in blocks] == ["eu"]


def test_extract_json_robust_to_ansi() -> None:
    """ANSI codes around the host header are stripped before block detection."""
    stdout = '\x1b[0;33meu | SUCCESS | rc=0 >>\x1b[0m\n[{"short_id": "aaa"}]\n'
    blocks = backup._extract_host_json_arrays(stdout)
    assert [h for h, _ in blocks] == ["eu"]


# ── restore: one host, and the fleet's group_vars (2.23.2) ─────────────────


def test_restore_host_picks_the_only_host() -> None:
    assert backup._restore_host("postgres", None, ["eu-control"]) == "eu-control"


def test_restore_host_needs_a_choice_when_several_hosts_have_the_target() -> None:
    import pytest

    with pytest.raises(BayError, match="More than one host backs up 'postgres'.*--host"):
        backup._restore_host("postgres", None, ["eu-control", "na-edge"])
    assert backup._restore_host("postgres", "na-edge", ["eu-control", "na-edge"]) == "na-edge"


def test_restore_host_refuses_a_host_without_the_target_and_a_missing_target() -> None:
    import pytest

    with pytest.raises(BayError, match="Host infra has no 'postgres' backup"):
        backup._restore_host("postgres", "infra", ["eu-control"])
    with pytest.raises(BayError, match="No host has a 'postgres' backup"):
        backup._restore_host("postgres", None, [])


def test_restore_runs_on_the_target_host_with_the_fleet(monkeypatch, tmp_path) -> None:
    """Snapshots are listed and restore.yml runs only on the host with the target,
    and the playbook gets the fleet (its group_vars), so it can start at all."""
    from typer.testing import CliRunner

    from bay_cli.context import Context

    fleet = tmp_path / "fleet"
    (fleet / "group_vars" / "all").mkdir(parents=True)
    (fleet / "group_vars" / "all" / "main.yml").write_text("---\nstack_name: shop\n")
    cx = Context(fleet_root=fleet, framework_root=Path("/x/bay"), source="flag")

    listed: dict[str, object] = {}
    played: dict[str, object] = {}

    monkeypatch.setattr(backup, "_hosts_with_target", lambda env, target, **kw: ["na-edge"])

    def fake_run_on_host(env, cmd, *, bay_dir, check=True, **kwargs):
        listed["limit"] = kwargs.get("limit")
        return _fake_completed(
            'na-edge | CHANGED | rc=0 >>\n[{"short_id": "ab12cd34", "time": "2026-11-17T03:00:00Z"}]\n'
        )

    def fake_run_playbook(playbook, env, **kwargs):
        played.update(kwargs, playbook=playbook, env=env)

    monkeypatch.setattr(backup, "_run_on_host", fake_run_on_host)
    monkeypatch.setattr(backup.ansible, "run_playbook", fake_run_playbook)

    out = CliRunner().invoke(
        backup.app, ["restore", "production", "postgres", "--snapshot", "ab12cd34"],
        input="y\n", obj=cx,
    )
    assert out.exit_code == 0, out.output
    # --snapshot given: no listing (restore.yml checks the id on the box).
    assert listed == {}
    assert played["playbook"] == "restore"
    assert played["fleet_root"] == fleet
    extra = played["extra_args"]
    assert extra[extra.index("-l") + 1] == "na-edge"
    assert json.loads(extra[1])["snapshot"] == "ab12cd34"

    # Without --snapshot the snapshots are listed on that host only.
    played.clear()
    out = CliRunner().invoke(backup.app, ["restore", "production", "postgres"],
                             input="1\ny\n", obj=cx)
    assert out.exit_code == 0, out.output
    assert listed["limit"] == "na-edge"
    assert json.loads(played["extra_args"][1])["snapshot"] == "ab12cd34"


def test_restore_stops_before_anything_runs_when_no_host_has_the_target(monkeypatch) -> None:
    from typer.testing import CliRunner

    from bay_cli.context import Context

    cx = Context(fleet_root=Path("/nonexistent"), framework_root=Path("/x/bay"), source="flag")
    monkeypatch.setattr(backup, "_hosts_with_target", lambda env, target, **kw: [])

    def boom(*a, **k):
        raise AssertionError("nothing may run")

    monkeypatch.setattr(backup, "_run_on_host", boom)
    monkeypatch.setattr(backup.ansible, "run_playbook", boom)
    out = CliRunner().invoke(backup.app, ["restore", "production", "postgres"], obj=cx)
    assert out.exit_code != 0
    assert isinstance(out.exception, BayError)
    assert "No host has a 'postgres' backup" in str(out.exception)
