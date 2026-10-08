"""SDK-backed DockerClient: the only module that imports ``docker``.

Runs on the target host and turns a ContainerSpec into docker API calls. The
executor's logic is unit-tested against a FakeDockerClient; this thin adapter
is validated end-to-end on sandbox (S7). Image pulls are present-aware so a
no-op never contacts the registry.
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import docker

from .models import (
    RELEASE_LABEL,
    RELEASE_SUFFIX,
    ContainerSpec,
    ContainerState,
    healthcheck_to_sdk,
    release_command_kwargs,
)
from .observe import (
    HASH_LABEL as _HASH_LABEL,
)
from .observe import (
    MANAGED_LABEL,
    STACK_LABEL,
    parse_state,
)

#: Container states in which nothing runs, so a leftover release container is safe to remove.
#: ``removing`` is Docker already deleting it; the remove call below tolerates it being gone.
_FINISHED = frozenset({"created", "exited", "dead", "removing"})


def _ctr_port_key(ctr: str) -> str:
    """Container-port key for the docker SDK ``ports`` mapping.

    Preserve an explicit protocol suffix (e.g. ``3478/udp``); default bare
    ports to ``/tcp``. Without this, ``3478/udp`` became ``3478/udp/tcp`` and
    Docker rejected it with "unknown protocol".
    """
    return ctr if "/" in ctr else f"{ctr}/tcp"


def _ports_to_sdk(ports: Sequence[str]) -> dict[str, object]:
    """'<ip>:<host>:<ctr>[/proto]' / '<host>:<ctr>[/proto]' -> docker SDK ``ports`` mapping."""
    out: dict[str, object] = {}
    for raw in ports:
        parts = str(raw).split(":")
        if len(parts) == 3:
            ip, host, ctr = parts
            out[_ctr_port_key(ctr)] = (ip, host)
        elif len(parts) == 2:
            host, ctr = parts
            out[_ctr_port_key(ctr)] = host
    return out


class SdkDockerClient:
    """Concrete DockerClient backed by ``docker.from_env()``."""

    def __init__(
        self,
        *,
        managed_label: str = MANAGED_LABEL,
        stack_label: str = STACK_LABEL,
        stack: str = "bay",
    ) -> None:
        self._c: Any = docker.from_env()
        self._managed_label = managed_label
        self._stack_label = stack_label
        self._stack = stack

    def observe(self, managed_label: str) -> dict[str, ContainerState]:
        out: dict[str, ContainerState] = {}
        local_ids: dict[str, str | None] = {}
        for ctr in self._c.containers.list(all=True):
            reference = (ctr.attrs.get("Config") or {}).get("Image")
            state = parse_state(
                ctr.attrs,
                managed_label=managed_label,
                hash_label=_HASH_LABEL,
                local_image_id=self._local_image_id(reference, local_ids),
            )
            out[state.name] = state
        return out

    def _local_image_id(self, reference: object, cache: dict[str, str | None]) -> str | None:
        """The id ``reference`` resolves to in the LOCAL image store, or None.

        One lookup per distinct reference per observe pass, cached, because a
        fleet repeats the same image across containers. None means the daemon
        has no such image, which the planner reads as no evidence of change
        rather than as a reason to redeploy.
        """
        ref = str(reference or "").strip()
        if not ref:
            return None
        if ref not in cache:
            cache[ref] = self._image_id(ref)
        return cache[ref]

    def image_id(self, reference: str) -> str | None:
        """The local image id of ``reference``, or None (read by the receipt state)."""
        return self._image_id(reference)

    def image_tags(self, reference: str) -> list[str]:
        """The repo tags of the image ``reference`` names (an id works), or ``[]``.

        Read by the receipt state: a container with no commit label takes its
        commit from the image's single commit tag.
        """
        try:
            image = self._c.images.get(reference)
        except (docker.errors.ImageNotFound, docker.errors.APIError):
            return []
        return [str(t) for t in (getattr(image, "tags", None) or [])]

    def _image_id(self, reference: str) -> str | None:
        try:
            image = self._c.images.get(reference)
        except (docker.errors.ImageNotFound, docker.errors.APIError):
            # Never pulled, or the daemon could not answer. Both are "unknown".
            return None
        return str(getattr(image, "id", "") or "") or None

    def create(self, spec: ContainerSpec, *, name_override: str | None = None) -> None:
        labels = dict(spec.labels)
        labels[_HASH_LABEL] = spec.config_hash
        labels[self._managed_label] = "true"
        labels[self._stack_label] = self._stack
        kwargs: dict[str, Any] = {
            "name": name_override or spec.name,
            "image": spec.image,
            "detach": True,
            "restart_policy": {"Name": spec.restart_policy},
            "environment": dict(spec.env),
            "labels": labels,
        }
        if spec.network_mode:
            kwargs["network_mode"] = spec.network_mode
        elif spec.networks:
            kwargs["network"] = spec.networks[0]
        if spec.volumes:
            kwargs["volumes"] = list(spec.volumes)
        if spec.command is not None:
            kwargs["command"] = spec.command
        if spec.entrypoint is not None:
            kwargs["entrypoint"] = spec.entrypoint
        if spec.user:
            kwargs["user"] = spec.user
        if spec.mem_limit:
            kwargs["mem_limit"] = spec.mem_limit
        if spec.memswap_limit:
            kwargs["memswap_limit"] = spec.memswap_limit
        if spec.ports:
            kwargs["ports"] = _ports_to_sdk(spec.ports)
        if spec.healthcheck:
            kwargs["healthcheck"] = healthcheck_to_sdk(spec.healthcheck)
        if spec.log_driver:
            kwargs["log_config"] = {"type": spec.log_driver, "config": dict(spec.log_options or {})}
        self._c.containers.run(**kwargs)

    def run_release(self, spec: ContainerSpec, *, timeout: float) -> tuple[int | None, str]:
        name = f"{spec.name}{RELEASE_SUFFIX}"
        self._remove_release(name, spec.name)
        kwargs: dict[str, Any] = {
            "name": name,
            "image": spec.image,
            "detach": True,
            "environment": dict(spec.env),
            "labels": {RELEASE_LABEL: spec.name},
        }
        kwargs.update(release_command_kwargs(spec.release))
        if spec.network_mode:
            kwargs["network_mode"] = spec.network_mode
        elif spec.networks:
            kwargs["network"] = spec.networks[0]
        if spec.volumes:
            kwargs["volumes"] = list(spec.volumes)
        if spec.user:
            kwargs["user"] = spec.user
        ctr = self._c.containers.run(**kwargs)
        try:
            try:
                result = ctr.wait(timeout=timeout)
            except Exception:  # noqa: BLE001 - the SDK raises the transport's read timeout
                return None, self._tail(ctr)
            return int((result or {}).get("StatusCode", 1)), self._tail(ctr)
        finally:
            try:
                ctr.remove(force=True)
            except docker.errors.NotFound:
                pass

    @staticmethod
    def _tail(ctr: Any) -> str:
        try:
            raw = ctr.logs(tail=20)
        except Exception:  # noqa: BLE001 - the logs only explain a failure
            return ""
        return raw.decode("utf-8", "replace").strip() if isinstance(raw, bytes) else str(raw)

    def _remove_release(self, name: str, of: str) -> None:
        """Remove a finished release container a crashed run left.

        Never a container of another owner, and never one that still runs: a
        webhook build may be running its migration right now, and a force
        remove would kill it. A running one fails this release instead.
        """
        try:
            ctr = self._c.containers.get(name)
        except docker.errors.NotFound:
            return
        if (ctr.labels or {}).get(RELEASE_LABEL) != of:
            raise RuntimeError(
                f"a container named {name} exists and is not the release container of {of}"
            )
        status = getattr(ctr, "status", None)
        if status not in _FINISHED:
            raise RuntimeError(
                f"the release container {name} is still {status or 'running'}, so another "
                f"release of {of} may be running; wait for it to end, or remove it with "
                f"`docker rm -f {name}` if it is stuck"
            )
        try:
            ctr.remove(force=True)
        except docker.errors.NotFound:
            pass
        except docker.errors.APIError as err:
            # Docker answers 409 when a removal is already in progress: the
            # container is going away, which is what this call wanted.
            if status != "removing" or getattr(err, "status_code", None) != 409:
                raise

    def stop(self, name: str, *, timeout: int = 10) -> None:
        # Absent is as stopped as it gets; an exited container answers 304,
        # which the SDK does not raise on.
        try:
            self._c.containers.get(name).stop(timeout=timeout)
        except docker.errors.NotFound:
            pass

    def remove(self, name: str) -> None:
        try:
            self._c.containers.get(name).remove(force=True)
        except docker.errors.NotFound:
            pass

    def rename(self, old: str, new: str) -> None:
        self._c.containers.get(old).rename(new)

    def inspect(self, name: str) -> ContainerState:
        try:
            ctr = self._c.containers.get(name)
        except docker.errors.NotFound:
            return ContainerState(name=name, exists=False)
        return parse_state(
            ctr.attrs,
            managed_label=self._managed_label,
            hash_label=_HASH_LABEL,
            local_image_id=self._local_image_id((ctr.attrs.get("Config") or {}).get("Image"), {}),
        )

    def pull(self, image: str) -> None:
        # present-aware: only contact the registry when the image is absent
        try:
            self._c.images.get(image)
        except docker.errors.ImageNotFound:
            self._c.images.pull(image)
