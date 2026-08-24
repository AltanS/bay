"""Parity-oracle tests for observe parsing + port normalization (M85-S3).

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
