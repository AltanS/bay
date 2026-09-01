"""The post-setup panel is the de facto onboarding checklist.

It is the last thing the wizard prints, so anything missing from it is missing
from onboarding. It used to omit the DNS record, `bin/bay validate` and
`bin/bay doctor` — and DNS guidance was printed only on the Headscale branch,
so an operator who picked no gateway was told to deploy with no record in
place, and Traefik's first ACME challenge failed.
"""

from __future__ import annotations

import pytest

from bay_cli import console as console_mod
from bay_cli.commands.framework import _print_next_steps
from bay_cli.wizard.models import RegionConfig, WizardResult

GATEWAYS = ["headscale", "wireguard", "none"]


def _result(gateway: str, **kwargs) -> WizardResult:
    return WizardResult(
        project_name="myapp",
        multi_region=False,
        server_ip="203.0.113.10",
        domain_base="example.test",
        letsencrypt_email="ops@example.test",
        access_gateway=gateway,  # type: ignore[arg-type]
        headscale_domain="hs.example.test" if gateway == "headscale" else None,
        **kwargs,
    )


def _render(result: WizardResult | None, root) -> str:
    """Capture the panel as plain text, wide enough that nothing wraps."""
    rich_console = console_mod.console
    # Save the *raw* override, not the computed width: reading `.width` and
    # writing it back pins a console that was previously auto-sizing, which
    # leaks into every later test in the session.
    previous = rich_console._width
    rich_console.width = 200
    try:
        with rich_console.capture() as capture:
            _print_next_steps(result, root)
    finally:
        rich_console._width = previous
    return capture.get()


@pytest.mark.parametrize("gateway", GATEWAYS)
def test_panel_names_dns_validate_and_doctor(gateway, tmp_path):
    out = _render(_result(gateway), tmp_path)
    assert "DNS record" in out, f"{gateway}: the panel never mentions DNS"
    assert "example.test" in out, f"{gateway}: DNS step names no domain"
    assert "203.0.113.10" in out, f"{gateway}: DNS step names no target"
    assert "bin/bay validate" in out, f"{gateway}: no validate step"
    assert "bin/bay doctor" in out, f"{gateway}: no doctor step"
    assert "bin/bay provision production" in out
    assert "bin/bay deploy production" in out


@pytest.mark.parametrize("gateway", GATEWAYS)
def test_panel_orders_the_checklist(gateway, tmp_path):
    """DNS → secrets → validate → doctor → provision → deploy."""
    out = _render(_result(gateway), tmp_path)
    order = ["DNS record", "secrets", "bin/bay validate", "bin/bay doctor",
             "bin/bay provision", "bin/bay deploy"]
    positions = [out.find(token) for token in order]
    assert all(p != -1 for p in positions), dict(zip(order, positions))
    assert positions == sorted(positions), (
        f"{gateway}: checklist out of order — {dict(zip(order, positions))}"
    )


def test_headscale_gets_its_control_plane_record(tmp_path):
    """A control-plane domain outside the base domain needs its own record."""
    result = _result("headscale")
    result.headscale_domain = "hs.other.test"
    out = _render(result, tmp_path)
    assert "hs.other.test" in out


def test_panel_survives_a_missing_result(tmp_path):
    """--no-interactive scaffolds without a WizardResult; DNS is still named."""
    out = _render(None, tmp_path)
    assert "DNS record" in out
    assert "bin/bay validate" in out
    assert "bin/bay doctor" in out


def test_multi_region_panel_still_lists_the_preflight_steps(tmp_path):
    result = WizardResult(
        project_name="myapp",
        multi_region=True,
        regions=[RegionConfig(name="eu", server_ip="203.0.113.10"),
                 RegionConfig(name="us", server_ip="203.0.113.11")],
        domain_base="example.test",
        access_gateway="headscale",
        headscale_domain="hs.example.test",
    )
    out = _render(result, tmp_path)
    assert "DNS record" in out
    assert "bin/bay validate" in out
    assert "bin/bay doctor" in out
