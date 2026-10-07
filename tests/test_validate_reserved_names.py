"""`bay validate` must refuse service names that collide with derived names.

Bay derives Traefik router names (`<svc>`, `<svc>-vpn`, `<svc>-public`,
`<svc>-health`), the zero-downtime canary container (`<svc>-new`) and the
one-shot release container (`<svc>-release`) from the
service name. The CrowdSec VPN-refusal whitelist trusts routers ending in
`-vpn@docker` and `-tailnet@file`, so a public service literally named
`foo-vpn` must not exist.
"""

from __future__ import annotations

import pytest

from bay_cli.commands.validate import ValidationResult, _validate_reserved_names


class _JsonMode:
    def __enter__(self):
        from bay_cli.console.output import set_json_mode

        set_json_mode(True)
        return self

    def __exit__(self, *args):
        from bay_cli.console.output import set_json_mode

        set_json_mode(False)


def _check(data: dict) -> ValidationResult:
    result = ValidationResult()
    with _JsonMode():
        _validate_reserved_names(data, result)
    return result


def _svc() -> dict:
    return {"access": "public", "image": "nginx:latest"}


@pytest.mark.parametrize("suffix", ["-vpn", "-public", "-health", "-tailnet", "-release"])
def test_reserved_router_ending_is_an_error(suffix):
    result = _check({"services": {f"foo{suffix}": _svc()}})
    assert result.total_issues == 1
    message = result.failed[0]
    assert f"foo{suffix}" in message
    assert f"'{suffix}'" in message


def test_vpn_message_explains_the_whitelist():
    result = _check({"services": {"foo-vpn": _svc()}})
    assert "CrowdSec" in result.failed[0]


@pytest.mark.parametrize(
    "name",
    ["vpnadmin", "my-vpn-ui", "vpn", "publicapi", "health", "tailnetui", "foo", "foo-new"],
)
def test_allowed_names_pass(name):
    result = _check({"services": {name: _svc()}})
    assert result.total_issues == 0, result.failed


def test_service_named_after_another_services_canary_is_an_error():
    result = _check({"services": {"foo": _svc(), "foo-new": _svc()}})
    assert result.total_issues == 1
    assert "foo-new" in result.failed[0]
    assert "canary" in result.failed[0]


def test_accessory_named_after_a_services_canary_is_an_error():
    result = _check(
        {"services": {"foo": _svc()}, "accessories": {"foo-new": {"image": "x"}}}
    )
    assert result.total_issues == 1
    assert result.failed[0].startswith("accessories.foo-new")


def test_new_ending_without_a_matching_service_is_fine():
    result = _check({"services": {"foo-new": _svc(), "bar": _svc()}})
    assert result.total_issues == 0


def test_accessory_suffix_is_not_reserved():
    """Accessories get no router and never canary, so only the -new rule applies."""
    result = _check({"accessories": {"db-vpn": {"image": "postgres:16"}}})
    assert result.total_issues == 0


def test_empty_and_underscore_entries_are_ignored():
    assert _check({}).total_issues == 0
    assert _check({"services": {"_anchors-vpn": {}}}).total_issues == 0
