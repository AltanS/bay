"""The crowdsec role's YAML check must cover every parser Bay renders.

The role validates rendered parsers with `python3 -c yaml.safe_load` before
`cscli hub update`, so a bad file fails the run with its path instead of
crashing the agent reload. The check used to match only `custom-*.yaml`, which
left Bay's own `bay-vpn-allowlist-refusals.yaml` (v0.7.4) unchecked. A broken
render of that file stops CrowdSec from reloading, and with it the SSH ban rule.
"""

from __future__ import annotations

import fnmatch
from pathlib import Path
from typing import Any

import pytest
import yaml

_ROLE = Path(__file__).resolve().parent.parent / "roles" / "crowdsec"

FIND_TASK = "List rendered parser files"
CHECK_TASK = "Validate rendered parser YAML"
BAY_DEPLOY_TASK = "Deploy VPN allowlist refusal whitelist"
CUSTOM_DEPLOY_TASK = "Deploy custom CrowdSec parsers"
HUB_TASK = "Update CrowdSec hub"


@pytest.fixture(scope="module")
def tasks() -> list[dict[str, Any]]:
    loaded = yaml.safe_load((_ROLE / "tasks" / "main.yml").read_text())
    assert isinstance(loaded, list)
    return loaded


def _index(tasks: list[dict[str, Any]], name: str) -> int:
    for i, task in enumerate(tasks):
        if task.get("name") == name:
            return i
    raise AssertionError(f"task {name!r} not found in the crowdsec role")


def _patterns(tasks: list[dict[str, Any]]) -> list[str]:
    patterns = tasks[_index(tasks, FIND_TASK)]["ansible.builtin.find"]["patterns"]
    return [patterns] if isinstance(patterns, str) else list(patterns)


def _matches(tasks: list[dict[str, Any]], filename: str) -> bool:
    return any(fnmatch.fnmatch(filename, p) for p in _patterns(tasks))


@pytest.mark.parametrize(
    "filename",
    ["custom-bot-verify-spoof.yaml", "bay-vpn-allowlist-refusals.yaml", "bay-anything.yaml"],
)
def test_find_matches_every_file_bay_renders(tasks: list[dict[str, Any]], filename: str) -> None:
    assert _matches(tasks, filename)


@pytest.mark.parametrize(
    "filename",
    [
        "cscli-parser.yaml",
        "crowdsecurity-traefik-logs.yaml",
        "traefik-logs.yaml",
        "s02-enrich.yaml",
        "bay.yaml",
        "custom-parser.yaml.bak",
        "notbay-x.yaml",
    ],
)
def test_find_leaves_hub_managed_files_out(tasks: list[dict[str, Any]], filename: str) -> None:
    assert not _matches(tasks, filename)


def test_find_still_recurses_under_the_parsers_directory(tasks: list[dict[str, Any]]) -> None:
    find = tasks[_index(tasks, FIND_TASK)]["ansible.builtin.find"]
    assert find["paths"] == "/etc/crowdsec/parsers"
    assert find["recurse"] is True


def test_a_bad_file_still_fails_the_run(tasks: list[dict[str, Any]]) -> None:
    check = tasks[_index(tasks, CHECK_TASK)]
    assert "yaml.safe_load" in check["ansible.builtin.command"]["cmd"]
    assert "item.path" in check["ansible.builtin.command"]["cmd"]
    assert check["failed_when"] == "_parser_yaml_check.rc != 0"
    assert "_rendered_parsers.files" in check["loop"]


def test_check_runs_after_both_parsers_are_deployed_and_before_the_hub(
    tasks: list[dict[str, Any]],
) -> None:
    """A find placed before the Bay deploy misses the file on its first render."""
    find = _index(tasks, FIND_TASK)
    assert _index(tasks, BAY_DEPLOY_TASK) < find
    assert _index(tasks, CUSTOM_DEPLOY_TASK) < find
    assert _index(tasks, CHECK_TASK) == find + 1
    assert _index(tasks, CHECK_TASK) < _index(tasks, HUB_TASK)
