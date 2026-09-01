"""A container spec's `user` must survive bundle → docker create.

The webhook receiver is started as "<uid>:<gid>" so that it shares the host's
build-control group. That is the whole reason `{{ stack_dir }}/triggers` and
`/state` could drop from 0777 to 2770 — if `user` is dropped anywhere between
build_specs.yml and the docker daemon, the container runs as its image default,
cannot write the 2770 directories, and builds stop firing.

`entrypoint` was silently dropped exactly this way once already (GH bay#26), so
the whole path is driven here: bundle parse, spec default, and the kwargs the
SDK client hands to `containers.run`.
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
    client = SdkDockerClient.__new__(SdkDockerClient)
    fake = _CapturingClient()
    client._c = fake
    client._managed_label = "bay.managed"
    client._stack_label = "bay.stack"
    client._stack = "test"
    return client, fake


def _spec(**over: Any) -> ContainerSpec:
    base: dict[str, Any] = {
        "name": "bay-webhook",
        "image": "bay-webhook:latest",
        "type": "infra",
        "config_hash": "deadbeef",
    }
    base.update(over)
    return ContainerSpec(**base)


def test_bundle_parses_user():
    spec = spec_from_dict({
        "name": "bay-webhook",
        "image": "bay-webhook:latest",
        "type": "infra",
        "config_hash": "h",
        "user": "10001:2000",
    })
    assert spec.user == "10001:2000"


def test_user_defaults_to_none():
    assert spec_from_dict({
        "name": "x", "image": "i", "type": "infra", "config_hash": "h",
    }).user is None


def test_create_forwards_user():
    client, fake = _client()
    client.create(_spec(user="10001:2000"))
    assert fake.containers.kwargs["user"] == "10001:2000"


def test_no_user_key_when_unset():
    """Passing user=None would be handed to the daemon as an explicit blank."""
    client, fake = _client()
    client.create(_spec())
    assert "user" not in fake.containers.kwargs
