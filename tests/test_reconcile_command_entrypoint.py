"""`command`/`entrypoint` must survive bundle → docker create (GH bay#26).

`build_specs.yml` emitting the keys is only half the path: the bundle loader
has to parse them into the ContainerSpec and the SDK client has to forward
them to `containers.run`. `entrypoint` had no ContainerSpec field at all, so
it was dropped between the CLI and the daemon even once emitted.

These tests drive the real `spec_from_dict` and `SdkDockerClient.create`,
capturing the kwargs instead of talking to a daemon.
"""

from __future__ import annotations

from typing import Any

from bay_reconcile.bundle import spec_from_dict
from bay_reconcile.models import ContainerSpec
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


def _spec(**over: Any) -> ContainerSpec:
    base: dict[str, Any] = {
        "name": "demo",
        "image": "alpine:3.20",
        "type": "accessory",
        "config_hash": "deadbeef",
    }
    base.update(over)
    return ContainerSpec(**base)


# ── bundle: the CLI → server contract ────────────────────────────────────


def test_bundle_parses_command_and_entrypoint():
    spec = spec_from_dict(
        {
            "name": "demo-backup",
            "image": "alpine:3.20",
            "type": "accessory",
            "config_hash": "abc",
            "command": ["sleep", "infinity"],
            "entrypoint": ["/bin/sh", "-c"],
        }
    )
    assert spec.command == ["sleep", "infinity"]
    assert spec.entrypoint == ["/bin/sh", "-c"]


def test_bundle_defaults_both_to_none():
    spec = spec_from_dict(
        {"name": "db", "image": "postgres:16", "type": "accessory", "config_hash": "abc"}
    )
    assert spec.command is None
    assert spec.entrypoint is None


# ── sdk: spec → docker create kwargs ─────────────────────────────────────


def test_create_forwards_command_and_entrypoint():
    client, fake = _client()
    client.create(_spec(command=["sleep", "infinity"], entrypoint=["/bin/sh", "-c"]))
    assert fake.containers.kwargs["command"] == ["sleep", "infinity"]
    assert fake.containers.kwargs["entrypoint"] == ["/bin/sh", "-c"]


def test_create_omits_both_when_unset():
    """Passing entrypoint=None would blank the image's ENTRYPOINT, not inherit it."""
    client, fake = _client()
    client.create(_spec())
    assert "command" not in fake.containers.kwargs
    assert "entrypoint" not in fake.containers.kwargs
