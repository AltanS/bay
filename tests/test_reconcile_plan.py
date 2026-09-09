"""Unit tests for the pure reconcile planner.

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
        # (two containers can't share a host port) — mirrors an earlier port-drift fix.
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


class TestPlanImageDigestDrift:
    """The field incident: a rebuilt ``bay-webhook:latest`` read as NoOp.

    The config hash covers the config TEXT only. Rebuilding an image under an
    unchanged reference leaves every byte of that text identical, so the run
    reported NoOp and the container kept the old layers until someone ran
    ``docker rm -f`` by hand. The observed state now carries the image id the
    container RUNS and the id the same reference resolves to on the host, and a
    mismatch takes the same path a config change takes.
    """

    OLD = "sha256:1111111111111111111111111111111111111111111111111111111111111111"
    NEW = "sha256:2222222222222222222222222222222222222222222222222222222222222222"

    def test_rebuilt_image_digest_recreates_despite_matching_config_hash(self) -> None:
        # CONTROL for the whole fix: identical config hash, different image id.
        # Against the pre-fix planner this is a NoOp.
        state = _state(
            "webhook",
            config_hash="h1",
            image="bay-webhook:latest",
            image_id=self.OLD,
            local_image_id=self.NEW,
        )
        action = plan([_spec("webhook", config_hash="h1")], {"webhook": state}).actions[0]
        assert isinstance(action, Recreate)
        assert "image changed" in action.reason
        assert "bay-webhook:latest" in action.reason
        # The short ids of both sides are named, so the operator can trace it.
        assert "111111111111" in action.reason
        assert "222222222222" in action.reason

    def test_same_image_digest_stays_a_noop(self) -> None:
        # The control's mirror: only the image id differs between the two.
        state = _state(
            "webhook",
            config_hash="h1",
            image="bay-webhook:latest",
            image_id=self.OLD,
            local_image_id=self.OLD,
        )
        result = plan([_spec("webhook", config_hash="h1")], {"webhook": state})
        assert result.actions == (NoOp("webhook"),)
        assert result.is_noop

    def test_image_digest_absent_locally_is_not_a_reason_to_redeploy(self) -> None:
        # Never pulled on this host: unknown is no evidence of change, and the
        # comparison must not raise on the None.
        state = _state(
            "webhook",
            config_hash="h1",
            image="bay-webhook:latest",
            image_id=self.OLD,
            local_image_id=None,
        )
        assert plan([_spec("webhook", config_hash="h1")], {"webhook": state}).is_noop

    def test_unknown_running_image_digest_is_not_a_reason_to_redeploy(self) -> None:
        # The other half of "unknown": docker reported no id for the container.
        state = _state(
            "webhook", config_hash="h1", image_id=None, local_image_id=self.NEW
        )
        assert plan([_spec("webhook", config_hash="h1")], {"webhook": state}).is_noop

    def test_pinned_tag_repulled_also_recreates(self) -> None:
        # Not a :latest special case. A semver tag whose local id changed after
        # a pull is the same rule and takes the same path.
        state = _state(
            "api",
            config_hash="h1",
            image="ghcr.io/example/api:1.4.2",
            image_id=self.OLD,
            local_image_id=self.NEW,
        )
        action = plan([_spec("api", config_hash="h1")], {"api": state}).actions[0]
        assert isinstance(action, Recreate)
        assert "ghcr.io/example/api:1.4.2" in action.reason

    def test_image_digest_drift_canaries_a_zero_downtime_service(self) -> None:
        # Same path as a config change: zero-downtime services still swap.
        spec = _spec("web", zero_downtime=True, config_hash="h1")
        state = _state(
            "web", config_hash="h1", image_id=self.OLD, local_image_id=self.NEW
        )
        action = plan([spec], {"web": state}).actions[0]
        assert isinstance(action, CanarySwap)
        assert "image changed" in action.reason

    def test_config_and_image_digest_both_changed_names_both(self) -> None:
        state = _state(
            "webhook", config_hash="oldhash0000", image_id=self.OLD, local_image_id=self.NEW
        )
        action = plan([_spec("webhook", config_hash="newhash0000")], {"webhook": state}).actions[0]
        assert isinstance(action, Recreate)
        assert "config-hash changed" in action.reason
        assert "image changed" in action.reason
