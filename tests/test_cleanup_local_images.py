"""Unit tests for post-push local image cleanup logic.

The cleanup task in roles/git_deploy/tasks/cleanup_local_images.yml uses a
pure Jinja2 expression to select content-hash tags for removal. These tests
exercise that expression directly against the Ansible Jinja env so the filter
chain is validated without needing an ansible-playbook harness.

Retention rules:
  1. Never remove `latest`.
  2. Never remove dangling tags (`<none>`).
  3. Never remove tags whose `<repo>:<tag>` reference is currently used by a
     running container.
  4. Keep the newest N content-hash tags (by docker CreatedAt); remove the
     rest.
"""

from __future__ import annotations

import re
from pathlib import Path

from jinja2 import Environment

_REPO_ROOT = Path(__file__).parent.parent
_CLEANUP_TASK = _REPO_ROOT / "roles" / "git_deploy" / "tasks" / "cleanup_local_images.yml"
_REBUILD_SH = _REPO_ROOT / "roles" / "git_deploy" / "templates" / "rebuild.sh.j2"

# Mirrors the Jinja2 expression in cleanup_local_images.yml's set_fact —
# kept in sync by a parity test below so drift in either location is caught.
_SELECTION_EXPR = """\
{% set _running_tags_for_repo = (
    running_refs
    | select('match', '^' ~ (image_repo | regex_escape) ~ ':')
    | map('regex_replace', '^' ~ (image_repo | regex_escape) ~ ':', '')
    | list
) %}
{{
  docker_images_output
  | map('split', '|')
  | rejectattr('0', 'eq', 'latest')
  | rejectattr('0', 'eq', '<none>')
  | list
  | sort(attribute=1, reverse=true)
  | map(attribute=0)
  | reject('in', _running_tags_for_repo)
  | list
}}\
"""


def _make_env() -> Environment:
    env = Environment()
    # Stand-ins for the Ansible-specific filters the task uses. Behaviors
    # match ansible.builtin.split / regex_replace / regex_escape.
    env.filters["split"] = lambda s, sep=None: s.split(sep)
    env.filters["regex_replace"] = lambda s, pat, rep: re.sub(pat, rep, s)
    env.filters["regex_escape"] = re.escape
    # 'match' is an Ansible test; mirror its behavior against re.match.
    env.tests["match"] = lambda s, pat: re.match(pat, s) is not None
    return env


def _select(
    images: list[str],
    running: list[str],
    keep: int,
    repo: str = "registry.example.com/demo/svc",
) -> list[str]:
    template = _make_env().from_string(_SELECTION_EXPR)
    raw = template.render(
        docker_images_output=images,
        running_refs=running,
        image_repo=repo,
    )
    # eval is acceptable here: input is a Jinja-rendered Python-literal list
    # produced by `{{ ... | list }}`.
    sorted_tags = eval(raw.strip(), {"__builtins__": {}}, {})
    return sorted_tags[keep:]


# ── Fixtures ───────────────────────────────────────────────────────────

# Format matches `docker images <repo> --format '{{.Tag}}|{{.CreatedAt}}'`.
# CreatedAt is a lex-sortable timestamp; newest first after reverse sort.
_TAGS_MIXED = [
    "latest|2026-04-17 12:00:00 +0000",
    "43618a3f73dc|2026-04-17 12:00:00 +0000",
    "a24aa4c01c69|2026-04-17 11:00:00 +0000",
    "52eea1111db0|2026-04-17 10:00:00 +0000",
    "59df05467176|2026-04-17 09:00:00 +0000",
    "d74ef60121fc|2026-04-17 08:00:00 +0000",
    "bdf0e31f4243|2026-04-17 07:00:00 +0000",
]


# ── Core retention behavior ────────────────────────────────────────────


def test_keeps_latest_always() -> None:
    removed = _select(_TAGS_MIXED, running=[], keep=3)
    assert "latest" not in removed


def test_keeps_newest_n_content_hash_tags() -> None:
    removed = _select(_TAGS_MIXED, running=[], keep=3)
    # Top 3 newest non-latest content hashes are kept: 43618a3f, a24aa4c0, 52eea111
    assert "43618a3f73dc" not in removed
    assert "a24aa4c01c69" not in removed
    assert "52eea1111db0" not in removed
    # Older ones are removed:
    assert set(removed) == {"59df05467176", "d74ef60121fc", "bdf0e31f4243"}


def test_skips_running_container_tag() -> None:
    # A container is running `<repo>:bdf0e31f4243` — that tag must survive
    # even though it's the oldest content-hash in the list.
    repo = "registry.example.com/demo/svc"
    removed = _select(
        _TAGS_MIXED,
        running=[f"{repo}:bdf0e31f4243"],
        keep=1,
        repo=repo,
    )
    assert "bdf0e31f4243" not in removed


def test_ignores_running_containers_from_other_repos() -> None:
    # Containers running other repos should have zero effect on this repo's
    # cleanup — the `select('match', '^<repo>:')` filter scopes the check.
    repo = "registry.example.com/demo/svc"
    removed = _select(
        _TAGS_MIXED,
        running=["traefik:v3.6", "ghcr.io/project-zot/zot:v2.1.15"],
        keep=3,
        repo=repo,
    )
    assert set(removed) == {"59df05467176", "d74ef60121fc", "bdf0e31f4243"}


def test_skips_dangling_tags() -> None:
    images = _TAGS_MIXED + ["<none>|2026-04-17 05:00:00 +0000"]
    removed = _select(images, running=[], keep=10)  # keep larger than list
    assert "<none>" not in removed


def test_empty_image_list_returns_empty() -> None:
    assert _select([], running=[], keep=3) == []


def test_keep_n_larger_than_tag_count() -> None:
    images = [
        "latest|2026-04-17 12:00:00 +0000",
        "t1|2026-04-17 12:00:00 +0000",
        "t2|2026-04-17 11:00:00 +0000",
    ]
    assert _select(images, running=[], keep=10) == []


def test_keep_zero_removes_all_non_protected() -> None:
    # keep=0 removes everything except latest/dangling/running. The include
    # in remote_build.yml is guarded by `git_deploy_build_keep_tags | int > 0`,
    # so callers who want to disable cleanup set keep_tags: 0.
    removed = _select(_TAGS_MIXED, running=[], keep=0)
    assert "latest" not in removed
    assert len(removed) == 6


# ── Parity with the actual task file ───────────────────────────────────


def test_expression_matches_task_file() -> None:
    """Markers from the task's filter chain must appear in order — guards
    against edits to either location drifting out of sync."""
    task_text = _CLEANUP_TASK.read_text()
    ordered_markers = [
        "map('split', '|')",
        "rejectattr('0', 'eq', 'latest')",
        "rejectattr('0', 'eq', '<none>')",
        "sort(attribute=1, reverse=true)",
        "map(attribute=0)",
        "reject('in', _running_tags_for_repo)",
    ]
    pos = 0
    for marker in ordered_markers:
        next_pos = task_text.find(marker, pos)
        assert next_pos != -1, f"Filter marker missing from task file: {marker!r}"
        pos = next_pos + len(marker)


def test_task_file_includes_keep_tags_slice() -> None:
    """Task file must slice _cleanup_tags_to_remove by git_deploy_build_keep_tags."""
    task_text = _CLEANUP_TASK.read_text()
    assert "_cleanup_tags_to_remove[git_deploy_build_keep_tags:]" in task_text


def test_task_file_tolerates_image_in_use_errors() -> None:
    """docker rmi can race with container restarts; the task must tolerate
    'image is being used' failures as a belt-and-suspenders guard even
    though Jinja already filters running containers."""
    task_text = _CLEANUP_TASK.read_text()
    assert '"image is being used" not in _cleanup_rmi_result.stderr' in task_text
    assert '"No such image" not in _cleanup_rmi_result.stderr' in task_text


# ── Webhook auto-build path (rebuild.sh) parity ────────────────────────


def test_rebuild_sh_also_prunes_after_push() -> None:
    """The Ansible cleanup task only runs during `bin/bay deploy`, but
    webhook auto-builds execute rebuild.sh directly and never reach it.
    Without an equivalent pruning block in the generated bash script, the
    disk bloat that this cleanup logic is meant to fix returns on the auto-build path.
    This test guards against someone removing or reverting that block.

    The remote strategy now pushes from BuildKit (`buildx --push`), so there
    is no separate `docker push` to anchor on and — for images built after
    that change — nothing left in the local store to prune. The block stays
    because build servers upgraded in place still hold the tags every
    `--load` build before it left behind."""
    sh_text = _REBUILD_SH.read_text()
    # The remote-strategy build block must invoke docker rmi somewhere after
    # the push-completion marker.
    push_idx = sh_text.find('_log "Pushed ${IMAGE_REPO}:${SHA}')
    assert push_idx != -1, "rebuild.sh.j2 missing the push-completion marker"
    # Search for the pruning markers only in the region AFTER the push.
    tail = sh_text[push_idx:]
    markers = [
        'KEEP_TAGS="{{ git_deploy_build_keep_tags',
        "docker images \"${IMAGE_REPO}\"",
        'docker rmi "${IMAGE_REPO}:${tag}"',
    ]
    for marker in markers:
        assert marker in tail, (
            f"rebuild.sh.j2 push-cleanup block missing marker: {marker!r}"
        )


def test_rebuild_sh_cleanup_keeps_latest() -> None:
    """The bash pruning block must explicitly exclude the `latest` tag,
    matching the Ansible task's behavior."""
    sh_text = _REBUILD_SH.read_text()
    assert "grep -v '^latest|'" in sh_text, (
        "rebuild.sh.j2 cleanup must filter out `latest|` lines so the "
        "always-present latest tag is never removed."
    )


def test_rebuild_sh_cleanup_tolerates_an_empty_local_store() -> None:
    """With `buildx --push` the build server holds no local image at all.

    `docker images` then prints nothing, the first `grep -v` in the candidate
    pipeline exits 1, and under `set -euo pipefail` that aborts the script via
    the ERR trap — turning a successful build into a "Webhook deploy failed"
    alert. The pipeline must swallow the empty case."""
    sh_text = _REBUILD_SH.read_text()
    assert "| cut -d'|' -f1 || true)" in sh_text, (
        "the content-hash tag candidate pipeline must tolerate an empty "
        "`docker images` result (no local image after a --push build)"
    )
