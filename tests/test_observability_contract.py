"""Enforce the build pipeline observability contract.

The contract (docs/build-pipeline-observability-contract.md) is the
authoritative reference for every terminal state in rebuild.sh.j2. This
test enforces that the contract's exit-path map stays in sync with the
template — any new exit path MUST be documented before the test passes.

This is the forcing function for Phase 1 of the observability plan.
It does not classify severity (that's a human review
task) — it only catches drift between the doc and the template.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_TEMPLATE = _REPO_ROOT / "roles" / "git_deploy" / "templates" / "rebuild.sh.j2"
_CONTRACT = _REPO_ROOT / "docs" / "build-pipeline-observability-contract.md"

_FILTER_DIR = str(_REPO_ROOT / "filter_plugins")
if _FILTER_DIR not in sys.path:
    sys.path.insert(0, _FILTER_DIR)


def _extract_template_exits() -> set[int]:
    """Return the set of 1-indexed line numbers containing `exit 0` or `exit 1`.

    Matches bash-level exits only — ignores `exit` appearing inside Jinja
    comments ({# ... #}) or in quoted strings. For the current template
    shape, a simple line-prefix regex is sufficient.
    """
    exits: set[int] = set()
    for i, line in enumerate(_TEMPLATE.read_text().splitlines(), start=1):
        if re.match(r"^\s*exit [01]\s*(#.*)?$", line):
            exits.add(i)
    return exits


def _extract_contract_exits() -> set[int]:
    """Return the set of line numbers listed in the contract's exit-path map.

    The map is a markdown table whose rows look like:
        | 6 | 358   | 0    | CB-OPEN — push blocked | ...
    We want column 2 (the line number). The "ERR trap" row has `ERR trap`
    in column 2 — we skip it since it's a trap, not a literal `exit` line.
    """
    lines: set[int] = set()
    for row in _CONTRACT.read_text().splitlines():
        match = re.match(
            r"^\|\s*\d+\s*\|\s*(\d+)\s*\|\s*[01]\s*\|",
            row,
        )
        if match:
            lines.add(int(match.group(1)))
    return lines


def _preceding_marker_present(line_num: int, window: int = 30) -> bool:
    """Check that a categorized stdout marker or notification call appears
    within the preceding N non-blank lines before an exit.

    The contract requires every terminal state to emit either a
    `[rebuild] ...` stdout marker OR a call to `notify_build` /
    `_record_failure` (which themselves log and notify). This is a cheap
    structural check — not a semantic one.

    `notify_build` is rebuild.sh's wrapper around the shared `bay_notify`
    fan-out (roles/alert_channel); it replaced the old `send_telegram`
    when alerts stopped being Telegram-only.
    """
    content = _TEMPLATE.read_text().splitlines()
    start = max(0, line_num - 1 - window)
    block = "\n".join(content[start : line_num - 1])
    return (
        "[rebuild]" in block
        or "notify_build" in block
        or "bay_notify" in block
        or "_record_failure" in block
    )


def test_exit_paths_match_contract():
    """Every `exit 0`/`exit 1` in rebuild.sh.j2 must be documented in the contract."""
    template_exits = _extract_template_exits()
    contract_exits = _extract_contract_exits()

    missing_from_doc = template_exits - contract_exits
    stale_in_doc = contract_exits - template_exits

    assert not missing_from_doc, (
        f"Undocumented exit path(s) in rebuild.sh.j2 at line(s) "
        f"{sorted(missing_from_doc)}. Add a row to the exit-path map in "
        f"{_CONTRACT.relative_to(_REPO_ROOT)} and classify the severity."
    )
    assert not stale_in_doc, (
        f"Contract references line(s) {sorted(stale_in_doc)} that no longer "
        f"contain an `exit`. Update the exit-path map in "
        f"{_CONTRACT.relative_to(_REPO_ROOT)}."
    )


def test_every_exit_has_preceding_marker():
    """Every exit path must emit a stdout marker or notification before exiting."""
    offenders: list[int] = []
    for line in sorted(_extract_template_exits()):
        if not _preceding_marker_present(line):
            offenders.append(line)
    assert not offenders, (
        f"Exit path(s) at line(s) {offenders} lack a preceding `[rebuild] ...` "
        f"marker, `notify_build` call, or `_record_failure` call within the "
        f"30-line window. Silent exits violate the observability contract."
    )


def test_contract_doc_exists():
    """Sanity check that the contract doc is present."""
    assert _CONTRACT.is_file(), (
        f"Contract doc missing at {_CONTRACT.relative_to(_REPO_ROOT)}. "
        f"The observability contract is the authoritative reference for "
        f"rebuild.sh terminal states — it must exist."
    )


# ── Production render + bash syntax check ────────────────────────────────
#
# Added after the v0.76.0 incident where a {% raw %}{% endraw %} block
# followed by a {% newline %} produced valid bash when rendered with
# default Jinja2 (trim_blocks=False) but invalid bash when rendered with
# Ansible's default (trim_blocks=True). Tests passed; production broke.
# This test renders the template the way Ansible does and shells out to
# `bash -n` to parse-check the result.


def _ansible_env():
    from bay_filters import (
        bay_build_dedup_map,
        bay_image_consumers,
        bay_image_region_map,
        bay_prefix_volumes,
        bay_repo_slug,
        bay_traefik_labels,
        bay_watchtower_labels,
    )
    from helpers import make_ansible_env

    env = make_ansible_env(_TEMPLATE.parent)
    env.filters["regex_replace"] = lambda v, p, r: re.sub(p, r, v)
    env.filters["to_json"] = json.dumps
    env.filters["bay_build_dedup_map"] = bay_build_dedup_map
    env.filters["bay_image_consumers"] = bay_image_consumers
    env.filters["bay_image_region_map"] = bay_image_region_map
    env.filters["bay_prefix_volumes"] = bay_prefix_volumes
    env.filters["bay_repo_slug"] = bay_repo_slug
    env.filters["bay_traefik_labels"] = bay_traefik_labels
    env.filters["bay_watchtower_labels"] = bay_watchtower_labels
    return env


def _minimal_render_context():
    """A minimal but realistic rendering context covering all the template branches.

    Includes a local-strategy service with a build block AND a pull-only
    service sharing a remote-built image — that combination exercised every
    per-service loop in the template.
    """
    return {
        "ansible_managed": "Ansible managed - syntax check",
        "services": {
            "svc-local": {
                "build": {
                    "repo": "git@github.com:x/y.git",
                    "branch": "main",
                    "strategy": "local",
                },
                "image": "registry.example.com/x/svc-local:latest",
                "access": "public",
                "domains": ["svc-local.example.com"],
                "ports": {"internal": 3000},
            },
            "svc-remote": {
                "build": {
                    "repo": "git@github.com:x/z.git",
                    "branch": "main",
                    "token": "ghp_fake",
                    "strategy": "remote",
                },
                "image": "registry.example.com/x/shared:latest",
                "access": "public",
                "domains": ["svc-remote.example.com"],
                "ports": {"internal": 3000},
            },
            "svc-pullonly": {
                "image": "registry.example.com/x/shared:latest",
                "access": "public",
                "domains": ["svc-pullonly.example.com"],
                "ports": {"internal": 3000},
            },
        },
        "git_deploy_rebuild_services": ["svc-local", "svc-remote", "svc-pullonly"],
        "git_deploy_services": ["svc-local"],
        "git_deploy_build_strategy": "local",
        "git_deploy_build_dir": "/opt/test/builds",
        "git_deploy_image_prefix": "bay-test",
        "git_deploy_remote_build_dir": "/opt/test/push-builds",
        "git_deploy_cb_max_failures": 5,
        "git_deploy_health_check_timeout": 90,
        "git_deploy_build_timeout": 1200,
        "git_deploy_build_mem_limit": "2g",
        "git_deploy_peer_webhook_urls": {"eu": "https://deploy.eu.example.com"},
        "stack_dir": "/opt/test",
        "stack_name": "test",
        "docker_monitor_telegram_bot_token": "fake",
        "docker_monitor_telegram_chat_id": "fake",
        "docker_monitor_alert_header": "[TEST] ",
        "inventory_hostname": "test-host",
        "traefik_docker_network": "services",
        "watchtower_enabled": False,
        "webhook": {"secret": "fake-secret"},
    }


def test_rebuild_sh_parses_under_ansible_render(tmp_path):
    """Render rebuild.sh.j2 with Ansible's Jinja2 config, run `bash -n`, assert 0.

    This catches regressions where a template change renders validly with
    Jinja2's defaults but not with Ansible's trim_blocks=True behavior.
    """
    env = _ansible_env()
    rendered = env.get_template(_TEMPLATE.name).render(**_minimal_render_context())
    out = tmp_path / "rebuild-rendered.sh"
    out.write_text(rendered)
    result = subprocess.run(
        ["bash", "-n", str(out)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"rebuild.sh produced by Ansible-compatible render fails `bash -n`.\n"
        f"stderr:\n{result.stderr}\n"
        f"Rendered output saved at {out} for inspection."
    )


def _all_local_render_context():
    """A host where NO service is pull-eligible — every service builds locally.

    The common single-host case, and the one the mixed fixture above cannot
    reach: with a pull-eligible service present, the pull-dispatch loop always
    opens an `if`, so an unguarded closing `fi` still balances. Strip them and
    the same `fi` closes the PULL_SIGNAL guard ~80 lines earlier instead.
    """
    ctx = _minimal_render_context()
    ctx["services"] = {
        "svc-local": {
            "build": {
                "repo": "git@github.com:x/y.git",
                "branch": "main",
                "strategy": "local",
            },
            "access": "public",
            "domains": ["svc-local.example.com"],
            "ports": {"internal": 3000},
        },
    }
    ctx["git_deploy_rebuild_services"] = ["svc-local"]
    ctx["git_deploy_services"] = ["svc-local"]
    return ctx


def test_rebuild_sh_parses_when_no_service_is_pull_eligible(tmp_path):
    """Regression (#32): an all-local-build host rendered invalid bash.

    The pull-dispatch loop emitted nothing while its closing `fi` was emitted
    unconditionally, so the rendered script was a syntax error AND called
    `_run_container` ~270 lines before its own definition. Every
    `bay-build@<service>` trigger died with `_run_container: command not
    found` (exit 127) — push-to-deploy dead host-wide, with a clean-looking
    `git push`. It went unnoticed for eleven days in production.
    """
    env = _ansible_env()
    rendered = env.get_template(_TEMPLATE.name).render(**_all_local_render_context())
    out = tmp_path / "rebuild-all-local.sh"
    out.write_text(rendered)
    result = subprocess.run(
        ["bash", "-n", str(out)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"rebuild.sh fails `bash -n` when no service is pull-eligible.\n"
        f"stderr:\n{result.stderr}\n"
        f"Rendered output saved at {out} for inspection."
    )

    # The dispatch is unreachable here (IMAGE_REF is empty for every service, so
    # the PULL_SIGNAL guard can never hold) — so it must not be emitted at all.
    assert "_run_container\n" in rendered, "local-build path must still define/call _run_container"


# ── Severity ladder ───────────────────────────────────────────────────────────
#
# The contract's four historical severity names are transport-flavoured
# (`Telegram-warn` names a sink that is becoming one adapter among several).
# `alerts/registry.yml` classifies alerts from every emitter, not just
# rebuild.sh, so both speak one transport-neutral ladder and the old names are
# retained as aliases. These tests keep the ladder, the alias mapping and the
# alert_id join column from being quietly edited away — the whole point of
# unifying was that two taxonomies over one set of facts will drift.

_LADDER = ("debug", "info", "warn", "critical")

_ALIASES = {
    "log-only-debug": "debug",
    "Telegram-info": "info",
    "Telegram-warn": "warn",
    "Telegram-critical": "critical",
}


def test_severity_ladder_is_documented_in_order():
    """The ordering is authoritative for every `min_level` comparison."""
    text = _CONTRACT.read_text()
    assert "### Severity ladder" in text, "Severity ladder section is missing"
    expected = "  <  ".join(_LADDER)
    assert expected in text, (
        f"The ladder must be documented in ascending order as `{expected}`. "
        "Recipient min_level comparisons depend on this ordering."
    )


def test_every_historical_severity_maps_to_a_ladder_level():
    """No contract row may lose its meaning to the rename."""
    text = _CONTRACT.read_text()
    for alias, level in _ALIASES.items():
        row = re.search(
            rf"^\|\s*`{re.escape(level)}`\s*\|\s*`{re.escape(alias)}`\s*\|",
            text,
            flags=re.M,
        )
        assert row is not None, (
            f"Alias `{alias}` has no mapping row to `{level}` in the ladder "
            "table. Every severity used by the exit-path map must map to a "
            "ladder level, or the map's rows become unclassifiable."
        )


def test_ladder_has_no_unused_error_tier():
    """Four tiers, not five — a tier no alert uses is drift bait.

    An `error` tier between `warn` and `critical` was considered and rejected.
    Adding a tier later is backwards-compatible; this guard exists so it is a
    decision rather than an accident.
    """
    assert "error" not in _LADDER
    text = _CONTRACT.read_text()
    assert re.search(r"^\|\s*`error`\s*\|", text, flags=re.M) is None, (
        "An `error` row appeared in the ladder table. If the tier is genuinely "
        "needed, add it to _LADDER here and to docs/alerting.md deliberately."
    )


def test_exit_path_map_rows_all_carry_an_alert_id_cell():
    """The alert_id column is the join key between this map and the registry.

    S2 populates the values; this only enforces that the column exists on every
    row, so one test can own both contracts once the registry lands.
    """
    def _cells(row: str) -> list[str]:
        return [c.strip() for c in row.strip().strip("|").split("|")]

    lines = _CONTRACT.read_text().splitlines()

    header = next((ln for ln in lines if ln.startswith("| # | Line")), None)
    assert header is not None, "Exit-path map header row not found"
    columns = _cells(header)
    assert columns[-1] == "alert_id", (
        f"Last column of the exit-path map is {columns[-1]!r}, not 'alert_id'. "
        "The alert_id column is the join key to alerts/registry.yml."
    )

    # Derive the width from the header rather than hardcoding it: a hardcoded
    # count silently tolerates a dropped cell if it is off by one, which is how
    # this guard first shipped green while the column was being stripped.
    width = len(columns)

    rows = [
        ln
        for ln in lines
        if re.match(r"^\|\s*\d+\s*\|\s*(?:\d+|ERR trap)\s*\|", ln)
    ]
    assert len(rows) == 16, f"Expected 16 exit-path rows, found {len(rows)}"

    malformed: list[str] = []
    for row in rows:
        cells = _cells(row)
        if len(cells) != width or not cells[-1]:
            malformed.append(f"{len(cells)}/{width} cells: {row[:60]}")
    assert not malformed, (
        "Exit-path rows are missing the trailing alert_id cell:\n"
        + "\n".join(malformed)
    )
