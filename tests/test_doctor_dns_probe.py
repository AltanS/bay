"""`bin/bay doctor` must resolve a name the wildcard record covers.

The probe used to resolve `domain_base` -- the bare apex. The wizard tells the
operator to create `*.example.com`, and a wildcard does not cover the apex, so
a correctly configured DNS zone reported NXDOMAIN.
"""

from __future__ import annotations

from pathlib import Path

from bay_cli.commands.doctor import (
    _dns_probe_target,
    _first_service_domain,
    _services_file,
)


def _write_services(root: Path, where: str, body: str) -> Path:
    path = root / "group_vars" / where / "services.yml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    return path


# ── Where services.yml is looked up ──────────────────────────────────────


def test_all_is_where_the_wizard_writes_it(tmp_path: Path) -> None:
    expected = _write_services(tmp_path, "all", "---\nservices: {}\n")
    assert _services_file(tmp_path, "production") == expected


def test_env_file_is_only_a_fallback(tmp_path: Path) -> None:
    _write_services(tmp_path, "all", "---\nservices: {}\n")
    env_file = _write_services(tmp_path, "production", "---\nservices: {}\n")
    assert _services_file(tmp_path, "production") != env_file
    assert _services_file(tmp_path, "production").parent.name == "all"


def test_env_file_used_when_all_is_absent(tmp_path: Path) -> None:
    env_file = _write_services(tmp_path, "production", "---\nservices: {}\n")
    assert _services_file(tmp_path, "production") == env_file


def test_no_services_file_at_all(tmp_path: Path) -> None:
    assert _services_file(tmp_path, "production") is None


# ── Which domain gets probed ─────────────────────────────────────────────


def test_first_service_domain_is_picked(tmp_path: Path) -> None:
    path = _write_services(
        tmp_path,
        "all",
        "---\nservices:\n  gatus:\n    domains:\n      - status.example.com\n",
    )
    assert _first_service_domain(path) == "status.example.com"


def test_templated_domains_are_skipped(tmp_path: Path) -> None:
    path = _write_services(
        tmp_path,
        "all",
        "---\n"
        "services:\n"
        "  gatus:\n"
        '    domains:\n'
        '      - "status.{{ domain_base }}"\n'
        "  app:\n"
        "    domains:\n"
        "      - app.example.com\n",
    )
    assert _first_service_domain(path) == "app.example.com"


def test_falls_back_to_the_status_subdomain(tmp_path: Path) -> None:
    domain, source = _dns_probe_target(None, "example.com")
    assert domain == "status.example.com"
    assert source


def test_never_probes_the_bare_apex(tmp_path: Path) -> None:
    """The apex is what a wildcard record does not cover."""
    path = _write_services(tmp_path, "all", "---\nservices: {}\n")
    for services in (path, None):
        domain, _source = _dns_probe_target(services, "example.com")
        assert domain != "example.com"
        assert domain.endswith(".example.com")


def test_no_domain_base_and_no_services_means_no_probe() -> None:
    assert _dns_probe_target(None, None) == (None, "")
