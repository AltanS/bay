"""Tests for port-drift detection filters.

`bay_port_binding_tuple` and `bay_port_spec_tuple` normalize both sides of
the port-drift comparison. They originally backed the inline check in
`deploy_accessory.yml` (retired with the per-container loops when the
reconciler shipped) and now
serve as the parity oracle for the reconciler's equivalent Python port logic in
`src/bay_reconcile/observe.py`. The original inline jinja comparison
false-equalled `'5432:5432'` (no IP prefix) against a container running on
`0.0.0.0`, so an IP-only change (e.g. migrating postgres from `127.0.0.1` to
`100.64.0.1` via `expose: tailnet`) didn't trigger container recreation.

The regression-test matrix covers every transition shape a consumer can
actually produce via `expose:` — loopback ↔ tailnet ↔ host, re-IP within
tailnet, port-number change, no-op, and degenerate inputs.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

FILTER_PATH = Path(__file__).resolve().parent.parent / "filter_plugins"
sys.path.insert(0, str(FILTER_PATH))
from bay_filters import bay_port_binding_tuple, bay_port_spec_tuple  # noqa: E402


# ── bay_port_binding_tuple (from Docker inspect) ─────────────────────


class TestBayPortBindingTuple:
    def test_explicit_ip(self):
        assert bay_port_binding_tuple(
            {"HostIp": "100.64.0.1", "HostPort": "5432"}
        ) == "100.64.0.1:5432"

    def test_empty_ip_defaults_to_all_interfaces(self):
        """Docker records an empty HostIp when the -p flag had no IP prefix."""
        assert bay_port_binding_tuple({"HostIp": "", "HostPort": "5432"}) == "0.0.0.0:5432"

    def test_missing_ip_field(self):
        assert bay_port_binding_tuple({"HostPort": "5432"}) == "0.0.0.0:5432"

    def test_none_input(self):
        assert bay_port_binding_tuple(None) == ""

    def test_non_dict_input(self):
        assert bay_port_binding_tuple("not a dict") == ""

    def test_int_host_port_coerced(self):
        """Jinja may pass HostPort as int or str depending on pipeline."""
        assert bay_port_binding_tuple(
            {"HostIp": "127.0.0.1", "HostPort": 5432}
        ) == "127.0.0.1:5432"

    def test_whitespace_stripped(self):
        assert bay_port_binding_tuple(
            {"HostIp": "  100.64.0.1  ", "HostPort": "  5432  "}
        ) == "100.64.0.1:5432"


# ── bay_port_spec_tuple (from compose-style `port:` field) ───────────


class TestBayPortSpecTuple:
    def test_no_ip_prefix_defaults_to_all_interfaces(self):
        """`5432:5432` with no host IP == Docker's `-p 5432:5432` which
        binds 0.0.0.0. The drift check must see this as 0.0.0.0:5432."""
        assert bay_port_spec_tuple("5432:5432") == "0.0.0.0:5432"

    def test_loopback(self):
        assert bay_port_spec_tuple("127.0.0.1:5432:5432") == "127.0.0.1:5432"

    def test_tailnet(self):
        assert bay_port_spec_tuple("100.64.0.1:5432:5432") == "100.64.0.1:5432"

    def test_public_host(self):
        assert bay_port_spec_tuple("0.0.0.0:5432:5432") == "0.0.0.0:5432"

    def test_ip_port_only_two_parts(self):
        """Rare but valid: `5432:5432` where the user omitted host IP."""
        assert bay_port_spec_tuple("5432:5432") == "0.0.0.0:5432"

    def test_trailing_protocol(self):
        """Docker supports `ip:host:container/proto`. Ports are extracted
        from the first two fields; the /proto suffix rides on container."""
        assert bay_port_spec_tuple("100.64.0.1:5432:5432/tcp") == "100.64.0.1:5432"

    def test_integer_input(self):
        """Sometimes `_spec.ports` has raw ints from set_fact."""
        assert bay_port_spec_tuple(5432) == "0.0.0.0:5432"

    def test_single_port(self):
        assert bay_port_spec_tuple("5432") == "0.0.0.0:5432"

    def test_empty_string(self):
        assert bay_port_spec_tuple("") == ""

    def test_none_input(self):
        """Must not crash when `_spec.ports` is None or absent — that's
        the safe-default path for accessories with no published ports."""
        assert bay_port_spec_tuple(None) == ""


# ── Regression matrix (the transitions that matter operationally) ────


@pytest.mark.parametrize(
    "current_binding, desired_spec, expect_drift",
    [
        # The 2026-04-22 incident shape: IP-only change that the old
        # regex-based IP comparison missed.
        ({"HostIp": "0.0.0.0", "HostPort": "5432"}, "100.64.0.1:5432:5432", True),
        ({"HostIp": "127.0.0.1", "HostPort": "5432"}, "100.64.0.1:5432:5432", True),
        ({"HostIp": "100.64.0.1", "HostPort": "5432"}, "0.0.0.0:5432:5432", True),
        # Re-IP within tailnet (Headscale reassigned the node IP).
        ({"HostIp": "100.64.0.2", "HostPort": "5432"}, "100.64.0.1:5432:5432", True),
        # Host-port change (e.g., moved postgres to 15432 because of a
        # conflict). The original IP-only comparison missed this too.
        ({"HostIp": "0.0.0.0", "HostPort": "5432"}, "0.0.0.0:15432:5432", True),
        ({"HostIp": "0.0.0.0", "HostPort": "5432"}, "5432:5432", False),  # no-op
        ({"HostIp": "127.0.0.1", "HostPort": "5432"}, "127.0.0.1:5432:5432", False),
        ({"HostIp": "100.64.0.1", "HostPort": "5432"}, "100.64.0.1:5432:5432", False),
    ],
)
def test_drift_matrix(current_binding, desired_spec, expect_drift):
    current = sorted([bay_port_binding_tuple(current_binding)])
    desired = sorted([bay_port_spec_tuple(desired_spec)])
    actual_drift = current != desired
    assert actual_drift is expect_drift, (
        f"binding={current_binding} spec={desired_spec}: "
        f"current={current} desired={desired} "
        f"(expected drift={expect_drift}, got {actual_drift})"
    )


def test_empty_current_and_desired_no_crash():
    """Accessory with no `port:` field — both sides empty, no drift."""
    current = []
    desired = [bay_port_spec_tuple(None)]
    desired = [t for t in desired if t]
    assert current == desired
