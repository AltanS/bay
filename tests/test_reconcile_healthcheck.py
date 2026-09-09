"""A healthcheck duration must reach the daemon as integer nanoseconds.

On 2026-09-10 the reconciler decided to Recreate `postgres` on the eu host.
The remove succeeded; the create failed with the daemon answering "cannot
unmarshal string into Go struct field
HealthcheckConfig.Config.Healthcheck.Interval of type time.Duration", because
`create()` forwarded the bundle's `interval: "5s"` unchanged and the docker
SDK's Healthcheck wants nanoseconds. Postgres was absent for about ten minutes
and every service on the host that uses it went down. Postgres was the only
shipped service with a healthcheck, which is why no earlier deploy hit this.

These drive the real `_duration_ns`, `_healthcheck_to_sdk` and
`SdkDockerClient.create`, capturing the create kwargs instead of talking to a
daemon.
"""

from __future__ import annotations

from typing import Any

import pytest

from bay_reconcile.models import ContainerSpec
from bay_reconcile.sdk_client import (
    SdkDockerClient,
    _duration_ns,
    _healthcheck_to_sdk,
)


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
    assert _duration_ns(value) == expected


@pytest.mark.parametrize("value", ["5 seconds", "abc", "", "5", "5x", "s5", "5s junk"])
def test_duration_ns_rejects_unparseable(value: str) -> None:
    with pytest.raises(ValueError):
        _duration_ns(value)


def test_bad_duration_names_the_key_and_the_value() -> None:
    """A bad inventory must fail readably at plan time, not as a daemon 400."""
    with pytest.raises(ValueError) as excinfo:
        _healthcheck_to_sdk({"interval": "5 seconds"})
    message = str(excinfo.value)
    assert "interval" in message
    assert "5 seconds" in message


def test_healthcheck_to_sdk_leaves_test_and_retries_alone() -> None:
    out = _healthcheck_to_sdk(
        {"test": ["CMD", "pg_isready", "-U", "app"], "retries": 5, "interval": "5s"}
    )
    assert out["test"] == ["CMD", "pg_isready", "-U", "app"]
    assert out["retries"] == 5
    assert out["interval"] == 5_000_000_000


def test_healthcheck_to_sdk_converts_every_duration_key() -> None:
    out = _healthcheck_to_sdk({"interval": "5s", "timeout": "3s", "start_period": "10s"})
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
