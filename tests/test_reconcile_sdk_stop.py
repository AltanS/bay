"""SdkDockerClient.stop is safe on a container that is already gone.

The executor now stops a container before it removes it, and the canary
fallback can reach that stop after the old container was already stopped and
removed. ``remove`` has always swallowed NotFound; ``stop`` must too, or the
fallback aborts before it recreates the service.
"""

from __future__ import annotations

from typing import Any

import docker
import pytest

from bay_reconcile.sdk_client import SdkDockerClient


class _Container:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def stop(self, **kwargs: Any) -> None:
        self.calls.append(("stop", kwargs))

    def remove(self, **kwargs: Any) -> None:
        self.calls.append(("remove", kwargs))


class _Containers:
    def __init__(self, store: dict[str, _Container]) -> None:
        self._store = store

    def get(self, name: str) -> _Container:
        if name not in self._store:
            raise docker.errors.NotFound(f"No such container: {name}")
        return self._store[name]


class _Daemon:
    def __init__(self, store: dict[str, _Container]) -> None:
        self.containers = _Containers(store)


def _client(store: dict[str, _Container]) -> SdkDockerClient:
    """An SdkDockerClient wired to a stub daemon, with no docker.from_env()."""
    client = SdkDockerClient.__new__(SdkDockerClient)
    client._c = _Daemon(store)  # type: ignore[attr-defined]
    return client


def test_stop_on_missing_container_does_not_raise() -> None:
    _client({}).stop("gone", timeout=30)


def test_remove_on_missing_container_does_not_raise() -> None:
    _client({}).remove("gone")


def test_stop_passes_the_timeout_through() -> None:
    ctr = _Container()
    _client({"db": ctr}).stop("db", timeout=30)
    assert ctr.calls == [("stop", {"timeout": 30})]


def test_stop_does_not_swallow_other_daemon_errors() -> None:
    class _Broken(_Containers):
        def get(self, name: str) -> _Container:
            raise docker.errors.APIError("daemon unavailable")

    client = _client({})
    client._c.containers = _Broken({})  # type: ignore[attr-defined]
    with pytest.raises(docker.errors.APIError):
        client.stop("db", timeout=30)
