"""Config read layer: comment-preserving YAML reads of the fleet's group_vars files via ruamel.yaml."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import TYPE_CHECKING, Any

from bay_cli.errors import BayError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ruamel.yaml.comments import CommentedMap

# ruamel.yaml costs ~12 ms to import and this module sits on the
# `bay_cli.cli` import path (commands/healthcheck.py, among others),
# so it is imported inside the call sites instead of at module scope.
# See tests/test_cli_import_time.py.


def _commented_map() -> type:
    """The ruamel CommentedMap class, imported on first use."""
    from ruamel.yaml.comments import CommentedMap

    return CommentedMap


class StackConfig:
    """Read/write interface to the consumer's group_vars YAML files.

    Read-only: it loads the YAML files and answers questions about them.
    Bay writes the generated services file through the compiler, and the
    other files by hand or through their own verbs.
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


# ── access_gateway ────────────────────────────────────────────────────────

#: The fleet file that sets ``access_gateway``, relative to the fleet root.
ACCESS_GATEWAY_FILE = Path("group_vars") / "all" / "access_gateway.yml"
#: The role default the deploy falls back to, relative to the framework root.
ACCESS_GATEWAY_DEFAULTS = Path("roles") / "access_gateway" / "defaults" / "main.yml"


def _yaml_value(path: Path, key: str) -> str | None:
    """``key`` of the YAML mapping in ``path`` as a string; None when absent or unreadable."""
    import yaml

    try:
        data = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError):
        return None
    if not isinstance(data, dict):
        return None
    value = data.get(key)
    return str(value) if isinstance(value, str) and value else None


def access_gateway_type(fleet_root: Path, framework_root: Path) -> str:
    """The ``access_gateway`` the boxes of this fleet deploy with.

    The fleet's ``group_vars/all/access_gateway.yml`` wins. Without it, the
    value comes from ``roles/access_gateway/defaults/main.yml`` of the
    framework checkout, read at run time, so the CLI never keeps a second copy
    of the default. A ``framework_root`` with no roles (a test context) falls
    back to the checkout of the running package.
    """
    from bay_cli.context import package_root

    value = _yaml_value(fleet_root / ACCESS_GATEWAY_FILE, "access_gateway")
    if value is not None:
        return value
    for root in (framework_root, package_root()):
        default = _yaml_value(root / ACCESS_GATEWAY_DEFAULTS, "access_gateway")
        if default is not None:
            return default
    raise BayError(
        f"cannot read access_gateway from {framework_root / ACCESS_GATEWAY_DEFAULTS}",
        hint="The framework checkout is incomplete; run `bay self version` to see which one runs.",
    )
