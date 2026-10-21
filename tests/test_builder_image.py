"""eMudhra CA incident (Oct 2026): image pins for the rig parts that trusted
"emSign ECC Root CA - G3".

These tests pin:
  * the local buildx builder's BuildKit image is a role variable, pinned by
    digest, and both builder paths (setup_builder.yml and the build server
    block in main.yml) create the builder with it;
  * a builder with another image (or none recorded) is removed under the build
    lock and created again; a matching or missing builder is not removed;
  * zot mounts the host CA bundle over its own, in the reconciler spec and in
    the compose mirror alike;
  * the webhook and tailnet-identity images build from the same python base,
    pinned by digest.
"""

from __future__ import annotations

import re
from pathlib import Path

import jinja2
import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parent.parent
_ROLE = _REPO_ROOT / "roles" / "git_deploy"
_DEFAULTS = _ROLE / "defaults" / "main.yml"
_BUILDER_FILES = [_ROLE / "tasks" / "setup_builder.yml", _ROLE / "tasks" / "main.yml"]
_BUILD_SPECS = _REPO_ROOT / "roles" / "container_lifecycle" / "tasks" / "build_specs.yml"
_ZOT_COMPOSE = _REPO_ROOT / "roles" / "deploy_stack" / "templates" / "_zot.j2"
_DOCKERFILES = [
    _ROLE / "files" / "webhook" / "Dockerfile",
    _REPO_ROOT / "roles" / "tailnet_identity" / "files" / "tailnet-identity" / "Dockerfile",
]
_CA_MOUNT = "/etc/ssl/certs/ca-certificates.crt:/etc/ssl/certs/ca-certificates.crt:ro"
_DIGEST_REF = re.compile(r"^[^@\s]+:[^@\s]+@sha256:[0-9a-f]{64}$")


def _tasks(path: Path) -> list[dict]:
    """Every task in a task file, blocks flattened."""
    out: list[dict] = []

    def walk(items: list[dict]) -> None:
        for item in items or []:
            out.append(item)
            for key in ("block", "rescue", "always"):
                walk(item.get(key, []))

    walk(yaml.safe_load(path.read_text()))
    return out


def _cmd(task: dict) -> str:
    cmd = task.get("ansible.builtin.command", {})
    return " ".join(str(cmd.get("cmd", "") if isinstance(cmd, dict) else cmd).split())


def _env() -> jinja2.Environment:
    env = jinja2.Environment()
    env.filters["bool"] = lambda v: (
        v if isinstance(v, bool) else str(v).lower() in ("1", "true", "yes", "on")
    )
    return env


def _holds(cond: str, **ctx) -> bool:
    return bool(_env().compile_expression(cond.strip())(**ctx))


def _builder_tasks(path: Path) -> tuple[dict, dict, dict]:
    tasks = _tasks(path)
    drift = next(t for t in tasks if "_buildx_image_drift" in t.get("ansible.builtin.set_fact", {}))
    rm = next(t for t in tasks if "buildx rm" in _cmd(t))
    create = next(
        t for t in tasks if "buildx create" in _cmd(t) and "--driver docker-container" in _cmd(t)
    )
    return drift, rm, create


def _drift(task: dict, **ctx) -> bool:
    expr = task["ansible.builtin.set_fact"]["_buildx_image_drift"].strip()
    assert expr.startswith("{{") and expr.endswith("}}")
    return _holds(expr[2:-2], **ctx)


_IMAGE = yaml.safe_load(_DEFAULTS.read_text())["git_deploy_buildkit_image"]


def _inspect(rc: int, image: str | None) -> dict:
    opts = f'Driver Options:        network="host" image="{image}"\n' if image else ""
    return {
        "rc": rc,
        "stdout": f"Name: bay-builder\nNodes:\nName: bay-builder0\n{opts}Status: running\n",
    }


def test_default_buildkit_image_is_pinned_by_digest():
    assert _DIGEST_REF.match(_IMAGE), _IMAGE
    assert _IMAGE.startswith("moby/buildkit:v0.34.1@")


@pytest.mark.parametrize("path", _BUILDER_FILES, ids=lambda p: p.name)
def test_builder_is_created_with_the_pinned_image(path: Path):
    _, _, create = _builder_tasks(path)
    rendered = (
        _env()
        .from_string(_cmd(create))
        .render(bay_buildx_builder="bay-builder", git_deploy_buildkit_image=_IMAGE)
    )
    assert f"--driver-opt image={_IMAGE}" in rendered
    unset = (
        _env()
        .from_string(_cmd(create))
        .render(bay_buildx_builder="bay-builder", git_deploy_buildkit_image="")
    )
    assert "image=" not in unset


@pytest.mark.parametrize("path", _BUILDER_FILES, ids=lambda p: p.name)
def test_builder_is_recreated_only_when_the_image_differs(path: Path):
    drift, rm, create = _builder_tasks(path)
    cases = {
        "same image": (_inspect(0, _IMAGE), False),
        "no image recorded": (_inspect(0, None), True),
        "older image": (_inspect(0, "moby/buildkit:buildx-stable-1"), True),
        "no builder": (_inspect(1, None), False),
    }
    for label, (inspect, want) in cases.items():
        got = _drift(drift, _buildx_inspect=inspect, git_deploy_buildkit_image=_IMAGE)
        assert got is want, label
        assert _holds(rm["when"], _buildx_image_drift=got) is want, label
        assert _holds(create["when"], _buildx_inspect=inspect, _buildx_image_drift=got) is (
            want or inspect["rc"] != 0
        )
    # An empty image turns the check off.
    assert not _drift(drift, _buildx_inspect=_inspect(0, None), git_deploy_buildkit_image="")


@pytest.mark.parametrize("path", _BUILDER_FILES, ids=lambda p: p.name)
def test_builder_removal_waits_for_the_build_lock(path: Path):
    _, rm, _ = _builder_tasks(path)
    cmd = _cmd(rm)
    assert cmd.startswith(
        "flock -x -w {{ git_deploy_build_lock_timeout }} {{ git_deploy_build_lock_path }} "
    )
    assert cmd.endswith("docker buildx rm {{ bay_buildx_builder }}")


def test_zot_mounts_the_host_ca_bundle_in_spec_and_compose():
    spec_text = _BUILD_SPECS.read_text()
    zot_task = next(
        t for t in yaml.safe_load(spec_text) if t.get("name") == "Build zot registry container spec"
    )
    assert _CA_MOUNT in zot_task["vars"]["_zot_spec"]["volumes"]
    assert f"- {_CA_MOUNT}" in _ZOT_COMPOSE.read_text()


def test_python_images_share_one_digest_pinned_base():
    froms = [
        next(line for line in p.read_text().splitlines() if line.startswith("FROM "))
        for p in _DOCKERFILES
    ]
    assert len(set(froms)) == 1, froms
    ref = froms[0].removeprefix("FROM ").strip()
    assert ref.startswith("python:3.12-slim@")
    assert _DIGEST_REF.match(ref), ref
