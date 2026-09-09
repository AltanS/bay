"""A healthcheck duration must reach the daemon as integer nanoseconds.

On 2026-09-10 the reconciler decided to Recreate `postgres` on the eu host.
The remove succeeded; the create failed with the daemon answering "cannot
unmarshal string into Go struct field
HealthcheckConfig.Config.Healthcheck.Interval of type time.Duration", because
`create()` forwarded the bundle's `interval: "5s"` unchanged and the docker
SDK's Healthcheck wants nanoseconds. Postgres was absent for about ten minutes
and every service on the host that uses it went down. Postgres was the only
shipped service with a healthcheck, which is why no earlier deploy hit this.

The first fix converted at create time. That was too late: a Recreate removes
the old container before it creates the new one, so a ValueError in `create`
costs the same outage the daemon's 400 cost. The conversion now runs in
`bundle.spec_from_dict`, before the fleet is observed and before any action is
planned, and `main` turns a bad bundle into a non-zero exit with no docker call
at all.

These drive the real `duration_ns`, `healthcheck_to_sdk`, `spec_from_dict`,
`SdkDockerClient.create` and `main`, capturing kwargs instead of talking to a
daemon.
"""

from __future__ import annotations

from typing import Any

import pytest

from bay_reconcile.__main__ import main
from bay_reconcile.bundle import load_bundle, spec_from_dict
from bay_reconcile.models import (
    ContainerSpec,
    ContainerState,
    duration_ns,
    healthcheck_to_sdk,
)
from bay_reconcile.sdk_client import SdkDockerClient


class _CapturingContainers:
    def __init__(self) -> None:
        self.kwargs: dict[str, Any] = {}

    def run(self, **kwargs: Any) -> None:
        self.kwargs = kwargs


class _CapturingClient:
    def __init__(self) -> None:
        self.containers = _CapturingContainers()


def _client() -> tuple[SdkDockerClient, _CapturingClient]:
    """An SdkDockerClient whose docker handle is captured, not connected."""
    client = SdkDockerClient.__new__(SdkDockerClient)
    fake = _CapturingClient()
    client._c = fake
    client._managed_label = "bay.managed"
    client._stack_label = "bay.stack"
    client._stack = "test"
    return client, fake


# ── duration parsing ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("5s", 5_000_000_000),
        ("30s", 30_000_000_000),
        ("1m30s", 90_000_000_000),
        ("500ms", 500_000_000),
        ("1.5s", 1_500_000_000),
        ("1h", 3_600_000_000_000),
        ("2us", 2_000),
        ("10ns", 10),
        (5_000_000_000, 5_000_000_000),
        (2.0, 2),
    ],
)
def test_duration_ns_parses(value: object, expected: int) -> None:
    assert duration_ns(value) == expected


@pytest.mark.parametrize("value", ["5 seconds", "abc", "", "5", "5x", "s5", "5s junk"])
def test_duration_ns_rejects_unparseable(value: str) -> None:
    with pytest.raises(ValueError):
        duration_ns(value)


def test_bad_duration_names_the_key_and_the_value() -> None:
    """A bad inventory must fail readably at plan time, not as a daemon 400."""
    with pytest.raises(ValueError) as excinfo:
        healthcheck_to_sdk({"interval": "5 seconds"})
    message = str(excinfo.value)
    assert "interval" in message
    assert "5 seconds" in message


def test_healthcheck_to_sdk_leaves_test_and_retries_alone() -> None:
    out = healthcheck_to_sdk(
        {"test": ["CMD", "pg_isready", "-U", "app"], "retries": 5, "interval": "5s"}
    )
    assert out["test"] == ["CMD", "pg_isready", "-U", "app"]
    assert out["retries"] == 5
    assert out["interval"] == 5_000_000_000


def test_healthcheck_to_sdk_converts_every_duration_key() -> None:
    out = healthcheck_to_sdk({"interval": "5s", "timeout": "3s", "start_period": "10s"})
    assert out == {
        "interval": 5_000_000_000,
        "timeout": 3_000_000_000,
        "start_period": 10_000_000_000,
    }


# ── spec -> docker create kwargs ─────────────────────────────────────────


def test_create_hands_the_daemon_nanoseconds() -> None:
    """The exact postgres block that took the eu host down."""
    client, fake = _client()
    spec = ContainerSpec(
        name="postgres",
        image="pgvector/pgvector:pg17",
        type="accessory",
        config_hash="deadbeef",
        healthcheck={"interval": "5s", "retries": 5, "test": ["CMD", "pg_isready", "-U", "app"]},
    )
    client.create(spec)
    hc = fake.containers.kwargs["healthcheck"]
    assert hc["interval"] == 5_000_000_000
    assert hc["retries"] == 5
    assert not isinstance(hc["interval"], str)


def test_create_omits_healthcheck_when_unset() -> None:
    """Control: without a healthcheck block nothing is added to the kwargs."""
    client, fake = _client()
    client.create(
        ContainerSpec(name="demo", image="alpine:3.20", type="accessory", config_hash="abc")
    )
    assert "healthcheck" not in fake.containers.kwargs


# ── bundle load time: the only place a bad duration is free ──────────────


def test_bundle_converts_durations_at_load() -> None:
    spec = spec_from_dict(
        {
            "name": "postgres",
            "image": "pgvector/pgvector:pg17",
            "type": "accessory",
            "config_hash": "abc",
            "healthcheck": {"interval": "5s", "retries": 5, "test": ["CMD", "pg_isready"]},
        }
    )
    assert spec.healthcheck is not None
    assert spec.healthcheck["interval"] == 5_000_000_000


def test_bundle_names_the_service_the_key_and_the_value() -> None:
    with pytest.raises(ValueError) as excinfo:
        spec_from_dict(
            {
                "name": "postgres",
                "image": "pgvector/pgvector:pg17",
                "type": "accessory",
                "config_hash": "abc",
                "healthcheck": {"interval": "5x"},
            }
        )
    message = str(excinfo.value)
    assert "postgres" in message
    assert "interval" in message
    assert "5x" in message


def test_bundle_leaves_a_spec_without_a_healthcheck_alone() -> None:
    """Control: the loader must not invent a healthcheck for the other services."""
    bundle = load_bundle(
        {
            "containers": [
                {"name": "web", "image": "nginx:1", "type": "service", "config_hash": "abc"}
            ]
        }
    )
    assert bundle.containers[0].healthcheck is None


# ── end to end: a bad bundle must not reach the daemon ────────────────────


class _RecordingClient:
    """A DockerClient that records calls instead of making them."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def observe(self, managed_label: str) -> dict[str, ContainerState]:
        """A postgres already running on a stale hash, as the eu host was.

        This is what makes the plan a Recreate rather than a Create, and a
        Recreate is the action that removes the live container before it
        creates the replacement. A fake reporting an empty host would let a
        broken bundle look harmless.
        """
        self.calls.append("observe")
        return {
            "postgres": ContainerState(
                name="postgres",
                exists=True,
                image="pgvector/pgvector:pg17",
                config_hash="stale",
                status="running",
                managed=True,
                image_id="sha256:aaa",
                local_image_id="sha256:aaa",
            )
        }

    def create(self, spec: ContainerSpec, *, name_override: str | None = None) -> None:
        self.calls.append(f"create:{spec.name}")

    def remove(self, name: str) -> None:
        self.calls.append(f"remove:{name}")

    def stop(self, name: str, *, timeout: int = 10) -> None:
        self.calls.append(f"stop:{name}")

    def rename(self, old: str, new: str) -> None:
        self.calls.append(f"rename:{old}")

    def inspect(self, name: str) -> Any:
        self.calls.append(f"inspect:{name}")
        raise AssertionError("not reached in these tests")

    def pull(self, image: str) -> None:
        self.calls.append(f"pull:{image}")


def _bundle_file(tmp_path: Any, interval: str) -> str:
    """The postgres bundle entry that took the eu host down, one field varied."""
    import json

    payload = {
        "stack": "bay",
        "managed_label": "bay.managed",
        "containers": [
            {
                "name": "postgres",
                "image": "pgvector/pgvector:pg17",
                "type": "accessory",
                "config_hash": "deadbeef",
                "healthcheck": {
                    "interval": interval,
                    "retries": 5,
                    "test": ["CMD", "pg_isready", "-U", "app"],
                },
            }
        ],
    }
    path = tmp_path / "bundle.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


def _patched_main(monkeypatch: Any, path: str) -> tuple[int, _RecordingClient]:
    import bay_reconcile.sdk_client as sdk

    recorder = _RecordingClient()
    monkeypatch.setattr(sdk, "SdkDockerClient", lambda **kwargs: recorder)
    return main([path]), recorder


def test_bad_duration_exits_non_zero_without_touching_docker(
    monkeypatch: Any, tmp_path: Any, capsys: Any
) -> None:
    """The whole run must fail before the first remove."""
    code, recorder = _patched_main(monkeypatch, _bundle_file(tmp_path, "5x"))
    assert [c for c in recorder.calls if c.startswith("remove")] == [], (
        f"a bad bundle removed a live container: {recorder.calls}"
    )
    assert [c for c in recorder.calls if c.startswith("create")] == []
    assert recorder.calls == [], f"docker was touched at all: {recorder.calls}"
    assert code != 0
    out = capsys.readouterr().out
    assert "postgres" in out and "interval" in out and "5x" in out


def test_the_same_bundle_with_a_good_duration_plans_and_creates(
    monkeypatch: Any, tmp_path: Any
) -> None:
    """Control: only the duration differs, and this one runs to a create."""
    code, recorder = _patched_main(monkeypatch, _bundle_file(tmp_path, "5s"))
    assert code == 0
    assert "observe" in recorder.calls
    # A stale hash on a running container plans a Recreate, so this path DOES
    # remove and re-create. That is exactly why the bad bundle above has to be
    # stopped before the plan is executed.
    assert "remove:postgres" in recorder.calls
    assert "create:postgres" in recorder.calls


# ── the hash must not move, or this release recreates postgres again ───────


def _spec_hash(spec: dict[str, Any]) -> str:
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).parent.parent / "filter_plugins"))
    from bay_filters import bay_spec_hash

    return str(bay_spec_hash(spec))


_POSTGRES_INVENTORY_SPEC: dict[str, Any] = {
    "name": "postgres",
    "type": "accessory",
    "image": "pgvector/pgvector:pg17",
    "healthcheck": {"interval": "5s", "retries": 5, "test": ["CMD", "pg_isready", "-U", "app"]},
}

# Frozen on v0.6.5, before the conversion moved to load time. If this moves,
# every service with a healthcheck recreates on the next deploy. The name is
# the leak scanner's context allowlist for a 64-char hex literal in a test;
# see HEX_CONTEXT_ALLOW in scripts/leak-scan.sh.
_FROZEN_SPEC_HASH = "0560310336f6620e139bb4936627e1dd53d4f67084a36b6656e64cca9b4301d4"


def test_inventory_hash_of_a_compose_duration_is_unchanged() -> None:
    assert _spec_hash(_POSTGRES_INVENTORY_SPEC) == _FROZEN_SPEC_HASH


def test_hashing_the_converted_values_would_have_moved_the_hash() -> None:
    """Control: this is the recreate this release had to avoid.

    It proves the freeze above is load-bearing rather than trivially true. The
    hash is computed by `bay_spec_hash` over the RAW inventory spec on the
    Ansible side, before the bundle is written, so `spec_from_dict` converting
    a duration on the server cannot reach it.
    """
    converted = dict(_POSTGRES_INVENTORY_SPEC)
    converted["healthcheck"] = healthcheck_to_sdk(_POSTGRES_INVENTORY_SPEC["healthcheck"])
    assert _spec_hash(converted) != _FROZEN_SPEC_HASH


def test_spec_from_dict_passes_the_bundle_hash_through_verbatim() -> None:
    """The converted healthcheck must not leak into the spec's config_hash."""
    spec = spec_from_dict(
        {
            "name": "postgres",
            "image": "pgvector/pgvector:pg17",
            "type": "accessory",
            "config_hash": _FROZEN_SPEC_HASH,
            "healthcheck": _POSTGRES_INVENTORY_SPEC["healthcheck"],
        }
    )
    assert spec.config_hash == _FROZEN_SPEC_HASH
    assert spec.healthcheck is not None
    assert spec.healthcheck["interval"] == 5_000_000_000
