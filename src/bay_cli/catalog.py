"""YAML catalog loader — scans catalog directories and returns service/accessory definitions."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

from bay_cli.errors import BayError


@dataclass
class CatalogEntry:
    """A service or accessory definition loaded from a YAML catalog file."""

    id: str
    name: str
    category: str  # "service" or "accessory"
    domain_prefix: str | None
    default_access: str  # "vpn" or "public"
    dependencies: list[str]
    spec_block: dict[str, Any]
    post_add_instructions: list[str]
    env_secret: list[str]
    config_files: list[str]
    definition_path: Path


_yaml = YAML()
_yaml.preserve_quotes = True

_VALID_CATEGORIES = {"service", "accessory"}


def _package_framework_root() -> Path:
    """Resolve the framework root from this package's file location.

    The package lives at ``<framework_root>/src/bay_cli/``, so the
    framework root is three levels up from this file.
    """
    return Path(__file__).resolve().parent.parent.parent


def load_catalog(
    framework_root: Path,
    consumer_root: Path,
) -> dict[str, CatalogEntry]:
    """Load catalog entries from framework and consumer directories.

    Scans ``framework_root/catalog/`` then ``consumer_root/catalog/``.
    Consumer entries with the same ``meta.id`` override framework entries
    entirely (no merging).
    """
    catalog: dict[str, CatalogEntry] = {}

    fw_catalog_dir = framework_root / "catalog"
    if fw_catalog_dir.is_dir():
        catalog.update(_scan_catalog_dir(fw_catalog_dir))

    consumer_catalog_dir = consumer_root / "catalog"
    if consumer_catalog_dir.is_dir():
        catalog.update(_scan_catalog_dir(consumer_catalog_dir))

    return catalog


def _scan_catalog_dir(path: Path) -> dict[str, CatalogEntry]:
    """Scan a single catalog directory and return entries keyed by id.

    Supports two file forms:
    - Flat file: ``<id>.yml``
    - Directory form: ``<id>/definition.yml`` (takes precedence)
    """
    entries: dict[str, CatalogEntry] = {}
    seen_dirs: set[str] = set()

    # Pass 1: directory form (takes precedence)
    for child in sorted(path.iterdir()):
        if child.is_dir():
            defn_file = child / "definition.yml"
            if defn_file.is_file():
                entry = _load_definition(defn_file, child.name)
                entries[entry.id] = entry
                seen_dirs.add(child.name)

    # Pass 2: flat files (skip if directory form already loaded)
    for child in sorted(path.iterdir()):
        if child.is_file() and child.suffix == ".yml":
            stem = child.stem
            if stem in seen_dirs:
                continue  # directory form takes precedence
            entry = _load_definition(child, stem)
            entries[entry.id] = entry

    return entries


def _load_definition(source_path: Path, expected_id: str) -> CatalogEntry:
    """Parse a single YAML definition file and return a CatalogEntry."""
    with source_path.open() as f:
        data = _yaml.load(f)

    if not isinstance(data, dict):
        raise BayError(f"Catalog definition is not a YAML mapping: {source_path}")

    _validate_definition(data, source_path, expected_id)

    meta = data["meta"]
    defaults = data.get("defaults", {}) or {}
    spec = data["spec"]

    return CatalogEntry(
        id=meta["id"],
        name=meta["name"],
        category=meta["category"],
        domain_prefix=meta.get("domain_prefix"),
        default_access=defaults.get("access", "vpn"),
        dependencies=list(meta.get("dependencies") or []),
        spec_block=dict(spec),
        post_add_instructions=list(meta.get("post_add_instructions") or []),
        env_secret=_extract_env_secret(spec),
        config_files=list(spec.get("config_files") or []),
        definition_path=source_path,
    )


def _validate_definition(
    data: dict[str, Any],
    source_path: Path,
    expected_id: str,
) -> None:
    """Validate a catalog definition's required fields and structure."""
    # meta key
    meta = data.get("meta")
    if not isinstance(meta, dict):
        raise BayError(f"Missing or invalid 'meta' section in {source_path}")

    # Required meta fields
    for field_name in ("id", "name", "category"):
        val = meta.get(field_name)
        if not val or not isinstance(val, str):
            raise BayError(
                f"Missing or empty 'meta.{field_name}' in {source_path}"
            )

    # Category validation
    category = meta["category"]
    if category not in _VALID_CATEGORIES:
        raise BayError(
            f"Invalid meta.category '{category}' in {source_path} "
            f"(must be one of: {', '.join(sorted(_VALID_CATEGORIES))})"
        )

    # Filesystem name must match meta.id
    if meta["id"] != expected_id:
        raise BayError(
            f"Filesystem name '{expected_id}' does not match meta.id "
            f"'{meta['id']}' in {source_path}"
        )

    # Services must have domain_prefix
    if category == "service" and not meta.get("domain_prefix"):
        raise BayError(
            f"Service definition missing 'meta.domain_prefix' in {source_path}"
        )

    # spec key
    spec = data.get("spec")
    if not isinstance(spec, dict):
        raise BayError(f"Missing or invalid 'spec' section in {source_path}")


def _extract_env_secret(spec_block: dict[str, Any]) -> list[str]:
    """Extract secret key names from spec.env.secret.

    Handles both formats:
    - list[str]: ``["KEY1", "KEY2"]`` -> ``["KEY1", "KEY2"]``
    - dict[str, str]: ``{ENV_VAR: VAULT_KEY}`` -> ``["ENV_VAR"]``
    """
    env = spec_block.get("env")
    if not isinstance(env, dict):
        return []

    secret = env.get("secret")
    if secret is None:
        return []

    if isinstance(secret, list):
        return [str(s) for s in secret]
    if isinstance(secret, dict):
        return list(secret.keys())

    return []


def resolve_dependencies(
    selected: list[str],
    catalog: dict[str, CatalogEntry],
    existing_accessories: list[str],
) -> list[str]:
    """Return catalog ids of missing dependencies that need to be added.

    Smart matching: if a selected service depends on "postgres", any
    postgres-compatible accessory already configured (e.g., "pgvector",
    "postgres-16") satisfies the dependency.
    """
    missing: list[str] = []
    seen: set[str] = set()

    # Compatibility map: dependency id -> compatible accessory patterns
    _COMPAT = {
        "postgres": {"postgres", "pgvector", "postgresql", "pg"},
    }

    for entry_id in selected:
        entry = catalog.get(entry_id)
        if entry is None:
            continue
        for dep_id in entry.dependencies:
            if dep_id in seen:
                continue

            # Check if dependency is already satisfied
            compatible = _COMPAT.get(dep_id, {dep_id})
            already_have = any(
                any(compat in acc.lower() for compat in compatible)
                for acc in existing_accessories
            )
            already_selected = dep_id in selected

            if not already_have and not already_selected:
                missing.append(dep_id)
                seen.add(dep_id)

    return missing
