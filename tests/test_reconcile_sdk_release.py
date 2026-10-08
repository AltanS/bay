"""SdkDockerClient.run_release leaves a running release container alone.

A webhook build may be running the release migration when `bay up` starts the
next one. The leftover cleanup must not force-remove that container: it removes
only a finished one that carries this service's label, and fails the release
for anything else.
"""

from __future__ import annotations

from typing import Any

import docker
import pytest

from bay_reconcile.models import RELEASE_LABEL, ContainerSpec
from bay_reconcile.sdk_client import SdkDockerClient


class _Container:
    def __init__(self, *, status: str = "exited", labels: dict[str, str] | None = None,
                 exit_code: int = 0, remove_error: Exception | None = None) -> None:
        self.remove_error = remove_error
        self.status = status
        self.labels = labels or {}
        self.exit_code = exit_code
        self.removed: list[bool] = []

    def wait(self, timeout: float | None = None) -> dict[str, int]:
        return {"StatusCode": self.exit_code}

    def logs(self, tail: int = 20) -> bytes:
        return b"migrated"

    def remove(self, force: bool = False) -> None:
        self.removed.append(force)
        if self.remove_error is not None:
            raise self.remove_error


class _Containers:
    def __init__(self, existing: dict[str, _Container]) -> None:
        self.existing = existing
        self.started: list[dict[str, Any]] = []
        self.new = _Container()

    def get(self, name: str) -> _Container:
        if name not in self.existing:
            raise docker.errors.NotFound(f"No such container: {name}")
        return self.existing[name]

    def run(self, **kwargs: Any) -> _Container:
        self.started.append(kwargs)
        return self.new


class _Daemon:
    def __init__(self, existing: dict[str, _Container]) -> None:
        self.containers = _Containers(existing)


def _client(existing: dict[str, _Container]) -> tuple[SdkDockerClient, _Daemon]:
    client = SdkDockerClient.__new__(SdkDockerClient)
    daemon = _Daemon(existing)
    client._c = daemon  # type: ignore[attr-defined]
    return client, daemon


def _spec() -> ContainerSpec:
    return ContainerSpec(
        name="web", image="web:latest", type="service", config_hash="h", release="bin/migrate"
    )


def test_run_release_without_a_leftover_runs_and_cleans_up() -> None:
    client, daemon = _client({})
    assert client.run_release(_spec(), timeout=30) == (0, "migrated")
    assert daemon.containers.started[0]["name"] == "web-release"
    assert daemon.containers.new.removed == [True]


@pytest.mark.parametrize("status", ["created", "exited", "dead", "removing"])
def test_a_finished_leftover_is_removed(status: str) -> None:
    old = _Container(status=status, labels={RELEASE_LABEL: "web"})
    client, daemon = _client({"web-release": old})
    assert client.run_release(_spec(), timeout=30)[0] == 0
    assert old.removed == [True]
    assert len(daemon.containers.started) == 1


@pytest.mark.parametrize("status", ["running", "restarting", "paused"])
def test_a_running_leftover_fails_the_release_and_survives(status: str) -> None:
    old = _Container(status=status, labels={RELEASE_LABEL: "web"})
    client, daemon = _client({"web-release": old})
    with pytest.raises(RuntimeError, match="web-release") as err:
        client.run_release(_spec(), timeout=30)
    assert status in str(err.value)
    assert old.removed == []
    assert daemon.containers.started == []


def test_a_container_of_another_owner_is_never_removed() -> None:
    other = _Container(status="exited", labels={RELEASE_LABEL: "api"})
    client, daemon = _client({"web-release": other})
    with pytest.raises(RuntimeError, match="not the release container of web"):
        client.run_release(_spec(), timeout=30)
    assert other.removed == [] and daemon.containers.started == []


def test_a_leftover_gone_before_the_remove_is_fine() -> None:
    old = _Container(status="exited", labels={RELEASE_LABEL: "web"},
                     remove_error=docker.errors.NotFound("gone"))
    client, daemon = _client({"web-release": old})
    assert client.run_release(_spec(), timeout=30)[0] == 0
    assert len(daemon.containers.started) == 1


def _conflict() -> docker.errors.APIError:
    response = type("R", (), {"status_code": 409, "reason": "Conflict", "content": b"",
                              "json": lambda self: {}})()
    return docker.errors.APIError("removal in progress", response=response)


def test_a_leftover_already_being_removed_is_tolerated() -> None:
    old = _Container(status="removing", labels={RELEASE_LABEL: "web"}, remove_error=_conflict())
    client, daemon = _client({"web-release": old})
    assert client.run_release(_spec(), timeout=30)[0] == 0
    assert len(daemon.containers.started) == 1


def test_a_conflict_on_a_non_removing_leftover_still_raises() -> None:
    old = _Container(status="exited", labels={RELEASE_LABEL: "web"}, remove_error=_conflict())
    client, _ = _client({"web-release": old})
    with pytest.raises(docker.errors.APIError):
        client.run_release(_spec(), timeout=30)
