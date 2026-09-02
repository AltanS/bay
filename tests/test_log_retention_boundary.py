"""The boundary ID capture is one batched inspect per side (M111 P7).

It used to be one `docker inspect` per container per side. The parsing that
replaced it is a Jinja expression in a `set_fact`, which is exactly the kind
of code that rots silently, so the expressions are pulled out of the task
files and evaluated here rather than restated.

The two cases that matter:
  * `docker inspect` prefixes names with `/` — the map keys must not, or the
    sentinel task looks up `api` and finds nothing.
  * a container that does not exist yet produces an error on stderr and NO
    stdout line, while rc is non-zero for the whole call. Every other
    container's line must still be parsed.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
import yaml
from jinja2 import Environment

from ansible.plugins.filter.core import FilterModule as _CoreFilters
from ansible.plugins.filter.mathstuff import FilterModule as _MathFilters
from ansible.plugins.test.core import TestModule as _CoreTests

_REPO_ROOT = Path(__file__).resolve().parent.parent
_TASKS = _REPO_ROOT / "roles" / "deploy_stack" / "tasks"
_PRE = _TASKS / "log_retention_boundary_pre.yml"
_POST = _TASKS / "log_retention_boundary_post.yml"


def _env() -> Environment:
    env = Environment()
    env.filters.update(_CoreFilters().filters())
    env.filters.update(_MathFilters().filters())
    env.tests.update(_CoreTests().tests())
    return env


def _task(path: Path, name: str) -> dict:
    for task in yaml.safe_load(path.read_text()):
        if task.get("name") == name:
            return task
    raise AssertionError(f"{path.name}: no task named {name!r}")


def _eval_map(path: Path, task_name: str, fact: str, **ctx) -> dict:
    expr = _task(path, task_name)["ansible.builtin.set_fact"][fact]
    rendered = _env().from_string(expr).render(**ctx)
    return ast.literal_eval(rendered.strip())


class _Result(dict):
    """Stand-in for a registered command result."""

    def __getattr__(self, item):
        try:
            return self[item]
        except KeyError as exc:  # pragma: no cover - fixture bug
            raise AttributeError(item) from exc


_SIDES = [
    (_PRE, "Build pre-recreate ID map", "_log_retention_pre_ids",
     "_log_retention_pre_ids_raw"),
    (_POST, "Build post-recreate ID map", "_log_retention_post_ids",
     "_log_retention_post_ids_raw"),
]
_SIDE_IDS = ["pre", "post"]


@pytest.mark.parametrize("path,task,fact,reg", _SIDES, ids=_SIDE_IDS)
def test_leading_slash_is_stripped(path, task, fact, reg):
    got = _eval_map(
        path, task, fact,
        **{reg: _Result(stdout_lines=["/api abc123", "/postgres def456"])},
    )
    assert got == {"api": "abc123", "postgres": "def456"}


@pytest.mark.parametrize("path,task,fact,reg", _SIDES, ids=_SIDE_IDS)
def test_a_missing_container_drops_out_without_losing_the_others(
    path, task, fact, reg
):
    """rc is non-zero for the whole call; the found containers still parse."""
    got = _eval_map(
        path, task, fact,
        **{reg: _Result(
            stdout_lines=["/api abc123", "/worker fff999"],
            stderr="Error: No such object: gone",
            rc=1,
        )},
    )
    assert got == {"api": "abc123", "worker": "fff999"}
    assert "gone" not in got


@pytest.mark.parametrize("path,task,fact,reg", _SIDES, ids=_SIDE_IDS)
def test_noise_lines_are_dropped_not_guessed_at(path, task, fact, reg):
    got = _eval_map(
        path, task, fact,
        **{reg: _Result(stdout_lines=["", "/api abc123", "some warning here"])},
    )
    assert got == {"api": "abc123"}


@pytest.mark.parametrize("path,task,fact,reg", _SIDES, ids=_SIDE_IDS)
def test_no_output_at_all_is_an_empty_map(path, task, fact, reg):
    assert _eval_map(path, task, fact, **{reg: _Result(stdout_lines=[])}) == {}


@pytest.mark.parametrize("path,task", [
    (_PRE, "Capture pre-recreate container IDs"),
    (_POST, "Capture post-recreate container IDs"),
], ids=_SIDE_IDS)
def test_each_side_is_a_single_inspect_that_ignores_rc(path, task):
    t = _task(path, task)
    cmd = t["ansible.builtin.command"]["cmd"]
    assert cmd.count("docker inspect") == 1
    assert "{{.Name}} {{.Id}}" in cmd
    assert "loop" not in t, "the per-container fan is back"
    assert t["failed_when"] is False
    assert t["changed_when"] is False
    # Names are shlex-quoted: `command` splits argv itself, no shell involved.
    assert "map('quote')" in cmd


def test_the_sentinel_task_reads_the_batched_map():
    t = _task(_POST, "Write container-recreated boundary sentinel to live.log")
    assert t["loop"] == '{{ (_log_retention_post_ids | default({})) | dict2items }}'
    assert t["environment"]["SVC"] == "{{ item.key }}"
    body = t["ansible.builtin.shell"]["cmd"]
    assert "${SVC}" in body
    assert "{{ item.key }}" not in body, "service key is back in the shell string"


def test_the_sentinel_conditions_survived_the_reshape():
    """Same three guards as before, now keyed on the batched map.

    Evaluated structurally rather than by rendering: ansible-core 2.19+
    returns a lazy marker from `default()` that a bare Jinja Environment
    compares differently from the real conditional evaluator, so a render
    here would assert the harness, not the playbook.
    """
    conds = [
        " ".join(c.split())
        for c in _task(
            _POST, "Write container-recreated boundary sentinel to live.log"
        )["when"]
    ]
    pre = "(_log_retention_pre_ids | default({}))[item.key] | default('')"
    assert "item.value != ''" in conds
    assert f"({pre}) != ''" in conds
    assert f"item.value != ({pre})" in conds
    assert not any("item.item" in c for c in conds), "stale .results shape"
