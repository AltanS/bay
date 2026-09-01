"""The catalog and the wizard must emit services.yml the schema accepts.

Two shipped definitions did not: Gatus used `healthcheck: {path: ...}`
(the schema's healthcheck block has no `path` — the probe path is the
sibling key `healthcheck_path`), and MariaDB used
`backup.method: mysqldump`, which is not in the method enum. Both reached
the consumer through `bin/bay setup`, so the very first `bay validate` on
a fresh project failed on a file the tool had just written.

Catalog `spec` blocks are fragments — `bin/bay service add` fills in the
service-level keys (`access`, `domains`) from the consumer's config — so
each fragment is completed here before validation, exactly the way the
add path completes it.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
import yaml
from jsonschema import Draft202012Validator

from bay_cli.catalog import _package_framework_root, load_catalog
from bay_cli.wizard.models import WizardResult
from bay_cli.wizard.scaffold import scaffold

ROOT = _package_framework_root()
SCHEMA = json.loads((ROOT / "src" / "bay_cli" / "schemas" / "services.schema.json").read_text())
CATALOG = load_catalog(ROOT, ROOT / "does-not-exist")


def _errors(document: dict) -> list[str]:
    return [
        f"{'.'.join(str(p) for p in e.absolute_path) or '(root)'}: {e.message}"
        for e in sorted(Draft202012Validator(SCHEMA).iter_errors(document), key=lambda e: list(e.path))
    ]


@pytest.mark.parametrize("entry_id", sorted(CATALOG))
def test_catalog_entry_matches_schema(entry_id: str) -> None:
    entry = CATALOG[entry_id]
    spec = copy.deepcopy(entry.spec_block)
    if entry.category == "service":
        spec.setdefault("access", entry.default_access)
        spec.setdefault("domains", [f"{entry.domain_prefix or entry_id}.example.com"])
        spec.setdefault("ports", {"internal": 8080})
        document = {"services": {entry_id: spec}}
    else:
        document = {"accessories": {entry_id: spec}}
    assert _errors(document) == []


def test_wizard_renders_a_valid_services_yml(tmp_path: Path) -> None:
    """Select every catalog entry at once — the widest scaffold there is."""
    result = WizardResult(
        project_name="testapp",
        multi_region=False,
        server_ip="203.0.113.10",
        domain_base="example.com",
        letsencrypt_email="ops@example.com",
        ssh_keys=[],
        access_gateway="headscale",
        headscale_domain="hs.example.com",
        selected_services=sorted(CATALOG),
    )
    scaffold(result, tmp_path)
    document = yaml.safe_load((tmp_path / "group_vars/all/services.yml").read_text())
    assert _errors(document) == []


def test_example_services_yml_matches_schema() -> None:
    document = yaml.safe_load((ROOT / "example" / "group_vars" / "all" / "services.yml").read_text())
    assert _errors(document) == []
