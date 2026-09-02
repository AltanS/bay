"""Config read/write layer — comment-preserving YAML round-trips via ruamel.yaml."""

from __future__ import annotations

import copy
import difflib
from io import StringIO
from pathlib import Path
from typing import TYPE_CHECKING, Any

from bay_cli.errors import BayError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ruamel.yaml.comments import CommentedMap

# ruamel.yaml costs ~12 ms to import and this module sits on the
# `bay_cli.cli` import path (commands/webhook.py, commands/healthcheck.py),
# so it is imported inside the call sites instead of at module scope.
# See tests/test_cli_import_time.py.


def _commented_map() -> type:
    """The ruamel CommentedMap class, imported on first use."""
    from ruamel.yaml.comments import CommentedMap

    return CommentedMap


class StackConfig:
    """Read/write interface to the consumer's group_vars YAML files.

    Uses a copy-on-write pattern: load() stores the original, mutations
    operate on a deep copy, diff() compares them, save() writes the copy.
    """

    def __init__(self, root: Path) -> None:
        from ruamel.yaml import YAML

        self._root = root
        self._yaml = YAML()
        self._yaml.preserve_quotes = True
        self._yaml.width = 4096  # prevent line-wrapping
        self._yaml.indent(mapping=2, sequence=4, offset=2)
        self._yaml.explicit_start = True

        self._services_path = root / "group_vars" / "all" / "services.yml"
        self._original: CommentedMap | None = None
        self._working: CommentedMap | None = None
        self._loaded = False

    # ── Loading ──────────────────────────────────────────────────────

    def _ensure_loaded(self) -> None:
        if not self._loaded:
            self._load()

    def _load(self) -> None:
        """Load services.yml and create the working copy."""
        if not self._services_path.is_file():
            raise BayError(f"services.yml not found: {self._services_path}")

        with self._services_path.open() as f:
            self._original = self._yaml.load(f)

        if not isinstance(self._original, _commented_map()):
            raise BayError(f"services.yml is not a valid YAML mapping: {self._services_path}")

        self._working = copy.deepcopy(self._original)
        self._loaded = True

    def load_services(self) -> CommentedMap:
        """Return the working copy of services.yml."""
        self._ensure_loaded()
        return self._working  # type: ignore[return-value]

    def load_file(self, relative_path: str) -> CommentedMap:
        """Load an arbitrary YAML file relative to the consumer root."""
        path = self._root / relative_path
        if not path.is_file():
            raise BayError(f"File not found: {path}")
        with path.open() as f:
            data = self._yaml.load(f)
        if not isinstance(data, _commented_map()):
            raise BayError(f"Not a valid YAML mapping: {path}")
        return data

    def load_gateway(self) -> dict[str, Any]:
        """Load access gateway config from group_vars/all/access_gateway.yml."""
        path = self._root / "group_vars" / "all" / "access_gateway.yml"
        if not path.is_file():
            # vpn_access.yml is the legacy name
            path = self._root / "group_vars" / "all" / "vpn_access.yml"
        if not path.is_file():
            return {}
        with path.open() as f:
            data = self._yaml.load(f)
        return dict(data) if isinstance(data, dict) else {}

    def load_domains(self, env: str = "production") -> dict[str, Any]:
        """Load domain config from group_vars/<env>/domains.yml."""
        path = self._root / "group_vars" / env / "domains.yml"
        if not path.is_file():
            return {}
        with path.open() as f:
            data = self._yaml.load(f)
        return dict(data) if isinstance(data, dict) else {}

    def load_secret_keys(self) -> list[str]:
        """Scan services.yml for all env.secret key names.

        Returns the union of all secret key names across services and
        accessories. Does NOT read vault files or attempt decryption.
        """
        self._ensure_loaded()
        keys: list[str] = []

        for section in ("services", "accessories"):
            block = self._working.get(section)  # type: ignore[union-attr]
            if not isinstance(block, dict):
                continue
            for _name, svc in block.items():
                if not isinstance(svc, dict):
                    continue
                env = svc.get("env")
                if not isinstance(env, dict):
                    continue
                secret = env.get("secret")
                if isinstance(secret, list):
                    # List form: env.j2 auto-prefixes with SERVICE_NAME_SECRET_NAME
                    prefix = _name.upper().replace("-", "_")
                    keys.extend(f"{prefix}_{s}" for s in secret)
                elif isinstance(secret, dict):
                    keys.extend(secret.values())

        return keys

    # ── Services ─────────────────────────────────────────────────────

    def get_services(self) -> dict[str, Any]:
        """Return the services mapping from the working copy."""
        self._ensure_loaded()
        svc = self._working.get("services")  # type: ignore[union-attr]
        if isinstance(svc, dict) and svc:
            return dict(svc)
        return {}

    def get_accessories(self) -> dict[str, Any]:
        """Return the accessories mapping from the working copy."""
        self._ensure_loaded()
        acc = self._working.get("accessories")  # type: ignore[union-attr]
        if isinstance(acc, dict) and acc:
            return dict(acc)
        return {}

    def get_service(self, name: str) -> dict[str, Any] | None:
        """Return a single service or accessory by name, or None."""
        self._ensure_loaded()
        for section in ("services", "accessories"):
            block = self._working.get(section)  # type: ignore[union-attr]
            if isinstance(block, dict) and name in block:
                return dict(block[name])
        return None

    # ── Mutations (copy-on-write) ────────────────────────────────────

    def add_service(self, name: str, block: dict[str, Any], category: str = "service") -> None:
        """Add a service or accessory to the working copy.

        Idempotent: if name already exists, this is a no-op.
        Handles ``services: {}`` → populated mapping transition.
        """
        self._ensure_loaded()
        section = "services" if category == "service" else "accessories"
        current = self._working.get(section)  # type: ignore[union-attr]

        cm_type = _commented_map()
        if not isinstance(current, cm_type):
            # Section doesn't exist or is empty scalar — create it
            current = cm_type()
            self._working[section] = current  # type: ignore[index]

        if name in current:
            return  # idempotent

        # Convert block to CommentedMap for proper YAML output
        current[name] = self._to_commented(block)

    def remove_service(self, name: str) -> bool:
        """Remove a service or accessory from the working copy.

        Returns True if removed, False if not found (idempotent).
        """
        self._ensure_loaded()
        for section in ("services", "accessories"):
            block = self._working.get(section)  # type: ignore[union-attr]
            if isinstance(block, dict) and name in block:
                del block[name]
                return True
        return False

    def update_service(self, name: str, changes: dict[str, Any]) -> None:
        """Patch specific keys on a service/accessory in the working copy.

        Only the keys present in *changes* are updated; all other keys
        are preserved. Raises BayError if the service does not exist.
        """
        self._ensure_loaded()
        for section in ("services", "accessories"):
            block = self._working.get(section)  # type: ignore[union-attr]
            if isinstance(block, dict) and name in block:
                svc = block[name]
                for key, value in changes.items():
                    svc[key] = self._to_commented(value) if isinstance(value, dict) else value
                return
        raise BayError(f"Service or accessory '{name}' not found in services.yml")

    # ── Diff & Save ──────────────────────────────────────────────────

    def diff(self) -> str:
        """Return a unified diff of original vs working copy."""
        self._ensure_loaded()
        original_text = self._dump_to_string(self._original)
        working_text = self._dump_to_string(self._working)

        diff_lines = difflib.unified_diff(
            original_text.splitlines(keepends=True),
            working_text.splitlines(keepends=True),
            fromfile=str(self._services_path),
            tofile=str(self._services_path),
        )
        return "".join(diff_lines)

    def save(self) -> None:
        """Write the working copy back to services.yml."""
        self._ensure_loaded()
        with self._services_path.open("w") as f:
            self._yaml.dump(self._working, f)

    def has_changes(self) -> bool:
        """Check if there are unsaved changes."""
        self._ensure_loaded()
        return self._dump_to_string(self._original) != self._dump_to_string(self._working)

    # ── Multi-Region Detection ───────────────────────────────────────

    def is_multi_region(self, env: str = "production") -> bool:
        """Check if the inventory uses a multi-region structure.

        Detects the ``[production:children]`` pattern in the inventory
        INI file.
        """
        inv_path = self._root / "hosts" / env
        if not inv_path.is_file():
            return False
        content = inv_path.read_text()
        return f"[{env}:children]" in content

    def get_domain_base(self, env: str = "production") -> str | None:
        """Read domain_base from group_vars config.

        Checks group_vars/<env>/main.yml, group_vars/all/main.yml, then
        region subdirs. For multi-region, returns the first region's value.
        Returns None if not found.
        """
        for path_parts in (
            ("group_vars", env, "main.yml"),
            ("group_vars", "all", "main.yml"),
        ):
            path = self._root / Path(*path_parts)
            if path.is_file():
                with path.open() as f:
                    data = self._yaml.load(f)
                if isinstance(data, dict) and "domain_base" in data:
                    return str(data["domain_base"])

        # Multi-region fallback: return the first region's domain_base
        bases = self.get_domain_bases()
        if bases:
            return next(iter(bases.values()))
        return None

    def get_domain_bases(self) -> dict[str, str]:
        """Read domain_base from all region group_vars.

        Returns a dict of {region: domain_base} for multi-region setups.
        """
        result: dict[str, str] = {}
        group_vars = self._root / "group_vars"
        if not group_vars.is_dir():
            return result
        skip = {"all", "production", "staging", "development"}
        for subdir in sorted(group_vars.iterdir()):
            if not subdir.is_dir() or subdir.name in skip:
                continue
            main_yml = subdir / "main.yml"
            if not main_yml.is_file():
                continue
            with main_yml.open() as f:
                data = self._yaml.load(f)
            if isinstance(data, dict) and "domain_base" in data:
                result[subdir.name] = str(data["domain_base"])
        return result

    def resolve_domain_bases(self, env: str = "production") -> dict[str, str]:
        """Region -> domain_base, for expanding `{{ domain_base }}` domains.

        Multi-region consumers set it per region subdir. Single-region ones set
        it once (group_vars/<env>/ or group_vars/all/) where there is no region
        key at all — map that under `env` so a service with no `regions:` still
        expands instead of falling through to the unresolved backstop.
        """
        bases = self.get_domain_bases()
        if bases:
            return bases
        single = self.get_domain_base(env)
        return {env: single} if single else {}

    def resolve_region_vars(self, env: str = "production") -> dict[str, dict[str, str]]:
        """Region -> its scalar group_vars, for expanding templated domains.

        The healthcheck resolves ANY `{{ var }}` in a services.yml domain, not
        just `domain_base`. Hardcoding that one name meant a consumer who added
        a second per-region domain variable had those endpoints silently
        skipped — see healthcheck._TEMPLATE_VAR_RE for the case that prompted
        this.

        Only string and number scalars are returned. A domain is built by
        string substitution, so a list or dict could never produce a valid
        hostname, and admitting them would turn a config error into a
        confusing half-substituted probe.

        Single-region consumers set their variables in group_vars/<env>/ or
        group_vars/all/ where there is no region key at all — those are mapped
        under `env` so a service with no `regions:` still expands.
        """
        result: dict[str, dict[str, str]] = {}
        group_vars = self._root / "group_vars"
        if not group_vars.is_dir():
            return result

        def scalars(data: Any) -> dict[str, str]:
            if not isinstance(data, dict):
                return {}
            return {
                k: str(v)
                for k, v in data.items()
                if isinstance(v, str) or (isinstance(v, (int, float)) and not isinstance(v, bool))
            }

        skip = {"all", "production", "staging", "development"}
        for subdir in sorted(group_vars.iterdir()):
            if not subdir.is_dir() or subdir.name in skip:
                continue
            main_yml = subdir / "main.yml"
            if not main_yml.is_file():
                continue
            with main_yml.open() as f:
                found = scalars(self._yaml.load(f))
            if found:
                result[subdir.name] = found
        if result:
            return result

        # Single-region fallback, mirroring resolve_domain_bases.
        merged: dict[str, str] = {}
        for name in ("all", env):
            main_yml = group_vars / name / "main.yml"
            if main_yml.is_file():
                with main_yml.open() as f:
                    merged.update(scalars(self._yaml.load(f)))
        return {env: merged} if merged else {}

    # ── Helpers ───────────────────────────────────────────────────────

    def _dump_to_string(self, data: Any) -> str:
        buf = StringIO()
        self._yaml.dump(data, buf)
        return buf.getvalue()

    def _to_commented(self, obj: Any) -> Any:
        """Recursively convert plain dicts to CommentedMap for YAML output."""
        cm_type = _commented_map()
        if isinstance(obj, dict) and not isinstance(obj, cm_type):
            cm = cm_type()
            for k, v in obj.items():
                cm[k] = self._to_commented(v)
            return cm
        if isinstance(obj, list):
            return [self._to_commented(item) for item in obj]
        return obj
