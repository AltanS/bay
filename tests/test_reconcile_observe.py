"""Parity-oracle tests for observe parsing + port normalization.

Mirrors tests/test_port_drift.py against the reconciler's pure helpers.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from bay_reconcile.observe import (  # noqa: E402
    desired_port_tuples,
    observed_port_tuples,
    parse_state,
    port_binding_tuple,
    port_spec_tuple,
)


class TestPortSpecTuple:
    def test_bare_port(self) -> None:
        assert port_spec_tuple("5432:5432") == "0.0.0.0:5432"

    def test_loopback(self) -> None:
        assert port_spec_tuple("127.0.0.1:5432:5432") == "127.0.0.1:5432"

    def test_tailnet(self) -> None:
        assert port_spec_tuple("100.64.0.1:5432:5432") == "100.64.0.1:5432"

    def test_empty_and_none(self) -> None:
        assert port_spec_tuple("") == ""
        assert port_spec_tuple(None) == ""


class TestPortBindingTuple:
    def test_with_ip(self) -> None:
        assert port_binding_tuple({"HostIp": "100.64.0.1", "HostPort": "5432"}) == "100.64.0.1:5432"

    def test_empty_ip_defaults_to_wildcard(self) -> None:
        assert port_binding_tuple({"HostIp": "", "HostPort": "5432"}) == "0.0.0.0:5432"

    def test_non_mapping(self) -> None:
        assert port_binding_tuple("nonsense") == ""


class TestDriftMatrix:
    """Real-world transitions from test_port_drift.py."""

    def test_loopback_to_tailnet_drifts(self) -> None:
        desired = desired_port_tuples(["100.64.0.1:5432:5432"])
        observed = observed_port_tuples({"5432/tcp": [{"HostIp": "127.0.0.1", "HostPort": "5432"}]})
        assert desired != observed

    def test_host_port_change_drifts(self) -> None:
        desired = desired_port_tuples(["15432:5432"])
        observed = observed_port_tuples({"5432/tcp": [{"HostIp": "", "HostPort": "5432"}]})
        assert desired != observed

    def test_unchanged_is_noop(self) -> None:
        desired = desired_port_tuples(["127.0.0.1:5432:5432"])
        observed = observed_port_tuples({"5432/tcp": [{"HostIp": "127.0.0.1", "HostPort": "5432"}]})
        assert desired == observed

    def test_no_ports_both_empty(self) -> None:
        assert desired_port_tuples([]) == observed_port_tuples(None) == ()


class TestParseState:
    @staticmethod
    def _attrs() -> dict[str, object]:
        return {
            "Name": "/web",
            "Config": {
                "Image": "web:latest",
                "Labels": {"com.bay.config-hash": "abc123", "com.bay.managed": "true"},
            },
            "State": {"Status": "running", "Health": {"Status": "healthy"}},
            "RestartCount": 2,
            "HostConfig": {"PortBindings": {"80/tcp": [{"HostIp": "", "HostPort": "8090"}]}},
        }

    def test_parses_core_fields(self) -> None:
        st = parse_state(self._attrs(), managed_label="com.bay.managed")
        assert st.name == "web"
        assert st.exists
        assert st.image == "web:latest"
        assert st.config_hash == "abc123"
        assert st.status == "running"
        assert st.health == "healthy"
        assert st.managed
        assert st.restart_count == 2
        assert st.port_bindings == ("0.0.0.0:8090",)
        assert st.running

    def test_unmanaged_when_label_absent(self) -> None:
        attrs = self._attrs()
        attrs["Config"] = {"Image": "x", "Labels": {}}
        st = parse_state(attrs, managed_label="com.bay.managed")
        assert not st.managed
        assert st.config_hash is None


class TestImageDigestParsing:
    """``Config.Image`` is the reference; the top-level ``Image`` is the id."""

    ID = "sha256:3333333333333333333333333333333333333333333333333333333333333333"

    def _attrs(self, **over: object) -> dict[str, object]:
        base: dict[str, object] = {
            "Name": "/webhook",
            "Image": self.ID,
            "Config": {"Image": "bay-webhook:latest", "Labels": {"bay.managed": "true"}},
            "State": {"Status": "running"},
        }
        base.update(over)
        return base

    def test_reference_and_running_id_are_separate_fields(self) -> None:
        state = parse_state(self._attrs(), managed_label="bay.managed")
        assert state.image == "bay-webhook:latest"
        assert state.image_id == self.ID

    def test_local_id_comes_from_the_caller(self) -> None:
        state = parse_state(
            self._attrs(), managed_label="bay.managed", local_image_id="sha256:44"
        )
        assert state.local_image_id == "sha256:44"
        assert state.image_drifted

    def test_local_id_defaults_to_unknown_and_never_drifts(self) -> None:
        state = parse_state(self._attrs(), managed_label="bay.managed")
        assert state.local_image_id is None
        assert not state.image_drifted

    def test_equal_ids_do_not_drift(self) -> None:
        state = parse_state(
            self._attrs(), managed_label="bay.managed", local_image_id=self.ID
        )
        assert not state.image_drifted

    def test_missing_running_id_is_none_not_empty(self) -> None:
        state = parse_state(self._attrs(Image=""), managed_label="bay.managed")
        assert state.image_id is None
        assert not state.image_drifted
