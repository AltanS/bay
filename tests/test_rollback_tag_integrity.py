"""Tests for M75-S9: reversible :latest retag in _handle_rollback.

Verifies that the _handle_rollback function in rebuild.sh.j2:
- Captures :latest digest before the retag (so it can be restored on double-failure)
- Restores :latest to the captured digest when the rollback container fails to start
- Restores :latest to the captured digest when the rollback health check also fails
- Skips restore when latest_digest is empty (first deploy / :latest not yet present)
- Does NOT restore on the success path (:latest correctly points at :previous after success)
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_TEMPLATE = _REPO_ROOT / "roles" / "git_deploy" / "templates" / "rebuild.sh.j2"


def _rollback_function_body() -> str:
    """Extract the _handle_rollback function body from the template."""
    content = _TEMPLATE.read_text()
    # Extract from _handle_rollback() { ... } — find the block between the
    # function opening and the first closing brace at column 0 after it.
    match = re.search(
        r"_handle_rollback\(\)\s*\{(.+?)^(?=_|\w)",
        content,
        re.DOTALL | re.MULTILINE,
    )
    assert match is not None, "_handle_rollback function not found in template"
    return match.group(1)


def _lines_of_function() -> list[tuple[int, str]]:
    """Return (1-indexed line number, line text) for all lines inside _handle_rollback."""
    content = _TEMPLATE.read_text()
    lines = content.splitlines()
    # Find the start and end of _handle_rollback
    start = None
    end = None
    brace_depth = 0
    for i, line in enumerate(lines):
        if "_handle_rollback()" in line and "{" in line:
            start = i
            brace_depth = 1
            continue
        if start is not None:
            brace_depth += line.count("{") - line.count("}")
            if brace_depth <= 0:
                end = i
                break
    assert start is not None, "_handle_rollback not found"
    assert end is not None, "_handle_rollback closing brace not found"
    return [(i + 1, lines[i]) for i in range(start, end + 1)]


def test_handle_rollback_captures_latest_digest_before_retag():
    """The digest capture must appear BEFORE docker tag (the retag) in _handle_rollback."""
    fn_lines = _lines_of_function()
    
    # Find the line with digest capture
    capture_line_num = None
    retag_line_num = None
    
    for lineno, line in fn_lines:
        # The capture: docker inspect --format '{{.Id}}' "${current_tag}"
        if "docker inspect --format" in line and "current_tag" in line:
            capture_line_num = lineno
        # The retag: docker tag "${prev_tag}" "${current_tag}"
        if re.search(r'docker tag "\$\{prev_tag\}" "\$\{current_tag\}"', line):
            retag_line_num = lineno
    
    assert capture_line_num is not None, (
        "latest_digest capture (docker inspect --format ... current_tag) not found "
        "in _handle_rollback. Expected: "
        "latest_digest=$(docker inspect --format '{{.Id}}' \"${current_tag}\" 2>/dev/null || echo \"\")"
    )
    assert retag_line_num is not None, (
        "docker tag ${prev_tag} ${current_tag} not found in _handle_rollback"
    )
    assert capture_line_num < retag_line_num, (
        f"Digest capture (line {capture_line_num}) must appear BEFORE "
        f"docker tag retag (line {retag_line_num}), but it appears after."
    )


def test_handle_rollback_has_latest_digest_variable_declaration():
    """latest_digest must be declared as a local variable in _handle_rollback."""
    fn_lines = _lines_of_function()
    has_local_decl = any(
        re.search(r"\blocal\b.*\blatest_digest\b", line) for _, line in fn_lines
    )
    assert has_local_decl, (
        "latest_digest is not declared with 'local' inside _handle_rollback. "
        "It should be declared as a local variable to avoid leaking into the caller scope."
    )


def test_handle_rollback_restores_latest_on_run_container_failure():
    """The _run_container-failed branch must restore :latest before exit 1."""
    fn_lines = _lines_of_function()
    
    # Find the _run_container failure branch
    run_container_fail_start = None
    run_container_fail_end = None
    depth = 0
    in_if_block = False
    
    for i, (lineno, line) in enumerate(fn_lines):
        if "if ! _run_container" in line:
            run_container_fail_start = i
            in_if_block = True
            depth = 1
            continue
        if in_if_block:
            if line.strip().startswith("if ") or line.strip().startswith("if!"):
                depth += 1
            if line.strip() == "fi":
                depth -= 1
                if depth == 0:
                    run_container_fail_end = i
                    break
    
    assert run_container_fail_start is not None, (
        "_run_container failure branch (if ! _run_container) not found in _handle_rollback"
    )
    assert run_container_fail_end is not None, (
        "fi closing the _run_container failure branch not found"
    )
    
    branch_lines = fn_lines[run_container_fail_start:run_container_fail_end + 1]
    branch_text = "\n".join(line for _, line in branch_lines)
    
    # Check for restore pattern: docker tag "${latest_digest}" "${current_tag}"
    has_restore = bool(re.search(
        r'docker tag "\$\{latest_digest\}" "\$\{current_tag\}"',
        branch_text
    ))
    assert has_restore, (
        "The _run_container-failed branch in _handle_rollback does not restore :latest. "
        "Expected: docker tag \"${latest_digest}\" \"${current_tag}\" 2>/dev/null || true\n"
        f"Branch content:\n{branch_text}"
    )
    
    # Verify the restore is guarded by [[ -n "${latest_digest}" ]]
    has_guard = bool(re.search(r'\[\[\s*-n\s*"\$\{latest_digest\}"\s*\]\]', branch_text))
    assert has_guard, (
        "The :latest restore in _run_container-failed branch is not guarded by "
        "[[ -n \"${latest_digest}\" ]]. Edge case: first deploy has no :latest to restore."
    )


def test_handle_rollback_restores_latest_on_rollback_health_failure():
    """The rollback health-check-failed branch must restore :latest before exit 1."""
    fn_lines = _lines_of_function()
    
    # Find the _wait_healthy failure branch (second if ! ... block)
    wait_healthy_fail_start = None
    wait_healthy_fail_end = None
    depth = 0
    in_if_block = False
    
    for i, (lineno, line) in enumerate(fn_lines):
        if "if ! _wait_healthy" in line:
            wait_healthy_fail_start = i
            in_if_block = True
            depth = 1
            continue
        if in_if_block:
            stripped = line.strip()
            if stripped.startswith("if "):
                depth += 1
            if stripped == "fi":
                depth -= 1
                if depth == 0:
                    wait_healthy_fail_end = i
                    break
    
    assert wait_healthy_fail_start is not None, (
        "_wait_healthy failure branch (if ! _wait_healthy) not found in _handle_rollback"
    )
    assert wait_healthy_fail_end is not None, (
        "fi closing the _wait_healthy failure branch not found"
    )
    
    branch_lines = fn_lines[wait_healthy_fail_start:wait_healthy_fail_end + 1]
    branch_text = "\n".join(line for _, line in branch_lines)
    
    # Check for restore pattern
    has_restore = bool(re.search(
        r'docker tag "\$\{latest_digest\}" "\$\{current_tag\}"',
        branch_text
    ))
    assert has_restore, (
        "The rollback health-check-failed branch in _handle_rollback does not restore :latest. "
        "Expected: docker tag \"${latest_digest}\" \"${current_tag}\" 2>/dev/null || true\n"
        f"Branch content:\n{branch_text}"
    )
    
    # Verify the restore is guarded by [[ -n "${latest_digest}" ]]
    has_guard = bool(re.search(r'\[\[\s*-n\s*"\$\{latest_digest\}"\s*\]\]', branch_text))
    assert has_guard, (
        "The :latest restore in rollback-health-check-failed branch is not guarded by "
        "[[ -n \"${latest_digest}\" ]]. Edge case: first deploy has no :latest to restore."
    )


def test_handle_rollback_restores_latest_includes_log_line_on_health_failure():
    """The rollback health-check-failed branch must log that :latest is being restored."""
    fn_lines = _lines_of_function()
    
    # Find the _wait_healthy failure branch
    wait_healthy_fail_start = None
    wait_healthy_fail_end = None
    depth = 0
    in_if_block = False
    
    for i, (lineno, line) in enumerate(fn_lines):
        if "if ! _wait_healthy" in line:
            wait_healthy_fail_start = i
            in_if_block = True
            depth = 1
            continue
        if in_if_block:
            stripped = line.strip()
            if stripped.startswith("if "):
                depth += 1
            if stripped == "fi":
                depth -= 1
                if depth == 0:
                    wait_healthy_fail_end = i
                    break
    
    assert wait_healthy_fail_start is not None
    branch_lines = fn_lines[wait_healthy_fail_start:wait_healthy_fail_end + 1]
    branch_text = "\n".join(line for _, line in branch_lines)
    
    has_log = bool(re.search(
        r'_log\s+"Rollback health check failed.*restored.*latest',
        branch_text
    ))
    assert has_log, (
        "The rollback health-check-failed branch should log a message when restoring :latest. "
        "Expected: _log \"Rollback health check failed — restored :latest to ...\"\n"
        f"Branch content:\n{branch_text}"
    )


def test_handle_rollback_no_restore_on_success_path():
    """The success path (rollback worked) must NOT restore :latest — :latest should stay pointing at :previous."""
    fn_lines = _lines_of_function()
    
    # The success path starts after the 'fi' that closes _wait_healthy's failure branch.
    # Find the _wait_healthy fi closing
    wait_healthy_fi_idx = None
    depth = 0
    in_if_block = False
    
    for i, (lineno, line) in enumerate(fn_lines):
        if "if ! _wait_healthy" in line:
            in_if_block = True
            depth = 1
            continue
        if in_if_block:
            stripped = line.strip()
            if stripped.startswith("if "):
                depth += 1
            if stripped == "fi":
                depth -= 1
                if depth == 0:
                    wait_healthy_fi_idx = i
                    break
    
    assert wait_healthy_fi_idx is not None
    
    # Lines after the fi are the success path
    success_lines = fn_lines[wait_healthy_fi_idx + 1:]
    success_text = "\n".join(line for _, line in success_lines)
    
    has_restore = bool(re.search(
        r'docker tag "\$\{latest_digest\}" "\$\{current_tag\}"',
        success_text
    ))
    assert not has_restore, (
        "The success path of _handle_rollback should NOT restore :latest. "
        "After a successful rollback, :latest correctly points at :previous. "
        f"Found restore in success path:\n{success_text}"
    )


def test_handle_rollback_no_restore_when_no_previous_image():
    """The 'no previous image' early-exit branch must not touch :latest at all (it's before the capture)."""
    fn_lines = _lines_of_function()
    
    # Find the "no previous image" branch
    no_prev_start = None
    no_prev_end = None
    depth = 0
    in_if_block = False
    
    for i, (lineno, line) in enumerate(fn_lines):
        if "docker image inspect" in line and "prev_tag" in line:
            # This is the condition line: if ! docker image inspect "${prev_tag}" ...
            no_prev_start = i
            in_if_block = True
            depth = 1
            continue
        if in_if_block:
            stripped = line.strip()
            if stripped.startswith("if "):
                depth += 1
            if stripped == "fi":
                depth -= 1
                if depth == 0:
                    no_prev_end = i
                    break
    
    assert no_prev_start is not None, (
        "'docker image inspect ${prev_tag}' not found in _handle_rollback"
    )
    
    # The no-prev branch should have exit 1 but no docker tag with latest_digest
    branch_lines = fn_lines[no_prev_start: no_prev_end + 1 if no_prev_end else len(fn_lines)]
    branch_text = "\n".join(line for _, line in branch_lines)
    
    has_bad_tag = bool(re.search(r'docker tag "\$\{latest_digest\}"', branch_text))
    assert not has_bad_tag, (
        "The 'no previous image' branch should NOT attempt to restore latest_digest — "
        "the capture hasn't happened yet at that point. "
        f"Branch content:\n{branch_text}"
    )
