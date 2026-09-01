"""Every catalog `config_files` entry must have a file behind it.

`catalog/gatus/definition.yml` declared `config_files: [gatus/config.yaml]`
for its whole life and no such file was ever shipped. `bin/bay service add`
copied nothing (its source directory did not exist), the wizard copied
nothing at all, and the failure surfaced on the server, mid-deploy, at the
"Deploy config files" task — for the wizard's *default* service.

This test walks the catalog itself, so a new definition that names a config
file it forgot to ship fails here instead of on someone's first deploy.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bay_cli.catalog import _package_framework_root, load_catalog

ROOT = _package_framework_root()


def _entries():
    catalog = load_catalog(ROOT, ROOT / "does-not-exist")
    return sorted(catalog.values(), key=lambda e: e.id)


def _declared() -> list[tuple[str, str]]:
    return [(e.id, cf) for e in _entries() for cf in e.config_files]


def test_catalog_declares_at_least_one_config_file() -> None:
    """Guard the guard: an empty list here would make this file vacuous."""
    assert _declared(), "no catalog entry declares config_files"


@pytest.mark.parametrize("entry_id,config_file", _declared())
def test_declared_config_file_is_shipped(entry_id: str, config_file: str) -> None:
    entry = load_catalog(ROOT, ROOT / "does-not-exist")[entry_id]
    src = entry.definition_path.parent / "files" / config_file
    assert src.is_file(), (
        f"catalog entry '{entry_id}' declares config_files: [{config_file}] "
        f"but {src.relative_to(ROOT)} does not exist"
    )


def test_gatus_config_is_one_60s_http_endpoint() -> None:
    import yaml

    path = ROOT / "catalog" / "gatus" / "files" / "gatus" / "config.yaml"
    data = yaml.safe_load(path.read_text())
    assert len(data["endpoints"]) == 1
    endpoint = data["endpoints"][0]
    assert endpoint["interval"] == "60s"
    assert endpoint["url"].startswith("https://status.")


def test_example_ships_the_files_it_declares() -> None:
    """`setup --no-interactive` copies example/ verbatim, gaps included."""
    import yaml

    services = yaml.safe_load(
        (ROOT / "example" / "group_vars" / "all" / "services.yml").read_text()
    )
    declared = [
        cf
        for section in ("services", "accessories")
        for entry in (services.get(section) or {}).values()
        for cf in (entry.get("config_files") or [])
    ]
    assert declared
    for cf in declared:
        assert (ROOT / "example" / "files" / cf).is_file(), f"example/files/{cf} missing"
