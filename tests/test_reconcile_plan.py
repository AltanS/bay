"""Unit tests for the pure reconcile planner (M85-S2).

No docker daemon, no Ansible — exercises models + plan() directly. These grow
into the parity oracle in S3.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from bay_reconcile import (  # noqa: E402
    CanarySwap,
    ContainerSpec,
    ContainerState,
    Create,
    NoOp,
    Plan,
    Recreate,
    Remove,
    plan,
)


def _spec(name: str = "web", *, config_hash: str = "h1", **kw: object) -> ContainerSpec:
    base: dict[str, object] = {
        "name": name,
        "image": f"{name}:latest",
        "type": "service",
        "config_hash": config_hash,
    }
    base.update(kw)
    return ContainerSpec(**base)  # type: ignore[arg-type]


def _state(
    name: str = "web", *, config_hash: str | None = "h1", exists: bool = True, **kw: object
) -> ContainerState:
    base: dict[str, object] = {
        "name": name,
        "exists": exists,
        "config_hash": config_hash,
        "status": "running",
        "managed": True,
    }
    base.update(kw)
    return ContainerState(**base)  # type: ignore[arg-type]


class TestPlanDecisions:
    def test_missing_container_is_created(self) -> None:
        result = plan([_spec("web")], {})
        assert result.actions == (Create(_spec("web")),)

    def test_matching_hash_is_noop(self) -> None:
        result = plan([_spec("web", config_hash="h1")], {"web": _state("web", config_hash="h1")})
        assert result.actions == (NoOp("web"),)
        assert result.is_noop

    def test_changed_hash_recreates_non_zero_downtime(self) -> None:
        result = plan(
            [_spec("db", type="accessory", config_hash="new")],
            {"db": _state("db", config_hash="old")},
        )
        assert len(result.actions) == 1
        assert isinstance(result.actions[0], Recreate)

    def test_changed_hash_canaries_zero_downtime_service(self) -> None:
        result = plan(
            [_spec("web", zero_downtime=True, config_hash="new")],
            {"web": _state("web", config_hash="old")},
        )
        assert len(result.actions) == 1
        assert isinstance(result.actions[0], CanarySwap)

    def test_zero_downtime_accessory_still_recreates(self) -> None:
        # zero-downtime only applies to services (accessories can't run two instances)
        result = plan(
            [_spec("db", type="accessory", zero_downtime=True, config_hash="new")],
            {"db": _state("db", config_hash="old")},
        )
        assert isinstance(result.actions[0], Recreate)

    def test_unstamped_container_is_recreated_not_noop(self) -> None:
        # a pre-reconciler container with no config-hash label must recreate to stamp it
        result = plan([_spec("web", config_hash="h1")], {"web": _state("web", config_hash=None)})
        assert isinstance(result.actions[0], Recreate)

    def test_managed_orphan_is_removed_when_enabled(self) -> None:
        result = plan([], {"old": _state("old", managed=True)}, remove_orphans=True)
        assert result.actions == (Remove("old", "orphaned — not in desired set"),)

    def test_orphans_kept_by_default(self) -> None:
        # opt-in safety: a managed container absent from desired is NOT removed
        # unless remove_orphans is explicitly set.
        result = plan([], {"old": _state("old", managed=True)})
        assert result.actions == ()

    def test_unmanaged_container_is_left_alone(self) -> None:
        # e.g. the buildx buildkit container — not bay-managed, must not be touched
        result = plan([], {"buildx": _state("buildx", managed=False)}, remove_orphans=True)
        assert result.actions == ()


class TestPlanAggregates:
    def test_all_matching_is_noop_plan(self) -> None:
        specs = [_spec("web", config_hash="a"), _spec("db", type="accessory", config_hash="b")]
        observed = {
            "web": _state("web", config_hash="a"),
            "db": _state("db", config_hash="b"),
        }
        assert plan(specs, observed).is_noop

    def test_summary_counts_by_kind(self) -> None:
        specs = [
            _spec("web", config_hash="a"),  # noop
            _spec("api", config_hash="new"),  # recreate
            _spec("new", config_hash="z"),  # create
        ]
        observed = {
            "web": _state("web", config_hash="a"),
            "api": _state("api", config_hash="old"),
            "gone": _state("gone", managed=True),  # remove
        }
        summary = plan(specs, observed, remove_orphans=True).summary()
        assert summary == {"NoOp": 1, "Recreate": 1, "Create": 1, "Remove": 1}

    def test_empty_plan_is_noop(self) -> None:
        assert Plan().is_noop


class TestPlanPortDrift:
    def test_port_drift_forces_recreate_not_canary(self) -> None:
        # zero-downtime service whose host binding changed must NOT canary
        # (two containers can't share a host port) — mirrors M88.
        spec = _spec("web", zero_downtime=True, config_hash="new", ports=["100.64.0.1:80:80"])
        state = _state("web", config_hash="old", port_bindings=("127.0.0.1:80",))
        action = plan([spec], {"web": state}).actions[0]
        assert isinstance(action, Recreate)
        assert "port drift" in action.reason

    def test_canary_when_ports_unchanged(self) -> None:
        spec = _spec("web", zero_downtime=True, config_hash="new", ports=["127.0.0.1:80:80"])
        state = _state("web", config_hash="old", port_bindings=("127.0.0.1:80",))
        action = plan([spec], {"web": state}).actions[0]
        assert isinstance(action, CanarySwap)
