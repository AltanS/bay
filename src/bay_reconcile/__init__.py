"""bay_reconcile — server-side deploy reconciler.

Self-contained: standard library + the docker SDK only (no Typer, Rich, Jinja2
or Ansible), so the whole package can be shipped to the target host and executed
there in a single pass — collapsing the per-task controller->server round-trips
that dominate the Ansible deploy.

Architecture (functional core / imperative shell):

    desired (ContainerSpec)  ┐
                             ├─> plan()  ──> Plan (tuple[Action, ...])  ──> execute()
    observed (ContainerState)┘   (pure)                                     (DockerClient)

The config_hash on each spec is precomputed on the CLI side (where vault is
already decrypted), so the reconciler compares opaque strings and never touches
secrets.
"""
from __future__ import annotations

from .bundle import Bundle, load_bundle, spec_from_dict
from .executor import execute
from .models import (
    Action,
    ActionResult,
    CanarySwap,
    ContainerSpec,
    ContainerState,
    Create,
    ExecutionReport,
    NoOp,
    Plan,
    ReconcilerConfig,
    Recreate,
    Remove,
)
from .planner import plan

__all__ = [
    "Action",
    "ActionResult",
    "Bundle",
    "CanarySwap",
    "ContainerSpec",
    "ContainerState",
    "Create",
    "ExecutionReport",
    "NoOp",
    "Plan",
    "ReconcilerConfig",
    "Recreate",
    "Remove",
    "execute",
    "load_bundle",
    "plan",
    "spec_from_dict",
]
