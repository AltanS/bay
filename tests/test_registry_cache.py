"""Remote builds push from BuildKit, with an opt-in registry layer cache.

Two invariants, both scoped to the *remote* build strategy and both
implemented twice — once in the Ansible path (`roles/git_deploy/tasks/
remote_build.yml`, the `bay deploy` route) and once in the webhook
auto-build path (`roles/git_deploy/templates/rebuild.sh.j2`). The second
one is easy to forget, so every assertion below is made against both.

1. One `docker buildx build --push` carrying both `-t` tags. No `--load`,
   no separate `docker push`. `--load` exports the whole image into the
   build server's local daemon and `docker push` then re-uploads it;
   `--push` exports straight to the registry, once.
2. `--cache-to`/`--cache-from type=registry,ref=<repo>:buildcache` appear
   only when `git_deploy_registry_cache` is true. When it is false the
   flags must be absent entirely — not rendered empty.

The local strategy keeps `--load` (the local daemon is the consumer of the
image) and gains neither cache flag.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml
from jinja2 import Environment
from test_observability_contract import _ansible_env, _minimal_render_context

_REPO_ROOT = Path(__file__).resolve().parent.parent
_TEMPLATE = _REPO_ROOT / "roles" / "git_deploy" / "templates" / "rebuild.sh.j2"
_REMOTE_BUILD = _REPO_ROOT / "roles" / "git_deploy" / "tasks" / "remote_build.yml"
_DEFAULTS = _REPO_ROOT / "roles" / "git_deploy" / "defaults" / "main.yml"

_CACHE_REF = "type=registry,ref=registry.example.com/acme/storefront:buildcache"


# ── Ansible path (remote_build.yml) ──────────────────────────────────────


def _ansible_build_cmd(*, registry_cache: bool) -> str:
    """Render the `cmd` of remote_build.yml's build task into a flat string."""
    tasks = yaml.safe_load(_REMOTE_BUILD.read_text())
    cmds = [
        task["ansible.builtin.command"]["cmd"]
        for block in tasks
        for task in block.get("block", [])
        if "buildx build" in str(task.get("ansible.builtin.command", {}).get("cmd", ""))
    ]
    assert len(cmds) == 1, f"expected exactly one buildx task, found {len(cmds)}"
    env = Environment(trim_blocks=True, lstrip_blocks=True)
    rendered = env.from_string(cmds[0]).render(
        bay_buildx_builder="argo-builder",
        git_deploy_registry_cache=registry_cache,
        git_deploy_remote_build_dir="/opt/test/push-builds",
        _image_repo="registry.example.com/acme/storefront",
        _image_ref="registry.example.com/acme/storefront:latest",
        _remote_sha={"stdout": "abc123def456"},
        _dockerfile="Dockerfile",
        _context=".",
        _svc_name="storefront",
        _build={},
    )
    return " ".join(rendered.split())


def test_ansible_remote_build_pushes_from_buildx():
    cmd = _ansible_build_cmd(registry_cache=False)
    assert cmd.count("--push") == 1
    assert "--load" not in cmd
    assert "docker push" not in cmd
    # Both tags ride the single --push call, so the registry sees the SHA
    # tag and :latest from one export.
    assert "-t registry.example.com/acme/storefront:abc123def456" in cmd
    assert "-t registry.example.com/acme/storefront:latest" in cmd
    assert "--provenance=false" in cmd


def test_ansible_remote_build_has_no_separate_push_task():
    """The two `docker push` tasks are gone from the whole file, not just the cmd."""
    text = _REMOTE_BUILD.read_text()
    assert "docker push" not in text
    assert "--load" not in text


def test_ansible_cache_flags_absent_when_disabled():
    cmd = _ansible_build_cmd(registry_cache=False)
    assert "cache-to" not in cmd
    assert "cache-from" not in cmd


def test_ansible_cache_flags_present_when_enabled():
    cmd = _ansible_build_cmd(registry_cache=True)
    assert f"--cache-to {_CACHE_REF},mode=max" in cmd
    assert f"--cache-from {_CACHE_REF}" in cmd
    # Still one build, still one push.
    assert cmd.count("--push") == 1
    assert cmd.count("docker buildx build") == 1


# ── Webhook path (rebuild.sh.j2) ─────────────────────────────────────────


def _render_rebuild(*, registry_cache: bool) -> str:
    ctx = _minimal_render_context()
    ctx["git_deploy_registry_cache"] = registry_cache
    return _ansible_env().get_template(_TEMPLATE.name).render(**ctx)


def _remote_build_block(rendered: str) -> str:
    """The remote-strategy `docker buildx build` invocation, flattened.

    Located by content rather than line number: the observability contract
    re-maps these lines on every change, and a line-pinned slice here would
    silently start testing the local build instead.
    """
    marker = 'if ! docker buildx build \\\n      --builder'
    start = rendered.index(marker)
    end = rendered.index('"${CONTEXT}"', start)
    return " ".join(rendered[start:end].replace("\\\n", " ").split())


def test_webhook_remote_build_pushes_from_buildx():
    block = _remote_build_block(_render_rebuild(registry_cache=False))
    assert block.count("--push") == 1
    assert "--load" not in block
    assert '-t "${IMAGE_REPO}:${SHA}"' in block
    assert '-t "${IMAGE_REF}"' in block


def test_webhook_remote_path_has_no_separate_docker_push():
    rendered = _render_rebuild(registry_cache=False)
    assert "docker push" not in rendered


def test_webhook_cache_flags_absent_when_disabled():
    rendered = _render_rebuild(registry_cache=False)
    assert "cache-to" not in rendered
    assert "cache-from" not in rendered


def test_webhook_cache_flags_present_when_enabled():
    block = _remote_build_block(_render_rebuild(registry_cache=True))
    assert '--cache-to "type=registry,ref=${IMAGE_REPO}:buildcache,mode=max"' in block
    assert '--cache-from "type=registry,ref=${IMAGE_REPO}:buildcache"' in block


def test_webhook_local_build_keeps_load_and_no_cache():
    """The local strategy exports to the local daemon and gains no cache flags."""
    rendered = _render_rebuild(registry_cache=True)
    local_start = rendered.index("Local strategy: build and restart on this server")
    local = rendered[local_start:]
    assert "--load" in local
    assert "cache-to" not in local
    assert "cache-from" not in local


# ── Shared invariants ────────────────────────────────────────────────────


@pytest.mark.parametrize("path", [_TEMPLATE, _REMOTE_BUILD])
def test_cache_from_is_tolerant_of_a_cold_cache(path: Path):
    """A missing `:buildcache` ref is the normal first run and must not fail.

    BuildKit treats an unresolvable `--cache-from` registry ref as a cache
    miss. That tolerance is lost the moment someone adds a hard requirement
    to the import, so pin its absence rather than trusting the default.
    """
    text = path.read_text()
    for line in text.splitlines():
        if "cache-from" in line:
            assert "require" not in line, (
                f"{path.name}: `--cache-from` must stay tolerant of a cold "
                f"cache (no `:buildcache` tag yet) — found: {line.strip()}"
            )


@pytest.mark.parametrize("path", [_TEMPLATE, _REMOTE_BUILD])
def test_cache_ref_is_derived_from_the_image_repo(path: Path):
    """The cache tag hangs off the image repo, never a hand-built string."""
    refs = re.findall(r"ref=(.+?)(?:,mode=max)?[\"\s\\]*$", path.read_text(), re.M)
    assert refs, f"{path.name}: no cache ref found"
    for ref in refs:
        assert ref.endswith(":buildcache"), ref
        assert "IMAGE_REPO" in ref or "_image_repo" in ref, ref


def test_registry_cache_defaults_to_false():
    defaults = yaml.safe_load(_DEFAULTS.read_text())
    assert defaults["git_deploy_registry_cache"] is False


def test_registry_cache_is_documented_in_defaults():
    text = _DEFAULTS.read_text()
    idx = text.index("git_deploy_registry_cache:")
    comment = text[max(0, idx - 1200) : idx]
    for needle in ("buildcache", "registry", "prune"):
        assert needle in comment.lower(), f"defaults comment must mention {needle!r}"


def test_rebuild_sh_parses_with_the_cache_enabled(tmp_path):
    """The cache flags sit inside a line-continued command; a stray newline
    from the `{% if %}` block would break the whole script. The observability
    suite only parse-checks the default (cache-off) render."""
    import subprocess

    out = tmp_path / "rebuild-cache-on.sh"
    out.write_text(_render_rebuild(registry_cache=True))
    result = subprocess.run(["bash", "-n", str(out)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
