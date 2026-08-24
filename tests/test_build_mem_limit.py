"""Unit tests guarding the build-memory variables' shape and plumbing.

Two distinct knobs are guarded here:

  * `git_deploy_build_mem_limit` — drives systemd `MemoryMax=` on
    bay-build@.service. Caps the rebuild.sh wrapper cgroup.
  * `git_deploy_buildkit_memory_max` — applied via `docker update --memory`
    on `buildx_buildkit_argo-builder0`. Caps the actual build process,  # legacy-argo: live buildx builder name on hosts, migrate separately
    which runs in a separate cgroup from the wrapper.

Regression context:
  2026-04-17 — live verification of the build-memory rollout revealed the default was `"2g"`
  (lowercase), which systemd silently rejected:
    journalctl: "Invalid memory limit '2g', ignoring"
  MemoryMax was effectively unset, and a concurrent locale build wedged the
  demo infra host by exhausting all RAM. v0.81.1 corrected the default
  to `"2G"`. These tests prevent regression to lowercase on either var and
  guard against anyone hardcoding values in the template or task.

Split context (issue #9):
  Originally one var (`git_deploy_build_mem_limit`) drove both the systemd
  unit and the buildkit container. Issue #9 needed asymmetric values
  (wrapper 2G, buildkit 2.5G), so the buildkit-side path was forked into
  `git_deploy_buildkit_memory_max`.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

_REPO_ROOT = Path(__file__).parent.parent
_DEFAULTS = _REPO_ROOT / "roles" / "git_deploy" / "defaults" / "main.yml"
_SERVICE_TEMPLATE = _REPO_ROOT / "roles" / "git_deploy" / "templates" / "bay-build@.service.j2"
_SETUP_BUILDER = _REPO_ROOT / "roles" / "git_deploy" / "tasks" / "setup_builder.yml"
_GIT_DEPLOY_MAIN = _REPO_ROOT / "roles" / "git_deploy" / "tasks" / "main.yml"

# systemd accepts K/M/G/T suffixes (uppercase) but only integers. Lowercase
# is silently rejected with "Invalid memory limit" in the journal. Docker
# accepts both lower/upper case AND decimals (e.g. "2.5G"). Two regexes
# because the vars feed two different consumers:
#   * git_deploy_build_mem_limit -> systemd MemoryMax= (strict)
#   * git_deploy_buildkit_memory_max -> docker update --memory (lenient)
_SYSTEMD_RE = re.compile(r"^\d+[KMGTP]$")
_DOCKER_RE = re.compile(r"^\d+(\.\d+)?[KMGTP]$")


def test_default_uses_capital_suffix() -> None:
    with _DEFAULTS.open() as f:
        defaults = yaml.safe_load(f)
    # The wrapper var feeds systemd directly — must be a literal int+suffix.
    value = defaults["git_deploy_build_mem_limit"]
    assert _SYSTEMD_RE.match(value), (
        f"git_deploy_build_mem_limit default is {value!r}; must use an "
        f"uppercase integer suffix accepted by systemd MemoryMax."
    )
    # The buildkit var defaults to a Jinja reference to the wrapper var so
    # consumers who only override the wrapper get unified caps preserved on
    # upgrade. Either the literal pattern OR the reference is acceptable.
    value = defaults["git_deploy_buildkit_memory_max"]
    is_reference = value.strip() == "{{ git_deploy_build_mem_limit }}"
    assert is_reference or _DOCKER_RE.match(value), (
        f"git_deploy_buildkit_memory_max default is {value!r}; must be "
        f"either a literal Docker-acceptable size or the Jinja reference "
        f"'{{{{ git_deploy_build_mem_limit }}}}' for backwards-compat."
    )


def test_systemd_template_references_variable() -> None:
    contents = _SERVICE_TEMPLATE.read_text()
    assert "{{ git_deploy_build_mem_limit }}" in contents, (
        "bay-build@.service.j2 must render MemoryMax from "
        "{{ git_deploy_build_mem_limit }} — do not hardcode."
    )
    # Guard against someone adding a hardcoded MemoryMax line alongside the
    # templated one.
    hardcoded = re.findall(r"^MemoryMax=\d+[KMGTPkmgtp]\s*$", contents, re.MULTILINE)
    assert not hardcoded, f"Hardcoded MemoryMax lines found: {hardcoded}"


def test_setup_builder_references_variable() -> None:
    contents = _SETUP_BUILDER.read_text()
    assert "{{ git_deploy_buildkit_memory_max }}" in contents, (
        "setup_builder.yml docker update --memory must reference "
        "{{ git_deploy_buildkit_memory_max }} — do not hardcode."
    )
    # Guard against a literal `--memory 2G` slipping in.
    hardcoded = re.findall(r"--memory\s+\d+[KMGTPkmgtp]\b", contents)
    assert not hardcoded, f"Hardcoded --memory literal found: {hardcoded}"


def test_remote_build_setup_applies_memory_limit() -> None:
    """Remote-only hosts skip setup_builder.yml (which only runs for local
    builds), so the buildkit memory cap must also be applied from main.yml's
    remote-build setup block. 2026-04-17 live verification exposed this:
    MemoryMax was set on the systemd unit but the buildkit sidecar had no
    limit."""
    contents = _GIT_DEPLOY_MAIN.read_text()
    # The remote block runs `docker update --memory <var> buildx_buildkit_<name>`.
    pattern = re.compile(
        r"docker\s+update\s+.*?--memory\s+\{\{\s*git_deploy_buildkit_memory_max\s*\}\}"
        r".*?buildx_buildkit_",
        re.DOTALL,
    )
    assert pattern.search(contents), (
        "git_deploy/tasks/main.yml must apply `docker update --memory "
        "{{ git_deploy_buildkit_memory_max }}` to the buildx buildkit "
        "container after creation. Without it, remote-only hosts run "
        "buildkit with no memory cap even when the var is set."
    )


@pytest.mark.parametrize("bad_value", ["2g", "4g", "512m", "1t"])
def test_systemd_regex_rejects_lowercase(bad_value: str) -> None:
    assert not _SYSTEMD_RE.match(bad_value)


@pytest.mark.parametrize("good_value", ["2G", "3G", "4G", "512M", "1T"])
def test_systemd_regex_accepts_uppercase_int(good_value: str) -> None:
    assert _SYSTEMD_RE.match(good_value)


@pytest.mark.parametrize("bad_value", ["2.5G", "0.5G"])
def test_systemd_regex_rejects_decimals(bad_value: str) -> None:
    """systemd MemoryMax does not accept decimals — guard the systemd var."""
    assert not _SYSTEMD_RE.match(bad_value)


@pytest.mark.parametrize("good_value", ["2G", "2.5G", "3G", "1.5T"])
def test_docker_regex_accepts_int_and_decimal(good_value: str) -> None:
    assert _DOCKER_RE.match(good_value)
