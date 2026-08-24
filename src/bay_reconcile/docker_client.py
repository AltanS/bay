"""DockerClient boundary (M85-S2): the single seam between the pure planner
and the docker daemon.

The executor (S4) depends only on this Protocol, so tests substitute a
FakeDockerClient with no daemon, and the real SDK-backed implementation
(``SdkDockerClient``, added in S4) is the only place that imports ``docker``.
"""
from __future__ import annotations

from typing import Protocol

from .models import ContainerSpec, ContainerState


class DockerClient(Protocol):
    """The minimal docker surface the reconciler needs.

    ``observe`` is the batched read that collapses the Ansible per-container
    inspects into one call; everything else is a targeted mutation used only
    when the plan calls for a change.
    """

    def observe(self, managed_label: str) -> dict[str, ContainerState]:
        """Return every container keyed by name, from one ``list(all=True)``."""
        ...

    def create(self, spec: ContainerSpec, *, name_override: str | None = None) -> None:
        """Create and start a container from ``spec`` (optionally under a
        canary name via ``name_override``)."""
        ...

    def stop(self, name: str, *, timeout: int = 10) -> None: ...

    def remove(self, name: str) -> None: ...

    def rename(self, old: str, new: str) -> None: ...

    def inspect(self, name: str) -> ContainerState:
        """Re-read a single container (used to poll canary health)."""
        ...

    def pull(self, image: str) -> None:
        """Ensure ``image`` is present locally (no-op when already present)."""
        ...
