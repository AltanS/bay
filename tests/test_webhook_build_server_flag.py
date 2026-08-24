"""Guards the webhook spec's build-server detection in build_specs.yml.

Context (2026-07-15). `ansible-lint` emitted:

    The `bool` filter coerced invalid value
    "{{ (build_server | default('')) == inventory_hostname }}" (str) to False.
    This feature will be removed from ansible-core version 2.23.

which reads like `IS_BUILD_SERVER` was always empty in production. It was not.
`_is_build_server_for_webhook` is a task-level var, and at RUNTIME ansible
resolves sibling `vars:` references before the filter runs, yielding a native
bool — verified against ansible-core 2.20:

    type_debug -> bool, is boolean -> True

Only ansible-lint's STATIC render of the `vars:` block leaves the sibling
unresolved, so `bool` there receives the raw "{{ ... }}" text and coerces it to
False. Runtime was correct; the lint-time render was not.

The fix is to drop `| bool`: `==` and `> 0` already produce native bools, so the
filter was redundant. Removal was verified behavior-preserving over the full
truth matrix (build-server yes/no x global-remote-builds yes/no) — old and new
forms agreed in all four cases.

This matters because the deprecation becomes a hard error in core 2.23, and
because the obvious "fix" (re-adding `| bool` to silence a linter) would
reintroduce it. These vars had ZERO test coverage before this file, so the
guard is structural: assert the sibling refs stay bare.

Do NOT "simplify" by adding `| bool` back.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

_BUILD_SPECS = (
    Path(__file__).resolve().parent.parent
    / "roles"
    / "container_lifecycle"
    / "tasks"
    / "build_specs.yml"
)

_WEBHOOK_TASK_NAME = "Build webhook receiver container spec"


@pytest.fixture(scope="module")
def webhook_task() -> dict:
    tasks = yaml.safe_load(_BUILD_SPECS.read_text())
    for task in tasks:
        task_vars = task.get("vars") or {}
        if "_is_build_server_for_webhook" in task_vars:
            return task
    raise AssertionError(
        "No task in "
        f"{_BUILD_SPECS} defines `_is_build_server_for_webhook`. If the webhook "
        "spec moved, retarget this guard rather than deleting it."
    )


class TestBuildServerFlagStaysBare:
    """`| bool` must not come back to these sibling references."""

    def test_is_build_server_var_is_the_expected_comparison(
        self, webhook_task: dict
    ) -> None:
        expr = webhook_task["vars"]["_is_build_server_for_webhook"]
        assert expr == "{{ (build_server | default('')) == inventory_hostname }}"

    def test_is_build_server_flag_references_var_bare(
        self, webhook_task: dict
    ) -> None:
        flag = webhook_task["vars"]["_webhook_spec"]["env"]["IS_BUILD_SERVER"]
        assert flag == "{{ 'true' if _is_build_server_for_webhook else '' }}"
        assert "| bool" not in flag, (
            "`| bool` re-added to IS_BUILD_SERVER. It is redundant (the var is "
            "already a native bool at runtime) and breaks ansible-lint's static "
            "render — a hard error from ansible-core 2.23. See module docstring."
        )

    def test_service_selection_references_vars_bare(
        self, webhook_task: dict
    ) -> None:
        expr = webhook_task["vars"]["_all_build_services_for_webhook"]
        assert "if (_is_build_server_for_webhook and _has_global_remote_builds)" in expr
        assert "_is_build_server_for_webhook | bool" not in expr
        assert "_has_global_remote_builds | bool" not in expr

    def test_when_condition_uses_the_var_bare(self, webhook_task: dict) -> None:
        # The `when:` already relied on bare truthiness before this change; it is
        # what established that the var resolves to a real bool at runtime.
        when_clauses = " ".join(str(c) for c in webhook_task["when"])
        assert "_is_build_server_for_webhook and _has_global_remote_builds" in (
            when_clauses
        )
        assert "_is_build_server_for_webhook | bool" not in when_clauses
