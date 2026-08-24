"""Unit tests for the Zot storage driver shape and plumbing.

Issue #10 — Zot role gained `zot_storage_driver: filesystem | s3` so
infra hosts with limited disk (demo-infra: 38 GiB) can offload OCI
blobs to S3. With the S3 driver the role must:

  1. Emit a `storageDriver` + `cacheDriver` block in `config.json`.
  2. Pull AWS credentials into the Zot container via `env_file`, NOT
     bake them into config.json (which is mounted ro into the container
     and persists on disk).
  3. Validate non-empty bucket/region/keys before deploy.

Filesystem mode (the default, backwards-compat) MUST emit neither the
storageDriver block nor the env_file directive — keeping the on-disk
artifact identical to pre-#10 deploys for consumers that don't opt in.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from helpers import make_ansible_env

_REPO_ROOT = Path(__file__).parent.parent
_ZOT_DEFAULTS = _REPO_ROOT / "roles" / "zot" / "defaults" / "main.yml"
_ZOT_TASKS = _REPO_ROOT / "roles" / "zot" / "tasks" / "main.yml"
_ZOT_CONFIG_TPL = _REPO_ROOT / "roles" / "zot" / "templates"
_DEPLOY_STACK_TPL = _REPO_ROOT / "roles" / "deploy_stack" / "templates"
_BUILD_SPECS = (
    _REPO_ROOT / "roles" / "container_lifecycle" / "tasks" / "build_specs.yml"
)


def _defaults() -> dict:
    return yaml.safe_load(_ZOT_DEFAULTS.read_text())


def _to_json(value):
    """Ansible-compatible `to_json` filter for the test renderer."""
    return json.dumps(value)


def _bool(value) -> bool:
    """Ansible-compatible `bool` filter."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() in {"true", "yes", "1", "on"}
    return bool(value)


def _render_config(**overrides) -> str:
    env = make_ansible_env(_ZOT_CONFIG_TPL)
    env.filters["to_json"] = _to_json
    env.filters["bool"] = _bool
    base = {
        "zot_storage_root": "/var/lib/registry",
        "zot_gc_enabled": True,
        "zot_gc_delay": "4h",
        "zot_gc_interval": "24h",
        "zot_untagged_retention_delay": "72h",
        "zot_retention_keep_count": 10,
        "zot_retention_keep_within": "720h",
        "zot_storage_driver": "filesystem",
        "zot_s3_bucket": "",
        "zot_s3_region": "",
        "zot_s3_rootdirectory": "/zot",
        "zot_s3_secure": True,
        "zot_s3_force_path_style": False,
        "zot_remote_cache": False,
        "zot_cache_driver": "boltdb",
        "zot_cache_dir": "/var/lib/registry",
        "zot_port": 5000,
        "zot_domain": "registry.example.com",
    }
    base.update(overrides)
    return env.get_template("config.json.j2").render(**base)


def _render_compose_zot(**overrides) -> str:
    """Render the _zot.j2 snippet with a stub macros context.

    `_zot.j2` references {{ macros.logging_block({}) }} from the parent
    docker-compose template. For this test we stub the macros symbol via
    an inline include so the snippet renders standalone.
    """
    env = make_ansible_env(_DEPLOY_STACK_TPL)
    env.filters["bool"] = _bool
    # Provide a minimal `macros` shim with the logging_block method.
    env.globals["macros"] = type(
        "Macros", (), {"logging_block": staticmethod(lambda _: "")}
    )()
    base = {
        "zot_enabled": True,
        "zot_image": "ghcr.io/project-zot/zot:v2.1.15",
        "zot_config_dir": "/opt/zot/config",
        "zot_data_dir": "/opt/zot/data",
        "zot_domain": "registry.example.com",
        "zot_port": 5000,
        "traefik_docker_network": "services",
        "stack_dir": "/opt/teststack",
        "zot_storage_driver": "filesystem",
    }
    base.update(overrides)
    return env.get_template("_zot.j2").render(**base)


# ── Defaults shape ──────────────────────────────────────────────────


def test_default_storage_driver_is_filesystem() -> None:
    """Backwards compat: existing consumers must not be flipped to S3."""
    assert _defaults()["zot_storage_driver"] == "filesystem"


def test_default_s3_credentials_are_empty() -> None:
    """Empty defaults force consumers to opt in via vault — never let
    a consumer accidentally inherit a previous user's keys."""
    d = _defaults()
    assert d["zot_aws_access_key_id"] == ""
    assert d["zot_aws_secret_access_key"] == ""
    assert d["zot_s3_bucket"] == ""
    assert d["zot_s3_region"] == ""


def test_gc_defaults_are_conservative() -> None:
    """Issue #10 counsel review: old 1h/6h/missing was too aggressive.
    Keep these values long enough that a slow build's manifest can't
    race the GC sweep."""
    d = _defaults()
    assert d["zot_gc_delay"] == "4h"
    assert d["zot_gc_interval"] == "24h"
    assert d["zot_untagged_retention_delay"] == "72h"


# ── config.json rendering ───────────────────────────────────────────


def test_filesystem_config_renders_valid_json_without_s3_block() -> None:
    rendered = _render_config(zot_storage_driver="filesystem")
    parsed = json.loads(rendered)
    assert "storageDriver" not in parsed["storage"]
    assert "cacheDriver" not in parsed["storage"]
    assert "remoteCache" not in parsed["storage"]
    assert parsed["storage"]["rootDirectory"] == "/var/lib/registry"


def test_s3_config_single_node_omits_cache_driver_block() -> None:
    """Issue #11 regression: Zot v2 rejects an explicit `cacheDriver`
    block unless `remoteCache: true`. Single-node S3 deployments (the
    common case, zot_remote_cache=False) must render `remoteCache: false`
    and OMIT the `cacheDriver` block entirely. Otherwise Zot fails at
    startup with `unsupported database driver, cacheDriver: boltdb`."""
    rendered = _render_config(
        zot_storage_driver="s3",
        zot_s3_bucket="my-bucket",
        zot_s3_region="eu-central-1",
    )
    parsed = json.loads(rendered)
    sd = parsed["storage"]["storageDriver"]
    assert sd["name"] == "s3"
    assert sd["bucket"] == "my-bucket"
    assert sd["region"] == "eu-central-1"
    assert sd["rootdirectory"] == "/zot"
    assert sd["secure"] is True
    assert sd["forcepathstyle"] is False
    assert parsed["storage"]["remoteCache"] is False
    assert "cacheDriver" not in parsed["storage"], (
        "remoteCache=False must omit cacheDriver — see issue #11"
    )


def test_s3_config_remote_cache_includes_cache_driver_with_directory() -> None:
    """HA path: zot_remote_cache=True opts into an explicit cacheDriver
    block. rootDir must be a *directory* (Zot creates `cache.db` inside
    it) — never a file path, or Zot tries to mkdir under a file."""
    rendered = _render_config(
        zot_storage_driver="s3",
        zot_s3_bucket="my-bucket",
        zot_s3_region="eu-central-1",
        zot_remote_cache=True,
        zot_cache_driver="boltdb",
        zot_cache_dir="/var/lib/registry",
    )
    parsed = json.loads(rendered)
    assert parsed["storage"]["remoteCache"] is True
    cd = parsed["storage"]["cacheDriver"]
    assert cd["name"] == "boltdb"
    assert cd["rootDir"] == "/var/lib/registry"
    assert not cd["rootDir"].endswith(".db"), (
        "cacheDriver.rootDir is a directory, not a file path"
    )


def test_s3_config_has_no_credentials_baked_in() -> None:
    """Critical: AWS keys must NEVER end up in config.json. They go
    in env_file. Anyone reading the deployed config.json on the host
    must not be able to recover credentials."""
    rendered = _render_config(
        zot_storage_driver="s3",
        zot_s3_bucket="my-bucket",
        zot_s3_region="eu-central-1",
    )
    # Generic sanity — these var names must not appear in the rendered
    # output regardless of the AWS_* values they happen to hold.
    forbidden = (
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "accessKey",
        "secretKey",
    )
    for needle in forbidden:
        assert needle not in rendered, (
            f"config.json must not contain {needle!r} — credentials "
            f"belong in env_file (mode 0600), never in this file."
        )


@pytest.mark.parametrize(
    "delay_var,delay_value",
    [
        ("zot_gc_delay", "8h"),
        ("zot_gc_interval", "48h"),
        ("zot_untagged_retention_delay", "168h"),
    ],
)
def test_gc_values_flow_from_vars_not_hardcoded(
    delay_var: str, delay_value: str
) -> None:
    """Guard against re-introducing hardcoded delays in the template."""
    rendered = _render_config(**{delay_var: delay_value})
    assert delay_value in rendered, (
        f"{delay_var}={delay_value!r} did not appear in rendered config; "
        f"someone may have hardcoded the value in config.json.j2 again."
    )


# ── _zot.j2 compose snippet ─────────────────────────────────────────


def test_filesystem_mode_does_not_add_env_file() -> None:
    rendered = _render_compose_zot(zot_storage_driver="filesystem")
    assert "env_file" not in rendered, (
        "Filesystem driver must not pull in zot.env — that file only "
        "exists when the S3 driver is active."
    )


def test_s3_mode_adds_env_file() -> None:
    rendered = _render_compose_zot(
        zot_storage_driver="s3",
        stack_dir="/opt/demo",
    )
    assert "env_file:" in rendered
    assert "/opt/demo/env/zot.env" in rendered


# ── tasks/main.yml validation ───────────────────────────────────────


def test_tasks_validates_s3_required_fields() -> None:
    """The role must refuse to deploy with zot_storage_driver=s3 and
    empty bucket/region/keys."""
    contents = _ZOT_TASKS.read_text()
    # All four required vars must be asserted non-empty in a single
    # `when: zot_storage_driver == 's3'` block.
    assert "zot_storage_driver == 's3'" in contents
    for var in (
        "zot_s3_bucket",
        "zot_s3_region",
        "zot_aws_access_key_id",
        "zot_aws_secret_access_key",
    ):
        assert var in contents, (
            f"tasks/main.yml must validate {var} when storage driver is s3"
        )


def test_tasks_renders_env_file_with_mode_0600() -> None:
    """AWS creds in zot.env must be 0600 — anyone who can read it can
    push to / delete from the registry's S3 bucket."""
    contents = _ZOT_TASKS.read_text()
    # Find the env-file render task and confirm it uses mode 0600.
    assert "stack_dir }}/env/zot.env" in contents
    assert 'mode: "0600"' in contents
    # Also confirm no_log: true on the credential render task.
    assert "no_log: true" in contents


def test_tasks_removes_env_file_when_filesystem() -> None:
    """If a consumer flips back from s3 → filesystem, the stale env file
    must be cleaned up so it can't be tailed for archival creds."""
    contents = _ZOT_TASKS.read_text()
    assert "Remove Zot AWS credentials env file" in contents
    assert "state: absent" in contents


# ── build_specs.yml _zot_spec env_file gate ─────────────────────────


def test_build_specs_zot_spec_gates_env_file_on_s3() -> None:
    """GH bay#14: `_zot_spec` shipped without env_file in an earlier
    version, so S3 deployments started with no AWS_* and restart-looped
    with `s3aws: NoCredentialProviders`. The zot spec must gate env_file on
    `zot_storage_driver == 's3'` exactly like the legacy `_zot.j2`
    compose template — unconditional doesn't work because the zot role
    removes the env file on filesystem mode and the reconciler slurps every
    spec's env_file into the bundle."""
    src = _BUILD_SPECS.read_text()
    zot_start = src.find("Build zot registry container spec")
    next_block = src.find("- name:", zot_start + 1)
    # Find the *following* task after the zot block — we want the slice
    # between the zot task header and the next sibling task.
    end = src.find("- name:", next_block + 1) if next_block != -1 else len(src)
    zot_block = src[zot_start:end]
    assert "stack_dir ~ '/env/zot.env'" in zot_block, (
        "_zot_spec must reference zot.env so S3 deployments receive AWS_* "
        "via env_file — see GH bay#14."
    )
    assert "zot_storage_driver" in zot_block, (
        "env_file must be gated on zot_storage_driver — filesystem mode "
        "removes the file and the reconciler's bundle slurp would fail."
    )
    assert "'s3'" in zot_block
