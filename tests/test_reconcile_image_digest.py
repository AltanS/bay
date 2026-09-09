"""The SDK client's local image-id lookup, the seam the planner's rule needs.

``observe`` reads containers. The id a reference resolves to lives in the image
store, not on the container, so the client looks it up per distinct reference
and hands it to ``parse_state``. A rebuilt ``bay-webhook:latest`` shipped once
because nothing performed this lookup.
"""

from __future__ import annotations

from typing import Any

import docker

from bay_reconcile.sdk_client import SdkDockerClient

OLD = "sha256:1111111111111111111111111111111111111111111111111111111111111111"
NEW = "sha256:2222222222222222222222222222222222222222222222222222222222222222"


class _Image:
    def __init__(self, image_id: str) -> None:
        self.id = image_id


class _Container:
    def __init__(self, name: str, reference: str, image_id: str) -> None:
        self.attrs: dict[str, Any] = {
            "Name": f"/{name}",
            "Image": image_id,
            "Config": {"Image": reference, "Labels": {"bay.managed": "true"}},
            "State": {"Status": "running"},
        }


class _Images:
    """Stand-in for ``client.images``; counts lookups so the cache is testable."""

    def __init__(self, store: dict[str, str], *, error: Exception | None = None) -> None:
        self.store = store
        self.error = error
        self.calls: list[str] = []

    def get(self, reference: str) -> _Image:
        self.calls.append(reference)
        if self.error is not None:
            raise self.error
        if reference not in self.store:
            raise docker.errors.ImageNotFound(reference)
        return _Image(self.store[reference])


class _Containers:
    def __init__(self, containers: list[_Container]) -> None:
        self._containers = containers

    def list(self, all: bool = False) -> list[_Container]:  # noqa: A002 - docker SDK name
        return list(self._containers)


class _Daemon:
    def __init__(self, containers: list[_Container], images: _Images) -> None:
        self.containers = _Containers(containers)
        self.images = images


def _client(containers: list[_Container], images: _Images) -> SdkDockerClient:
    """An SdkDockerClient wired to a stub daemon, with no docker.from_env()."""
    client = SdkDockerClient.__new__(SdkDockerClient)
    client._c = _Daemon(containers, images)  # type: ignore[attr-defined]
    client._managed_label = "bay.managed"  # type: ignore[attr-defined]
    client._stack_label = "bay.stack"  # type: ignore[attr-defined]
    client._stack = "bay"  # type: ignore[attr-defined]
    return client


def test_observe_reports_the_rebuilt_local_image_digest() -> None:
    images = _Images({"bay-webhook:latest": NEW})
    client = _client([_Container("webhook", "bay-webhook:latest", OLD)], images)
    state = client.observe("bay.managed")["webhook"]
    assert state.image_id == OLD
    assert state.local_image_id == NEW
    assert state.image_drifted


def test_observe_reports_no_drift_when_the_store_matches() -> None:
    images = _Images({"bay-webhook:latest": OLD})
    client = _client([_Container("webhook", "bay-webhook:latest", OLD)], images)
    assert not client.observe("bay.managed")["webhook"].image_drifted


def test_absent_image_yields_unknown_not_a_crash() -> None:
    images = _Images({})
    client = _client([_Container("webhook", "bay-webhook:latest", OLD)], images)
    state = client.observe("bay.managed")["webhook"]
    assert state.local_image_id is None
    assert not state.image_drifted


def test_daemon_error_yields_unknown_not_a_crash() -> None:
    images = _Images({}, error=docker.errors.APIError("daemon said no"))
    client = _client([_Container("webhook", "bay-webhook:latest", OLD)], images)
    assert client.observe("bay.managed")["webhook"].local_image_id is None


def test_one_lookup_per_distinct_reference() -> None:
    images = _Images({"bay-webhook:latest": NEW, "api:1.0": OLD})
    containers = [
        _Container("webhook", "bay-webhook:latest", OLD),
        _Container("webhook-two", "bay-webhook:latest", OLD),
        _Container("api", "api:1.0", OLD),
    ]
    client = _client(containers, images)
    client.observe("bay.managed")
    assert sorted(images.calls) == ["api:1.0", "bay-webhook:latest"]
