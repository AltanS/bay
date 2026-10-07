"""The catalog must emit services.yml the schema accepts.

Two shipped definitions did not: Gatus used `healthcheck: {path: ...}`
(the schema's healthcheck block has no `path` — the probe path is the
sibling key `healthcheck_path`), and MariaDB used
`backup.method: mysqldump`, which is not in the method enum. Both reached
the fleet through the catalog, so the
very first `bay validate` on a fresh project failed on a file the tool had
just written.

Catalog `spec` blocks are fragments. The service-level keys (`access`,
`domains`) come from the consumer's config, so each fragment is completed
here before validation.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
import yaml
from jsonschema import Draft202012Validator

from bay_cli.catalog import _package_framework_root, load_catalog

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


def test_example_services_yml_matches_schema() -> None:
    document = yaml.safe_load((ROOT / "example" / "group_vars" / "all" / "services.yml").read_text())
    assert _errors(document) == []
