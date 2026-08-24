"""Regression tests for the reconciler's port-spec → docker SDK mapping.

The headscale coordinator publishes ``3478:3478/udp`` (DERP/STUN). Before the
fix, ``_ports_to_sdk`` unconditionally appended ``/tcp`` and produced the key
``3478/udp/tcp``, which Docker rejected with "unknown protocol" on container
create — surfacing only on a fresh create (headscale was a NoOp until then).
"""

from bay_reconcile.sdk_client import _ports_to_sdk


def test_udp_protocol_preserved_two_part():
    assert _ports_to_sdk(["3478:3478/udp"]) == {"3478/udp": "3478"}


def test_udp_protocol_preserved_three_part():
    assert _ports_to_sdk(["0.0.0.0:3478:3478/udp"]) == {"3478/udp": ("0.0.0.0", "3478")}


def test_bare_port_defaults_to_tcp():
    assert _ports_to_sdk(["5432:5432"]) == {"5432/tcp": "5432"}
    assert _ports_to_sdk(["100.64.0.1:5432:5432"]) == {"5432/tcp": ("100.64.0.1", "5432")}


def test_mixed_specs():
    out = _ports_to_sdk(["3478:3478/udp", "5432:5432"])
    assert out == {"3478/udp": "3478", "5432/tcp": "5432"}
