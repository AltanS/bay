"""Label dual-read across the pre-1.0 framework rename (M108-S04).

The reconciler discovers, diffs and orphan-collects managed containers purely
from docker labels. Renaming those labels in one release would have made every
already-running container invisible (never adopted, never orphan-collected)
*and*, because the config-hash label key moved too, would have made every
surviving container look like it had no hash at all — a fleet-wide recreate on
the first post-rename deploy.

These tests pin the transition contract:

- a container stamped only with the pre-1.0 labels is still `managed`
- its pre-1.0 config-hash is still read, so an unchanged spec is a NoOp
- it is still orphan-collectable when it drops out of the desired set
- new-labelled containers behave identically
- a mixed stack (some old, some new) plans correctly in one pass

Named so `pytest -k legacy_label` selects the whole surface.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from bay_reconcile.bundle import load_bundle  # noqa: E402
from bay_reconcile.models import ContainerSpec, NoOp, Recreate, Remove  # noqa: E402
from bay_reconcile.observe import (  # noqa: E402
    HASH_LABEL,
    LEGACY_HASH_LABEL,
    LEGACY_MANAGED_LABEL,
    MANAGED_LABEL,
    parse_state,
)
from bay_reconcile.planner import plan  # noqa: E402

_HASH = "a" * 64


def _attrs(name: str, labels: dict[str, str]) -> dict[str, object]:
    return {
        "Name": f"/{name}",
        "Config": {"Labels": labels, "Image": "img:latest"},
        "State": {"Status": "running"},
        "HostConfig": {"PortBindings": {}},
        "RestartCount": 0,
    }


def _spec(name: str, config_hash: str = _HASH) -> ContainerSpec:
    return ContainerSpec(
        name=name, image="img:latest", type="service", config_hash=config_hash
    )


class TestLegacyLabelObservation:
    def test_legacy_label_marks_container_managed(self) -> None:
        state = parse_state(
            _attrs("web", {LEGACY_MANAGED_LABEL: "true"}), managed_label=MANAGED_LABEL
        )
        assert state.managed is True

    def test_legacy_label_config_hash_is_read(self) -> None:
        state = parse_state(
            _attrs("web", {LEGACY_MANAGED_LABEL: "true", LEGACY_HASH_LABEL: _HASH}),
            managed_label=MANAGED_LABEL,
        )
        assert state.config_hash == _HASH

    def test_new_label_wins_over_legacy_label(self) -> None:
        state = parse_state(
            _attrs(
                "web",
                {
                    MANAGED_LABEL: "true",
                    HASH_LABEL: "b" * 64,
                    LEGACY_HASH_LABEL: _HASH,
                },
            ),
            managed_label=MANAGED_LABEL,
        )
        assert state.config_hash == "b" * 64

    def test_unlabelled_container_is_not_managed(self) -> None:
        state = parse_state(_attrs("stray", {}), managed_label=MANAGED_LABEL)
        assert state.managed is False
        assert state.config_hash is None


class TestLegacyLabelPlanning:
    def test_legacy_labelled_container_is_a_noop(self) -> None:
        """The failure mode this whole spec exists to avoid: a pre-rename
        container whose only difference is the label *key* must not recreate."""
        observed = {
            "web": parse_state(
                _attrs("web", {LEGACY_MANAGED_LABEL: "true", LEGACY_HASH_LABEL: _HASH}),
                managed_label=MANAGED_LABEL,
            )
        }
        result = plan([_spec("web")], observed)
        assert [type(a) for a in result.actions] == [NoOp]

    def test_legacy_labelled_container_still_recreates_on_real_change(self) -> None:
        observed = {
            "web": parse_state(
                _attrs("web", {LEGACY_MANAGED_LABEL: "true", LEGACY_HASH_LABEL: _HASH}),
                managed_label=MANAGED_LABEL,
            )
        }
        result = plan([_spec("web", config_hash="c" * 64)], observed)
        assert [type(a) for a in result.actions] == [Recreate]

    def test_new_labelled_container_is_a_noop(self) -> None:
        observed = {
            "web": parse_state(
                _attrs("web", {MANAGED_LABEL: "true", HASH_LABEL: _HASH}),
                managed_label=MANAGED_LABEL,
            )
        }
        result = plan([_spec("web")], observed)
        assert [type(a) for a in result.actions] == [NoOp]

    def test_legacy_labelled_orphan_is_collected(self) -> None:
        observed = {
            "gone": parse_state(
                _attrs("gone", {LEGACY_MANAGED_LABEL: "true", LEGACY_HASH_LABEL: _HASH}),
                managed_label=MANAGED_LABEL,
            )
        }
        result = plan([], observed, remove_orphans=True)
        assert [type(a) for a in result.actions] == [Remove]

    def test_unlabelled_container_is_never_orphan_collected(self) -> None:
        observed = {"stray": parse_state(_attrs("stray", {}), managed_label=MANAGED_LABEL)}
        assert plan([], observed, remove_orphans=True).actions == ()

    def test_mixed_legacy_label_stack_plans_in_one_pass(self) -> None:
        observed = {
            "old": parse_state(
                _attrs("old", {LEGACY_MANAGED_LABEL: "true", LEGACY_HASH_LABEL: _HASH}),
                managed_label=MANAGED_LABEL,
            ),
            "new": parse_state(
                _attrs("new", {MANAGED_LABEL: "true", HASH_LABEL: _HASH}),
                managed_label=MANAGED_LABEL,
            ),
            "old-orphan": parse_state(
                _attrs("old-orphan", {LEGACY_MANAGED_LABEL: "true"}),
                managed_label=MANAGED_LABEL,
            ),
        }
        result = plan(
            [_spec("old"), _spec("new")], observed, remove_orphans=True
        )
        by_type = [type(a).__name__ for a in result.actions]
        assert by_type.count("NoOp") == 2
        assert by_type.count("Remove") == 1


class TestLegacyLabelDefaults:
    def test_bundle_defaults_to_the_new_managed_label(self) -> None:
        assert load_bundle({"containers": []}).managed_label == MANAGED_LABEL

    def test_new_containers_are_stamped_with_new_labels_only(self) -> None:
        """Desired state never carries the legacy spelling — the dual-read is
        an observation-side shim, not a write-side one."""
        client = Path(__file__).parent.parent / "src" / "bay_reconcile" / "sdk_client.py"
        source = client.read_text()
        create_body = source.split("def create(", 1)[1].split("def stop(", 1)[0]
        assert LEGACY_MANAGED_LABEL not in create_body
        assert LEGACY_HASH_LABEL not in create_body
