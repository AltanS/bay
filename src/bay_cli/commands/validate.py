"""Validate command — pre-deploy schema and connectivity checks."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

import requests
import typer
from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from bay_cli import console, paths
from bay_cli.errors import BayError

# ── Stale config detection constants ──────────────────────────────────────
# sentinel file written by git_deploy/tasks/main.yml on every deploy
# (after both webhook.yml and systemd.yml complete)
_GIT_DEPLOY_SENTINEL = "state/git-deploy-config.timestamp"
# config files rendered by git_deploy that should be newer than the sentinel
_GIT_DEPLOY_CONFIG_FILES = [
    "bin/rebuild.sh",
    "webhook/config.json",
    "webhook/image-map.json",
]

# Jinja2 vault-ref pattern shared by _validate_build_tokens and _probe_token_scope
_TOKEN_RE = re.compile(r"\{\{\s*secrets\.(\w+)\s*\}\}")


app = typer.Typer(help="Validate configuration files.")


# ── Stale config detection ────────────────────────────────────────────────

def _read_stack_name_local(root: Path) -> str | None:
    """Read stack_name from consumer group_vars/all/main.yml.

    Returns None when not found (validate may run without the framework
    fully installed). Uses ruamel.yaml (already a dependency).
    """
    main_yml = root / "group_vars" / "all" / "main.yml"
    if not main_yml.is_file():
        return None
    try:
        _yaml = YAML()
        with main_yml.open() as fh:
            data = _yaml.load(fh)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    name = data.get("stack_name")
    return str(name) if name else None


def _check_config_age_remote(
    root: Path,
    env: str,
    bay_dir: Path,
    result: ValidationResult,
    *,
    limit: str | None = None,
) -> None:
    """SSH to the target host and compare git_deploy config file mtimes.

    Compares the mtime of each config file against the sentinel timestamp.
    Files older than the sentinel indicate a partial deploy left the webhook
    infrastructure stale (e.g., `--tags deploy_stack` without `git_deploy`).

    Reports a warning (not a hard failure) because:
    - The files might not exist yet (first deploy, or no build services).
    - Minor clock skew between renders is not actionable.
    """
    console.header("Config Freshness")

    stack_name = _read_stack_name_local(root)
    if stack_name is None:
        result.warn(
            "Config freshness   cannot read stack_name from "
            "group_vars/all/main.yml -- skipping config-age check"
        )
        return

    # shell snippet: emit one line per file: "<status>\t<filename>"
    # where status is:
    #   ok         — file is newer than sentinel (or sentinel missing → nothing to check)
    #   stale      — file is older than sentinel (drift!)
    #   missing    — config file doesn't exist (not deployed yet)
    #   no_sentinel — sentinel doesn't exist (git_deploy hasn't run yet)
    stack_dir = f"/opt/{stack_name}"
    sentinel = f"{stack_dir}/{_GIT_DEPLOY_SENTINEL}"
    files_expr = " ".join(
        f'"{stack_dir}/{f}"' for f in _GIT_DEPLOY_CONFIG_FILES
    )
    snippet = (
        f"SENTINEL={sentinel}; "
        f"if [ ! -f \"$SENTINEL\" ]; then "
        f"  echo 'no_sentinel\t(none)'; exit 0; "
        f"fi; "
        f"STIME=$(stat -c %Y \"$SENTINEL\"); "
        f"for F in {files_expr}; do "
        f"  NAME=$(basename \"$F\"); "
        f"  if [ ! -f \"$F\" ]; then "
        f"    echo \"missing\t${{NAME}}\"; "
        f"  elif [ $(stat -c %Y \"$F\") -ge $STIME ]; then "
        f"    echo \"ok\t${{NAME}}\"; "
        f"  else "
        f"    echo \"stale\t${{NAME}}\"; "
        f"  fi; "
        f"done"
    )

    try:
        from bay_cli import ansible

        uv_cmd = ansible._uv_run_cmd(bay_dir)
        full_cmd = [
            *uv_cmd,
            "ansible", env,
            "-m", "ansible.builtin.shell",
            "-a", snippet,
            "--become",
        ]
        if limit:
            full_cmd.extend(["--limit", limit])

        proc = subprocess.run(
            full_cmd,
            capture_output=True,
            text=True,
            timeout=30,
            env={**__import__("os").environ, **ansible._collections_env(bay_dir)},
        )
        output = proc.stdout or ""
    except subprocess.TimeoutExpired:
        result.warn(
            "Config freshness   timed out reaching host -- "
            "skipping config-age check"
        )
        return
    except FileNotFoundError:
        result.warn(
            "Config freshness   ansible not found -- "
            "skipping config-age check"
        )
        return

    # Parse ansible ad-hoc output — strip ANSI codes and header lines
    _ansi_re = re.compile(r"\x1b\[[0-9;]*m")
    lines = [_ansi_re.sub("", ln).strip() for ln in output.splitlines()]
    # Collect tab-separated status lines (filter out ansible header/footer)
    status_lines = [ln for ln in lines if "\t" in ln and not ln.startswith(">>")]

    if not status_lines:
        # Ansible may have returned a non-zero exit (host unreachable etc.)
        result.warn(
            "Config freshness   no output from host -- "
            "check ansible connectivity (ansible returned "
            f"rc={proc.returncode})"
        )
        return

    stale: list[str] = []
    missing: list[str] = []
    no_sentinel = False
    ok_count = 0

    for line in status_lines:
        parts = line.split("\t", 1)
        if len(parts) != 2:
            continue
        status, name = parts[0].strip(), parts[1].strip()
        if status == "no_sentinel":
            no_sentinel = True
            break
        elif status == "ok":
            ok_count += 1
        elif status == "stale":
            stale.append(name)
        elif status == "missing":
            missing.append(name)

    if no_sentinel:
        result.warn(
            "Config freshness   sentinel not found on host "
            f"({sentinel}) -- git_deploy hasn't run since sentinel "
            "support was added (deploy once with `bin/bay deploy`)"
        )
        return

    if missing:
        result.warn(
            f"Config freshness   {len(missing)} config file(s) missing on host: "
            + ", ".join(missing)
            + " -- run `bin/bay deploy` to create them"
        )

    if stale:
        result.warn(
            f"Config freshness   {len(stale)} config file(s) are older than "
            "the last git_deploy run: "
            + ", ".join(stale)
            + " -- these files may not reflect current services.yml. "
            "Re-run without --tags to refresh git_deploy configs."
        )
    elif not missing and not no_sentinel:
        result.ok(
            f"Config freshness   all {ok_count} config file(s) are current"
        )


# ── Result collector ─────────────────────────────────────────────────────

class ValidationResult:
    """Collect pass/fail/warn results across all checks."""

    def __init__(self) -> None:
        self.passed: list[str] = []
        self.failed: list[str] = []
        self.warnings: list[str] = []

    def ok(self, msg: str) -> None:
        self.passed.append(msg)
        console.success(msg)

    def fail(self, msg: str) -> None:
        self.failed.append(msg)
        console.error(msg)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)
        console.warning(msg)

    @property
    def total_issues(self) -> int:
        return len(self.failed)

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "failed": self.failed,
            "warnings": self.warnings,
            "total_passed": len(self.passed),
            "total_failed": len(self.failed),
            "total_warnings": len(self.warnings),
        }


# ── Framework version drift ─────────────────────────────────────────────

def _validate_version_drift(
    root: Path, bay_dir: Path, result: ValidationResult
) -> None:
    """Check that the installed framework version matches ``.argo-version``."""  # legacy-argo: .argo/.argo-version convention, rename lands in S03
    # Dev-link mode: version pinning is intentionally bypassed
    if paths.is_dev_linked(root):
        result.warn(
            "Framework version  dev-link active -- version pinning bypassed"
        )
        return

    pinned = paths.read_pinned_version(root)
    if pinned is None:
        result.warn(
            "Framework version  .argo-version not found, skipping drift check"  # legacy-argo: .argo/.argo-version convention, rename lands in S03
        )
        return

    installed = paths.read_installed_version(bay_dir)
    if installed is None:
        result.fail(
            "Framework version  .argo/version.yml missing or unreadable"  # legacy-argo: .argo/.argo-version convention, rename lands in S03
        )
        return

    if installed.lstrip("v") == pinned.lstrip("v"):
        result.ok(f"Framework version  {pinned} matches installed")
    else:
        result.fail(
            f"Framework version  mismatch: .argo-version={pinned}, "  # legacy-argo: .argo/.argo-version convention, rename lands in S03
            f"installed={installed} -- run 'bin/bay install' to sync"
        )


# ── YAML parsing validation ─────────────────────────────────────────────

def _validate_yaml_files(root: Path, env: str, result: ValidationResult) -> dict[str, Any]:
    """Validate all group_vars YAML files parse correctly.

    Returns a dict of {relative_path: parsed_data} for files that parsed OK.
    """
    console.header("YAML Syntax")

    parsed: dict[str, Any] = {}
    yaml = YAML()
    yaml.preserve_quotes = True

    # Collect all YAML files in group_vars/
    group_vars = root / "group_vars"
    if not group_vars.is_dir():
        result.fail(f"group_vars directory not found at {group_vars}")
        return parsed

    yaml_files: list[Path] = []
    for subdir in sorted(group_vars.iterdir()):
        if subdir.is_dir():
            for f in sorted(subdir.iterdir()):
                if f.suffix in (".yml", ".yaml") and f.is_file():
                    yaml_files.append(f)

    if not yaml_files:
        result.warn("No YAML files found in group_vars/")
        return parsed

    for path in yaml_files:
        rel = path.relative_to(root)

        # Check if file is vault-encrypted
        try:
            first_line = path.read_text(errors="replace").split("\n", 1)[0]
        except OSError as e:
            result.fail(f"{rel}  read error: {e}")
            continue

        if first_line.strip().startswith("$ANSIBLE_VAULT"):
            # Attempt to decrypt and validate
            _validate_vault_file(root, path, rel, yaml, result, parsed)
            continue

        try:
            with path.open() as f:
                data = yaml.load(f)
            parsed[str(rel)] = data
            result.ok(f"{rel}")
        except YAMLError as e:
            # Extract the most useful line from the error
            err_msg = str(e).split("\n")[0] if str(e) else "parse error"
            result.fail(f"{rel}  {err_msg}")

    return parsed


def _validate_vault_file(
    root: Path,
    path: Path,
    rel: Path,
    yaml: YAML,
    result: ValidationResult,
    parsed: dict[str, Any],
) -> None:
    """Try to decrypt and validate a vault-encrypted file."""
    vault_pass = root / ".vault_pass"
    if not vault_pass.exists():
        result.warn(f"{rel}  vault-encrypted, skipped (no .vault_pass)")
        return

    try:
        proc = subprocess.run(
            [
                "ansible-vault", "decrypt",
                "--vault-password-file", str(vault_pass),
                "--output=-",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if proc.returncode != 0:
            stderr = proc.stderr.strip().split("\n")[0] if proc.stderr else "decrypt failed"
            result.warn(f"{rel}  vault decrypt failed: {stderr}")
            return

        from io import StringIO
        data = yaml.load(StringIO(proc.stdout))
        parsed[str(rel)] = data
        result.ok(f"{rel}  (vault-encrypted)")
    except subprocess.TimeoutExpired:
        result.warn(f"{rel}  vault decrypt timed out")
    except YAMLError as e:
        err_msg = str(e).split("\n")[0] if str(e) else "parse error"
        result.fail(f"{rel}  vault decrypted but invalid YAML: {err_msg}")
    except FileNotFoundError:
        result.warn(f"{rel}  ansible-vault not found, skipping")


# ── Inventory validation ─────────────────────────────────────────────────

def _resolve_inventory(root: Path, env: str) -> Path | None:
    """Find the inventory file for the given env.

    Checks hosts/<env> first, then falls back to hosts/production
    if <env> is a region group within it.
    """
    direct = root / "hosts" / env
    if direct.is_file():
        return direct
    # Multi-region fallback: env may be a group inside hosts/production
    production = root / "hosts" / "production"
    if production.is_file():
        content = production.read_text()
        if f"[{env}]" in content:
            return production
    return None


_DOCUMENTATION_NETS = ("192.0.2.", "198.51.100.", "203.0.113.")


def _is_documentation_ip(host: str) -> bool:
    """True for RFC 5737 documentation addresses (never routable)."""
    return host.startswith(_DOCUMENTATION_NETS)


def _validate_inventory(root: Path, env: str, result: ValidationResult) -> None:
    """Validate the hosts inventory file."""
    console.header("Inventory")

    inv_path = _resolve_inventory(root, env)
    if inv_path is None:
        result.fail(f"hosts/{env}  inventory file not found")
        return

    inv_label = inv_path.relative_to(root)
    if inv_path.name != env:
        inv_label = f"{inv_label} (group '{env}')"

    text = inv_path.read_text()
    lines = text.splitlines()

    # Check for basic structure: at least one group header or host line
    has_group = False
    has_host = False
    errors: list[str] = []

    for i, line in enumerate(lines, 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith(";"):
            continue

        if stripped.startswith("["):
            if not stripped.endswith("]"):
                errors.append(f"line {i}: unclosed group header: {stripped}")
            else:
                has_group = True
            continue

        # Should be a host line: first token is hostname/IP
        has_host = True

    if errors:
        for err in errors:
            result.fail(f"{inv_label}  {err}")
    elif not has_group:
        result.fail(f"{inv_label}  no group headers found")
    elif not has_host:
        result.fail(f"{inv_label}  no host entries found")
    else:
        # Count hosts and groups
        from bay_cli.inventory import InventoryConfig
        inv = InventoryConfig()
        inv.load(inv_path)
        hosts = inv.list_hosts()
        groups = inv.get_groups()
        result.ok(
            f"{inv_label}  {len(hosts)} host{'s' if len(hosts) != 1 else ''} "
            f"in {len(groups)} group{'s' if len(groups) != 1 else ''}"
        )
        # The scaffolded inventory ships an RFC 5737 documentation address
        # so the file is structurally valid on day one. Deploying to it
        # goes nowhere, so say so every run until it is replaced.
        placeholders = sorted(
            {
                str(h.get("ip") or h.get("name") or "")
                for h in hosts
                if _is_documentation_ip(str(h.get("ip") or h.get("name") or ""))
            }
        )
        if placeholders:
            result.warn(
                f"{inv_label}  still points at the example address "
                f"{', '.join(placeholders)} -- replace it with your server"
            )


# ── Schema validation ───────────────────────────────────────────────────

def _load_schema() -> dict[str, Any]:
    """Load the services.yml JSON schema from package data."""
    schema_path = Path(__file__).parent.parent / "schemas" / "services.schema.json"
    if not schema_path.is_file():
        raise BayError(f"Schema file not found: {schema_path}")
    return json.loads(schema_path.read_text())


def _validate_services_schema(
    root: Path,
    parsed_files: dict[str, Any],
    result: ValidationResult,
) -> dict[str, Any] | None:
    """Validate services.yml against the JSON schema.

    Returns the plain-dict version of services data for use by later checks,
    or None if services.yml was not found / not parsed.
    """
    from jsonschema import Draft202012Validator, ValidationError

    console.header("Service Schema")

    # Find services.yml in parsed files
    services_data = None
    services_rel = None
    for rel_path, data in parsed_files.items():
        if rel_path.endswith("services.yml") and "services" in (data or {}):
            services_data = data
            services_rel = rel_path
            break

    if services_data is None:
        result.warn("services.yml  not found or not parsed, skipping schema validation")
        return None

    schema = _load_schema()

    # Convert ruamel CommentedMap to plain dict for jsonschema
    plain_data = _to_plain(services_data)

    validator = Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(plain_data), key=lambda e: list(e.path))

    if not errors:
        svc_count = len(plain_data.get("services", {}))
        acc_count = len(plain_data.get("accessories", {}))
        result.ok(
            f"{services_rel}  schema valid "
            f"({svc_count} service{'s' if svc_count != 1 else ''}, "
            f"{acc_count} accessor{'ies' if acc_count != 1 else 'y'})"
        )
    else:
        for error in errors:
            path = ".".join(str(p) for p in error.absolute_path) if error.absolute_path else "(root)"
            # Simplify common error messages
            msg = _format_schema_error(error, path)
            result.fail(f"{services_rel}  {msg}")

    # Additional cross-field validations that JSON schema can't express
    _validate_cross_references(plain_data, services_rel or "", result)
    _validate_service_links(plain_data, services_rel or "", result)
    _validate_accessory_bindings(plain_data, services_rel or "", result)
    _validate_link_target_exposure(plain_data, services_rel or "", result)
    _validate_expose_host_ack(plain_data, services_rel or "", result)

    return plain_data


def _validate_accessory_bindings(
    data: dict[str, Any],
    rel_path: str,
    result: ValidationResult,
) -> None:
    """Pre-deploy binding-IP validator.

    Asserts that each accessory's `port:` string is consistent with its
    declared (or defaulted) `expose:` mode. Catches:
      - expose: loopback but port has a non-loopback IP
      - expose: host but port has a non-0.0.0.0 IP
      - expose: tailnet with a hardcoded IP (the tailnet IP is
        injected at render time, so the operator shouldn't prefix)
      - no expose: set + explicit 0.0.0.0 IP prefix (suspicious —
        operator probably meant expose: host)
      - any unexpected IP prefix that isn't one of the three sanctioned
        forms

    The goal is to catch the *next* variant of the 2026-04-22 incident
    shape before any role, macro, or template accidentally ships a
    port binding outside the three sanctioned patterns.
    """
    import re

    accessories = data.get("accessories") or {}
    if not isinstance(accessories, dict):
        return

    _ip_prefix_re = re.compile(r"^(\d{1,3}(?:\.\d{1,3}){3}):")

    for name, acc in accessories.items():
        if not isinstance(acc, dict):
            continue
        port = acc.get("port")
        if not port:
            continue
        expose = acc.get("expose")

        port_str = str(port)
        m = _ip_prefix_re.match(port_str)
        ip = m.group(1) if m else None

        if expose == "loopback":
            if ip and ip != "127.0.0.1":
                result.fail(
                    f"{rel_path}  accessory '{name}' has port binding "
                    f"'{port_str}' which does not match expose: loopback "
                    f"(expected '127.0.0.1:<port>' or no IP prefix). "
                    f"Remove the IP prefix, or change expose: to 'host' "
                    f"or 'tailnet' if the non-loopback bind is intentional."
                )
        elif expose == "host":
            if ip and ip != "0.0.0.0":
                result.fail(
                    f"{rel_path}  accessory '{name}' has port binding "
                    f"'{port_str}' which does not match expose: host "
                    f"(expected '0.0.0.0:<port>' or no IP prefix)."
                )
        elif expose == "tailnet":
            if ip is not None:
                result.fail(
                    f"{rel_path}  accessory '{name}' has port binding "
                    f"'{port_str}' but expose: tailnet renders the "
                    f"tailnet IP at deploy time. Remove the '{ip}:' "
                    f"prefix — use port: '<host_port>:<container_port>'."
                )
        elif expose is None:
            # expose: not declared. Unprefixed (loopback default via
            # shim/S7-removal path) is fine; explicit 0.0.0.0 is
            # suspicious because it bypasses the declared-exposure
            # contract established by S2.
            if ip == "0.0.0.0":
                result.fail(
                    f"{rel_path}  accessory '{name}' has port binding "
                    f"'{port_str}' with an explicit 0.0.0.0 prefix but "
                    f"no expose: field. Add expose: host to make the "
                    f"public bind explicit and auditable."
                )
            elif ip and ip not in ("127.0.0.1", "0.0.0.0"):
                # Hardcoded non-sanctioned IP (e.g., the operator typed
                # the tailnet IP literally). Not a security issue today
                # but breaks the S10 drift-check invariant.
                result.fail(
                    f"{rel_path}  accessory '{name}' has port binding "
                    f"'{port_str}' with a hardcoded IP prefix. Use "
                    f"expose: tailnet / loopback / host instead so the "
                    f"bind IP is resolved per host."
                )


def _validate_expose_host_ack(
    data: dict[str, Any],
    rel_path: str,
    result: ValidationResult,
) -> None:
    """`expose: host` must carry an explicit `expose_host_ack: true`.

    A host-published port (``0.0.0.0:<port>``) is DNAT'd by Docker in
    PREROUTING and never reaches the nftables ``input`` chain — which is
    where the CrowdSec bouncer set lives.  Bay deliberately does not
    manage a DOCKER-USER chain, so such a port has no firewall in front
    of it at all.  That is a legitimate choice for some workloads, but it
    must be a deliberate, recorded one rather than a one-word default, so
    the sibling boolean ``expose_host_ack`` is required alongside it.

    Applies to both shapes: an accessory's top-level ``expose:`` and a
    service's ``ports.expose:``.  Every other expose mode (loopback,
    gateway, tailnet) needs no acknowledgement.
    """
    _why = (
        "A host-published port is DNAT'd in PREROUTING and never reaches the "
        "nftables input chain, so it is never seen by the CrowdSec bouncer and "
        "no firewall rule applies to it (Bay deliberately manages no "
        "DOCKER-USER chain). "
    )

    accessories = data.get("accessories") or {}
    if isinstance(accessories, dict):
        for name, acc in accessories.items():
            if not isinstance(acc, dict):
                continue
            if acc.get("expose") != "host":
                continue
            if acc.get("expose_host_ack") is True:
                continue
            result.fail(
                f"{rel_path}  accessory '{name}' sets expose: host without "
                f"expose_host_ack: true. " + _why + "Add "
                f"'expose_host_ack: true' next to 'expose: host' to record "
                f"that you accept the bypass, or use expose: loopback / "
                f"gateway instead."
            )

    services = data.get("services") or {}
    if isinstance(services, dict):
        for name, svc in services.items():
            if not isinstance(svc, dict):
                continue
            ports = svc.get("ports")
            if not isinstance(ports, dict):
                continue
            if ports.get("expose") != "host":
                continue
            if ports.get("expose_host_ack") is True:
                continue
            result.fail(
                f"{rel_path}  service '{name}' sets ports.expose: host "
                f"without ports.expose_host_ack: true. " + _why + "Add "
                f"'expose_host_ack: true' under 'ports:' to record that you "
                f"accept the bypass, or drop the host binding and let Traefik "
                f"front the service."
            )


def _validate_link_target_exposure(
    data: dict[str, Any],
    rel_path: str,
    result: ValidationResult,
) -> None:
    """Cross-region link target must declare host exposure.

    A service in region X that declares ``links: { foo: { region: Y } }``
    needs ``foo`` to be reachable on host Y's tailnet interface — otherwise
    the consumer container resolves ``LINKS_FOO_HOST/PORT`` correctly but no
    daemon is listening (the user's bug from issue #7).

    For services, the declaration is ``ports.expose: tailnet`` (or ``host``).
    For accessory link targets, ``expose: tailnet`` (top-level shape).
    Same-region links are unaffected — containers reach each other inside
    the docker network by container name.
    """
    services = data.get("services") or {}
    accessories = data.get("accessories") or {}
    if not isinstance(services, dict):
        return

    for src_name, svc in services.items():
        if not isinstance(svc, dict):
            continue
        links = svc.get("links")
        if not isinstance(links, dict):
            continue
        src_regions = svc.get("regions") or []

        for tgt_name, link_cfg in links.items():
            if not isinstance(link_cfg, dict):
                continue
            tgt_region = link_cfg.get("region")
            if not tgt_region:
                continue

            # Same-region link → no host binding needed
            if src_regions and tgt_region in src_regions and len(src_regions) == 1:
                continue

            target = services.get(tgt_name) or accessories.get(tgt_name)
            if not isinstance(target, dict):
                # Missing target is reported separately by _validate_service_links
                continue

            # Determine declared exposure
            if tgt_name in services:
                ports = target.get("ports") or {}
                expose = ports.get("expose") if isinstance(ports, dict) else None
                expose_path = "ports.expose"
            else:
                expose = target.get("expose")
                expose_path = "expose"

            if expose not in ("tailnet", "host"):
                result.fail(
                    f"{rel_path}  service '{src_name}' has cross-region link to "
                    f"'{tgt_name}' (region: {tgt_region}) but '{tgt_name}' does "
                    f"not declare {expose_path}: tailnet (or host). Without it, "
                    f"the link target has no host-port binding on region "
                    f"'{tgt_region}' and the consumer will resolve "
                    f"LINKS_{tgt_name.upper().replace('-', '_')}_HOST/PORT but "
                    f"connect to nothing. Add {expose_path}: tailnet to "
                    f"'{tgt_name}'."
                )


def _validate_service_links(
    data: dict[str, Any],
    rel_path: str,
    result: ValidationResult,
) -> None:
    """Run the Python link validator against services/accessories.

    Catches the same-stack link trap that lets a stray `links:`
    entry silently expose an accessory via the deploy_stack port
    rewrite. Running here blocks `bin/bay deploy` at its pre-deploy
    validation step.
    """
    from bay_cli.links import validate_links

    services = data.get("services") or {}
    accessories = data.get("accessories") or {}
    if not isinstance(services, dict) or not isinstance(accessories, dict):
        return

    errors = validate_links(services, accessories)
    for err in errors:
        result.fail(f"{rel_path}  {err}")


def _validate_backup_config(
    services_data: dict[str, Any],
    parsed_files: dict[str, Any],
    result: ValidationResult,
) -> None:
    """Check backup configuration consistency.

    Warns when accessories define ``backup:`` blocks but
    ``backup_enabled`` is not set (defaults to false), and checks for
    required S3/restic credentials when backups are enabled.
    """
    console.header("Backup Configuration")

    accessories = services_data.get("accessories", {}) or {}

    # Find accessories with backup: blocks
    backup_accessories: list[str] = []
    for acc_name, acc in accessories.items():
        if not isinstance(acc, dict):
            continue
        if acc.get("backup"):
            backup_accessories.append(acc_name)

    if not backup_accessories:
        result.ok("Backup config      no accessories have backup configured")
        return

    # Check if backup_enabled is set in any group_vars file
    backup_enabled: bool | None = None
    for _rel_path, data in parsed_files.items():
        if not isinstance(data, dict):
            continue
        if "backup_enabled" in data:
            backup_enabled = bool(data["backup_enabled"])

    acc_list = ", ".join(sorted(backup_accessories))
    count = len(backup_accessories)
    acc_noun = "accessories" if count != 1 else "accessory"

    if not backup_enabled:
        reason = (
            "not set (defaults to false)" if backup_enabled is None else "false"
        )
        result.warn(
            f"Backup config      {count} {acc_noun} ({acc_list}) "
            f"define backup: blocks, but backup_enabled is {reason} "
            f"-- backups will not run. Set backup_enabled: true in "
            f"group_vars/all/main.yml and add S3 credentials to vault"
        )
        return

    # backup_enabled is true — check for required credentials in vault
    _backup_required_keys = [
        "backup_s3_endpoint",
        "backup_s3_bucket",
        "backup_s3_access_key_id",
        "backup_s3_secret_access_key",
        "backup_restic_password",
    ]

    # Find decrypted secrets dict
    vault_data: dict[str, Any] | None = None
    for rel_path, data in parsed_files.items():
        if rel_path.endswith("secrets.yml") and isinstance(data, dict):
            vault_data = (
                data.get("secrets", data)
                if isinstance(data.get("secrets"), dict)
                else data
            )
            break

    if vault_data is None:
        result.ok(
            f"Backup config      {count} {acc_noun} ({acc_list}) "
            f"with backup_enabled=true"
        )
        result.warn(
            "Backup config      vault not decryptable -- "
            "cannot verify S3/restic credentials"
        )
        return

    missing = [k for k in _backup_required_keys if not vault_data.get(k)]
    if missing:
        result.ok(
            f"Backup config      {count} {acc_noun} ({acc_list}) "
            f"with backup_enabled=true"
        )
        for key in missing:
            result.fail(
                f"Backup config      missing secret: '{key}' "
                f"-- add to vault via 'bin/bay vault edit production'"
            )
    else:
        result.ok(
            f"Backup config      {count} {acc_noun} ({acc_list}) "
            f"with backup_enabled=true, S3 credentials present"
        )


_ALERT_LEVELS = ("debug", "info", "warn", "critical")
_ALERT_ADAPTERS = ("telegram", "webhook")
# Reserved for a later phase. Validated-but-rejected so that turning
# them on is purely additive and nobody ships config that silently does nothing.
_ALERT_RESERVED_KEYS = ("include", "exclude")


def _validate_alert_recipients(
    parsed_files: dict[str, Any],
    result: ValidationResult,
) -> None:
    """Check the alert recipient list, and the two migration traps.

    Trap 1 - duplicate delivery. Legacy `alert_webhook_url` /
    `docker_monitor_telegram_*` desugar into implicit recipients that still
    fire. The first thing an operator does when trying the new syntax is add an
    explicit recipient pointing at the same chat, and then every alert arrives
    twice.

    Trap 2 - group_vars precedence. Both real consumers set
    `alert_webhook_url` in `group_vars/<env>/main.yml` (env level) while the
    CLI writes `group_vars/all/alerts.yml`. Ansible precedence puts env above
    all, so legacy config silently wins over the new list.
    """
    console.header("Alert Recipients")

    merged: dict[str, Any] = {}
    origin: dict[str, str] = {}
    for rel, data in (parsed_files or {}).items():
        if not isinstance(data, dict):
            continue
        for key, value in data.items():
            merged[key] = value
            origin[key] = rel

    recipients = merged.get("alert_recipients")
    legacy_url = merged.get("alert_webhook_url") or ""
    legacy_token = merged.get("docker_monitor_telegram_bot_token") or ""

    if not recipients and not legacy_url and not legacy_token:
        console.info("No alert recipients configured")
        return

    if recipients is not None and not isinstance(recipients, list):
        result.fail("alert_recipients must be a list")
        return

    seen_names: set[str] = set()
    targets: dict[str, str] = {}

    for index, recipient in enumerate(recipients or []):
        label = f"alert_recipients[{index}]"
        if not isinstance(recipient, dict):
            result.fail(f"{label} must be a mapping")
            continue

        name = recipient.get("name")
        if not name:
            result.fail(f"{label} has no name")
            name = label
        elif name in seen_names:
            result.fail(f"Duplicate recipient name: {name}")
        seen_names.add(name)

        adapter = recipient.get("adapter", "webhook")
        if adapter not in _ALERT_ADAPTERS:
            result.fail(
                f"{name}: unknown adapter {adapter!r} "
                f"(expected one of {', '.join(_ALERT_ADAPTERS)})"
            )

        min_level = recipient.get("min_level", "info")
        if min_level not in _ALERT_LEVELS:
            result.fail(
                f"{name}: unknown min_level {min_level!r} "
                f"(expected one of {', '.join(_ALERT_LEVELS)})"
            )

        for reserved in _ALERT_RESERVED_KEYS:
            if reserved in recipient:
                result.fail(
                    f"{name}: per-recipient {reserved!r} is reserved and not yet "
                    f"implemented. Use min_level, or alerts_disabled for a "
                    f"global mute — a reserved key here would silently do nothing."
                )

        config = recipient.get("config") or {}
        if not isinstance(config, dict):
            result.fail(f"{name}: config must be a mapping")
            continue

        if adapter == "telegram":
            if not (config.get("bot_token") or config.get("token_env")):
                result.fail(f"{name}: telegram recipient needs bot_token or token_env")
            if not (config.get("chat_id") or config.get("chat_id_env")):
                result.fail(f"{name}: telegram recipient needs chat_id or chat_id_env")
            target = "telegram:" + str(config.get("chat_id", ""))
        else:
            if not (config.get("url") or config.get("url_env")):
                result.fail(f"{name}: webhook recipient needs url or url_env")
            target = "webhook:" + str(config.get("url", ""))

        if target not in ("telegram:", "webhook:"):
            if target in targets:
                result.fail(
                    f"{name} and {targets[target]} both deliver to the same "
                    f"target - every alert would arrive twice"
                )
            else:
                targets[target] = name

    # Trap 1: an explicit recipient pointing where the legacy vars already point.
    if legacy_url and ("webhook:" + str(legacy_url)) in targets:
        result.fail(
            f"{targets['webhook:' + str(legacy_url)]} points at the same URL as "
            f"the legacy alert_webhook_url, so every alert is delivered twice. "
            f"Remove alert_webhook_url, or point the recipient elsewhere."
        )

    # Trap 2: legacy config at env level outranks the new list at all level.
    if recipients and legacy_url:
        legacy_origin = origin.get("alert_webhook_url", "")
        list_origin = origin.get("alert_recipients", "")
        if "/all/" in list_origin and "/all/" not in legacy_origin:
            result.warn(
                f"alert_webhook_url is set in {legacy_origin} (env level) while "
                f"alert_recipients is in {list_origin} (all level). Ansible "
                f"precedence puts env above all, so the legacy sink keeps firing "
                f"alongside the new list."
            )

    count = len(recipients or [])
    legacy_count = (1 if legacy_url else 0) + (1 if legacy_token else 0)
    result.ok(f"{count} explicit recipient(s), {legacy_count} legacy sink(s)")


def _validate_cross_references(
    data: dict[str, Any],
    rel_path: str,
    result: ValidationResult,
) -> None:
    """Validate cross-references: database.accessory must exist, etc."""
    accessories = data.get("accessories", {}) or {}
    services = data.get("services", {}) or {}

    for svc_name, svc in (services or {}).items():
        if not isinstance(svc, dict):
            continue

        # Database binding must reference an existing accessory
        db = svc.get("database")
        if isinstance(db, dict):
            acc_name = db.get("accessory", "")
            if acc_name and acc_name not in accessories:
                result.fail(
                    f"{rel_path}  services.{svc_name}.database.accessory "
                    f"references '{acc_name}' which is not defined in accessories"
                )

        # Build strategy validation
        build = svc.get("build")
        if isinstance(build, dict):
            strategy = build.get("strategy", "")
            if strategy == "registry" and not svc.get("image"):
                result.fail(
                    f"{rel_path}  services.{svc_name}.build.strategy is 'registry' "
                    f"but no image: field is defined -- registry strategy requires "
                    f"an image to pull"
                )
            if strategy in ("remote", "push") and not svc.get("image"):
                result.fail(
                    f"{rel_path}  services.{svc_name}.build.strategy is '{strategy}' "
                    f"but no image: field -- remote strategy requires an image "
                    f"reference (e.g., registry.example.com/stack/service:latest)"
                )

        # Build paths validation
        if isinstance(build, dict):
            build_paths = build.get("paths")
            if isinstance(build_paths, dict):
                for key in ("include", "exclude"):
                    val = build_paths.get(key)
                    if val is not None and not isinstance(val, list):
                        result.fail(
                            f"{rel_path}  services.{svc_name}.build.paths.{key} "
                            f"must be a list of glob patterns"
                        )
                    elif isinstance(val, list):
                        for i, pattern in enumerate(val):
                            if not isinstance(pattern, str) or not pattern.strip():
                                result.fail(
                                    f"{rel_path}  services.{svc_name}.build.paths.{key}[{i}] "
                                    f"must be a non-empty string"
                                )
                if not build_paths.get("include") and not build_paths.get("exclude"):
                    result.warn(
                        f"{rel_path}  services.{svc_name}.build.paths is defined "
                        f"but has neither 'include' nor 'exclude'"
                    )

        # image + build without strategy:registry or strategy:remote/push is an error
        if svc.get("image") and isinstance(svc.get("build"), dict):
            build_strategy = svc["build"].get("strategy", "")
            if build_strategy not in ("registry", "remote", "push"):
                result.fail(
                    f"{rel_path}  services.{svc_name} has both 'image' and 'build' "
                    f"-- set build.strategy: registry (or remote) or remove one"
                )

    # ── Webhook + strategy drift detection ──
    webhook_cfg = data.get("webhook")
    if webhook_cfg:
        build_services = {
            name: svc
            for name, svc in services.items()
            if isinstance(svc, dict) and isinstance(svc.get("build"), dict)
        }
        local_build_services = [
            name
            for name, svc in build_services.items()
            if svc["build"].get("strategy", "") in ("", "local")
        ]
        non_local_build_services = [
            name
            for name, svc in build_services.items()
            if svc["build"].get("strategy", "") in ("remote", "push", "registry")
        ]

        # Per-service warning: registry + webhook won't auto-build
        # (remote/push strategies now support auto-builds via build server webhook)
        registry_build_services = [
            name
            for name, svc in build_services.items()
            if svc["build"].get("strategy", "") == "registry"
        ]
        for svc_name in registry_build_services:
            result.warn(
                f"{rel_path}  services.{svc_name} uses strategy 'registry' "
                f"with webhook configured -- webhook pushes will notify but "
                f"not auto-build. Use external CI or 'bin/bay deploy'."
            )

        # Global warning: all build services use registry strategy
        if build_services and not local_build_services and registry_build_services and len(registry_build_services) == len(build_services):
            result.warn(
                f"{rel_path}  all build services use registry strategy "
                f"({', '.join(registry_build_services)}) but webhook is "
                f"configured -- the webhook receiver will be deployed but "
                f"no auto-builds will trigger"
            )


def _format_schema_error(error: Any, path: str) -> str:
    """Format a jsonschema ValidationError into a readable message."""
    if error.validator == "required":
        missing = error.message.split("'")[1] if "'" in error.message else error.message
        return f"{path}: missing required field '{missing}'"

    if error.validator == "enum":
        return f"{path}: {error.message}"

    if error.validator == "type":
        expected = error.validator_value
        return f"{path}: expected {expected}, got {type(error.instance).__name__}"

    if error.validator == "minLength":
        return f"{path}: must not be empty"

    if error.validator == "minItems":
        return f"{path}: must not be empty"

    if error.validator == "minimum":
        return f"{path}: must be >= {error.validator_value}"

    if error.validator == "additionalProperties":
        return f"{path}: {error.message}"

    if error.validator == "oneOf":
        return f"{path}: must match exactly one of the allowed types"

    if error.validator == "anyOf":
        # This is typically for image/build mutual exclusion
        return f"{path}: must have either 'image' or 'build' (one is required)"

    if error.validator == "not":
        return f"{path}: 'image' and 'build' are mutually exclusive"

    return f"{path}: {error.message}"


def _to_plain(obj: Any) -> Any:
    """Recursively convert ruamel CommentedMap/Seq to plain dict/list."""
    if isinstance(obj, dict):
        return {str(k): _to_plain(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_to_plain(item) for item in obj]
    if isinstance(obj, bool):
        return obj
    if isinstance(obj, (int, float)):
        return obj
    if obj is None:
        return obj
    return str(obj)


# ── Connectivity and access pre-checks (S3) ─────────────────────────────

def _validate_connectivity(
    root: Path,
    services_data: dict[str, Any],
    parsed_files: dict[str, Any],
    result: ValidationResult,
) -> None:
    """Run connectivity and access pre-checks on service definitions.

    Checks: git repo accessibility, Docker image existence, vault key
    presence, depends_on references, and domain uniqueness.
    """
    console.header("Connectivity & References")

    services = services_data.get("services", {}) or {}
    accessories = services_data.get("accessories", {}) or {}
    all_entries = {**services, **accessories}

    # ── Domain uniqueness ──────────────────────────────────────────────
    _check_domain_uniqueness(services, result)

    # ── depends_on references ──────────────────────────────────────────
    _check_depends_on(services, accessories, result)

    # ── Vault key validation ───────────────────────────────────────────
    _check_vault_keys(services, accessories, parsed_files, result)

    # ── Build token vault presence ─────────────────────────────────────
    # Extract vault_data for _validate_build_tokens (same logic as _check_vault_keys)
    _bt_vault_data: dict[str, Any] | None = None
    _bt_secrets_path: str = ""
    for _rel_path, _data in parsed_files.items():
        if _rel_path.endswith("secrets.yml") and isinstance(_data, dict):
            _bt_vault_data = _data.get("secrets", _data) if isinstance(_data.get("secrets"), dict) else _data
            _bt_secrets_path = _rel_path
            break
    _validate_build_tokens(services, accessories, _bt_vault_data, _bt_secrets_path, result)

    # ── Git repo accessibility (build services) ────────────────────────
    _check_git_repos(services, accessories, result)

    # ── Docker image existence (image services) ────────────────────────
    _check_docker_images(services, accessories, result)


def _check_domain_uniqueness(
    services: dict[str, Any],
    result: ValidationResult,
) -> None:
    """Verify no two services claim the same domain (with region overlap)."""
    # Map: domain -> list of (svc_name, regions)
    domain_map: dict[str, list[tuple[str, list[str]]]] = {}

    for svc_name, svc in services.items():
        if not isinstance(svc, dict):
            continue
        domains = svc.get("domains", [])
        regions = list(svc.get("regions", []))
        for domain in domains:
            domain_map.setdefault(str(domain), []).append((svc_name, regions))

    has_dups = False
    for domain, owners in domain_map.items():
        if len(owners) < 2:
            continue
        # Check for region overlap among the owners
        for i, (name_a, regions_a) in enumerate(owners):
            for name_b, regions_b in owners[i + 1:]:
                if _regions_overlap(regions_a, regions_b):
                    result.fail(
                        f"Domain '{domain}' is claimed by both '{name_a}' and '{name_b}' "
                        f"(overlapping regions) -- this causes silent Traefik routing conflicts"
                    )
                    has_dups = True

    if not has_dups:
        result.ok("Domain uniqueness  no duplicates found")


def _regions_overlap(a: list[str], b: list[str]) -> bool:
    """Check if two region lists overlap (empty = all = overlaps with anything)."""
    if not a or not b:
        return True
    return bool(set(a) & set(b))


def _check_depends_on(
    services: dict[str, Any],
    accessories: dict[str, Any],
    result: ValidationResult,
) -> None:
    """Verify depends_on entries reference existing services or accessories."""
    all_names = set(services.keys()) | set(accessories.keys())
    all_entries = {**services, **accessories}
    has_errors = False

    for entry_name, entry in all_entries.items():
        if not isinstance(entry, dict):
            continue
        deps = entry.get("depends_on", [])
        if not deps:
            continue
        for dep in deps:
            if dep not in all_names:
                result.fail(
                    f"'{entry_name}'.depends_on references '{dep}' "
                    f"which is not defined in services or accessories"
                )
                has_errors = True

    if not has_errors:
        result.ok("depends_on refs    all references valid")


def _check_vault_keys(
    services: dict[str, Any],
    accessories: dict[str, Any],
    parsed_files: dict[str, Any],
    result: ValidationResult,
) -> None:
    """Verify env.secret keys exist in the decrypted vault.

    If vault was not decryptable (not in parsed_files), skip with warning.
    """
    # Collect all required secret key names from services + accessories
    required_keys: dict[str, list[str]] = {}  # key_name -> [svc_names]
    all_entries = {**services, **accessories}

    for entry_name, entry in all_entries.items():
        if not isinstance(entry, dict):
            continue
        env = entry.get("env")
        if not isinstance(env, dict):
            continue
        secret = env.get("secret")
        if isinstance(secret, list):
            # List form: env.j2 auto-prefixes with SERVICE_NAME_SECRET_NAME
            prefix = entry_name.upper().replace("-", "_")
            for key_name in secret:
                vault_key = f"{prefix}_{key_name}"
                required_keys.setdefault(vault_key, []).append(entry_name)
        elif isinstance(secret, dict):
            for vault_key in secret.values():
                required_keys.setdefault(str(vault_key), []).append(entry_name)

        # Database binding: vault key is SERVICE_POSTGRES_PASSWORD
        db = entry.get("database")
        if isinstance(db, dict):
            db_user = db.get("user") or entry_name
            db_vault_key = db_user.upper().replace("-", "_") + "_POSTGRES_PASSWORD"
            required_keys.setdefault(db_vault_key, []).append(entry_name)

    if not required_keys:
        result.ok("Vault keys         no secret references to check")
        return

    # Find decrypted secrets.yml in parsed files
    # Vault files define a top-level `secrets` dict (referenced as secrets.KEY)
    vault_data: dict[str, Any] | None = None
    for rel_path, data in parsed_files.items():
        if rel_path.endswith("secrets.yml") and isinstance(data, dict):
            # Unwrap the `secrets` key if present
            vault_data = data.get("secrets", data) if isinstance(data.get("secrets"), dict) else data
            break

    if vault_data is None:
        result.warn(
            f"Vault keys         {len(required_keys)} secret(s) referenced but vault "
            f"not decryptable -- skipping key validation"
        )
        return

    # Check each required key against vault contents. Presence is not
    # enough: a scaffolded `KEY: ""` used to pass here and then hand the
    # container an empty password, which reads as "auth is broken" days
    # later instead of "you never filled this in".
    missing: list[str] = []
    empty: list[str] = []
    for key_name in sorted(required_keys.keys()):
        users = ", ".join(sorted(set(required_keys[key_name])))
        if key_name not in vault_data:
            missing.append(f"'{key_name}' (used by {users})")
            continue
        value = vault_data[key_name]
        if value is None or (isinstance(value, str) and not value.strip()):
            empty.append(f"'{key_name}' (used by {users})")

    if missing or empty:
        for m in missing:
            result.fail(f"Vault keys         missing: {m}")
        for e in empty:
            result.fail(
                f"Vault keys         empty: {e} -- generate one with 'bin/bay secret'"
            )
    else:
        result.ok(f"Vault keys         all {len(required_keys)} referenced secret(s) present")


def _validate_build_tokens(
    services: dict,
    accessories: dict,
    vault_data: dict | None,
    secrets_path: str,
    result: ValidationResult,
) -> None:
    """Check that build.token vault key references exist and are non-empty.

    If vault_data is None (vault not decryptable), emits a visible warning
    and returns — never silently skips, because that would be a false-negative.
    """
    if vault_data is None:
        result.warn("build.token vault presence check skipped — vault not decryptable")
        return

    all_entries = {**services, **accessories}
    errors: list[str] = []
    checked = 0

    for name, entry in all_entries.items():
        if not isinstance(entry, dict):
            continue
        build = entry.get("build")
        if not isinstance(build, dict):
            continue
        token = build.get("token")
        if token is None:
            continue

        checked += 1
        token_str = str(token)
        m = _TOKEN_RE.search(token_str)
        if not m:
            errors.append(
                f"services.{name}.build.token: unrecognised format — "
                f"expected '{{{{ secrets.KEY }}}}' "
                f"(hint: use bin/bay service add --build-token to set this correctly)"
            )
            continue

        key = m.group(1)
        if key not in vault_data:
            errors.append(
                f"services.{name}.build.token: references {key} — "
                f"not found in vault ({secrets_path})"
            )
        elif vault_data[key] == "" or vault_data[key] is None:
            errors.append(
                f"services.{name}.build.token: references {key} — "
                f"value is empty in vault ({secrets_path})"
            )

    if not checked:
        result.ok("Build tokens       no build.token references to check")
        return

    if errors:
        for err in errors:
            result.fail(err)
    else:
        result.ok(f"Build tokens       all {checked} build.token reference(s) valid")


def _check_git_repos(
    services: dict[str, Any],
    accessories: dict[str, Any],
    result: ValidationResult,
) -> None:
    """Check git repo accessibility for build-from-source services."""
    all_entries = {**services, **accessories}
    build_services: list[tuple[str, dict[str, Any]]] = []

    for name, entry in all_entries.items():
        if not isinstance(entry, dict):
            continue
        build = entry.get("build")
        if isinstance(build, dict) and build.get("repo"):
            build_services.append((name, build))

    if not build_services:
        return  # no build services, nothing to check

    from concurrent.futures import ThreadPoolExecutor, as_completed

    def _check_one_repo(svc_name: str, build: dict[str, Any]) -> tuple[str, str, str]:
        """Check a single git repo. Returns (level, svc_name, message)."""
        repo = str(build["repo"])
        branch = build.get("branch")
        try:
            proc = subprocess.run(
                ["git", "ls-remote", "--exit-code", repo],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if proc.returncode != 0:
                hint = ""
                if "could not read Username" in (proc.stderr or ""):
                    hint = " -- set up a deploy token or add build.token"
                elif "not found" in (proc.stderr or "").lower():
                    hint = " -- check the repo URL"
                return ("warn", svc_name,
                    f"Git repo           '{svc_name}' repo unreachable: {repo}{hint}")

            if branch:
                refs = proc.stdout or ""
                branch_pattern = f"refs/heads/{branch}"
                if branch_pattern not in refs:
                    return ("warn", svc_name,
                        f"Git repo           '{svc_name}' branch '{branch}' not found in {repo}")

            branch_info = f" (branch: {branch})" if branch else ""
            return ("ok", svc_name, f"Git repo           '{svc_name}'{branch_info} accessible")

        except subprocess.TimeoutExpired:
            return ("warn", svc_name,
                f"Git repo           '{svc_name}' timed out reaching {repo}")
        except FileNotFoundError:
            return ("skip", svc_name,
                "Git repo           git command not found, skipping repo checks")

    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = {
            pool.submit(_check_one_repo, name, build): name
            for name, build in build_services
        }
        for future in as_completed(futures):
            level, _name, msg = future.result()
            if level == "ok":
                result.ok(msg)
            elif level == "warn":
                result.warn(msg)
            elif level == "skip":
                result.warn(msg)
                return


def _check_docker_images(
    services: dict[str, Any],
    accessories: dict[str, Any],
    result: ValidationResult,
) -> None:
    """Check Docker image tag existence via registry API or docker manifest."""
    all_entries = {**services, **accessories}
    image_entries: list[tuple[str, str]] = []

    for name, entry in all_entries.items():
        if not isinstance(entry, dict):
            continue
        image = entry.get("image")
        if image and not entry.get("build"):
            image_entries.append((name, str(image)))

    if not image_entries:
        return  # no image services

    from concurrent.futures import ThreadPoolExecutor, as_completed

    # Check if skopeo or docker CLI is available for manifest inspection
    has_skopeo = _command_exists("skopeo")

    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = {
            pool.submit(_check_single_image, name, ref, has_skopeo): name
            for name, ref in image_entries
        }
        for future in as_completed(futures):
            level, msg = future.result()
            if level == "ok":
                result.ok(msg)
            else:
                result.warn(msg)


def _command_exists(cmd: str) -> bool:
    """Check if a command exists on the system."""
    try:
        proc = subprocess.run(
            ["which", cmd],
            capture_output=True,
            timeout=5,
        )
        return proc.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


def _check_single_image(
    svc_name: str,
    image_ref: str,
    has_skopeo: bool,
) -> tuple[str, str]:
    """Check a single Docker image tag. Returns (level, message)."""
    # Parse image reference into registry/repo:tag
    tag = "latest"
    image_name = image_ref
    if ":" in image_ref:
        # Handle port numbers in registry (e.g., registry:5000/image:tag)
        last_colon = image_ref.rfind(":")
        potential_tag = image_ref[last_colon + 1:]
        # If the part after the last colon looks like a tag (no slashes), split
        if "/" not in potential_tag:
            tag = potential_tag
            image_name = image_ref[:last_colon]

    if has_skopeo:
        try:
            proc = subprocess.run(
                ["skopeo", "inspect", "--raw", f"docker://{image_name}:{tag}"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if proc.returncode == 0:
                return ("ok", f"Image              '{svc_name}' {image_name}:{tag} exists")
            stderr = (proc.stderr or "").strip().split("\n")[0]
            return ("warn",
                f"Image              '{svc_name}' {image_name}:{tag} "
                f"not verified -- {stderr}")
        except subprocess.TimeoutExpired:
            return ("warn",
                f"Image              '{svc_name}' {image_name}:{tag} "
                f"registry check timed out")

    # Fallback: try Docker Hub API for Docker Hub images
    if _is_docker_hub_image(image_name):
        return _check_dockerhub_image(svc_name, image_name, tag)

    return ("warn",
        f"Image              '{svc_name}' {image_name}:{tag} "
        f"skipped (install skopeo to verify non-Hub images)")


def _is_docker_hub_image(image_name: str) -> bool:
    """Check if an image reference points to Docker Hub."""
    # Docker Hub images have no registry prefix, or use docker.io / registry.hub.docker.com
    if "/" not in image_name:
        return True  # e.g., "nginx" -> library/nginx on Docker Hub
    first_part = image_name.split("/")[0]
    # If first part has a dot or colon, it's a custom registry
    if "." in first_part or ":" in first_part:
        return False
    # Otherwise it's a Docker Hub org/repo (e.g., "library/nginx", "vaultwarden/server")
    return True


def _check_dockerhub_image(
    svc_name: str,
    image_name: str,
    tag: str,
) -> tuple[str, str]:
    """Check Docker Hub image tag existence via the registry API. Returns (level, message)."""
    import urllib.request
    import urllib.error

    # Normalize Docker Hub image name
    if "/" not in image_name:
        api_name = f"library/{image_name}"
    else:
        api_name = image_name

    url = f"https://registry.hub.docker.com/v2/repositories/{api_name}/tags/{tag}"

    try:
        req = urllib.request.Request(url, method="GET")
        req.add_header("Accept", "application/json")
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status == 200:
                return ("ok", f"Image              '{svc_name}' {image_name}:{tag} exists")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return ("warn",
                f"Image              '{svc_name}' {image_name}:{tag} "
                f"tag not found on Docker Hub")
        return ("warn",
            f"Image              '{svc_name}' {image_name}:{tag} "
            f"Docker Hub API error: HTTP {e.code}")
    except (urllib.error.URLError, OSError, TimeoutError):
        return ("warn",
            f"Image              '{svc_name}' {image_name}:{tag} "
            f"Docker Hub unreachable")

    return ("warn",
        f"Image              '{svc_name}' {image_name}:{tag} "
        f"could not verify")


# ── Registry configuration checks ────────────────────────────────────────

def _validate_registry_config(
    services_data: dict[str, Any],
    parsed_files: dict[str, Any],
    result: ValidationResult,
) -> None:
    """Validate docker_registries config and remote/push strategy requirements."""
    console.header("Registry Configuration")

    # Collect docker_registries from parsed files
    registries: list[dict[str, Any]] = []
    for _rel, data in parsed_files.items():
        if isinstance(data, dict) and "docker_registries" in data:
            registries = data["docker_registries"]
            break

    # Validate docker_registries entries
    if registries:
        if not isinstance(registries, list):
            result.fail(
                "Registry config    docker_registries must be a list"
            )
            return
        registry_domains: list[str] = []
        for i, entry in enumerate(registries):
            if not isinstance(entry, dict):
                result.fail(
                    f"Registry config    docker_registries[{i}] must be a mapping"
                )
                continue
            domain = entry.get("domain", "")
            if not domain:
                result.fail(
                    f"Registry config    docker_registries[{i}] missing 'domain'"
                )
            else:
                registry_domains.append(str(domain))
            if not entry.get("username"):
                result.warn(
                    f"Registry config    docker_registries[{i}] ({domain}) "
                    f"has no username -- anonymous access only"
                )
    else:
        registry_domains = []

    # Check remote/push strategy services have a matching registry
    services = services_data.get("services", {}) or {}
    remote_services = []
    for svc_name, svc in services.items():
        if not isinstance(svc, dict):
            continue
        build = svc.get("build")
        if isinstance(build, dict) and build.get("strategy") in ("remote", "push"):
            remote_services.append(svc_name)

    if remote_services and not registries:
        result.fail(
            f"Registry config    services ({', '.join(remote_services)}) use "
            f"strategy 'remote' but no docker_registries configured -- "
            f"add docker_registries to group_vars"
        )
    elif remote_services and registries:
        for svc_name in remote_services:
            image = services[svc_name].get("image", "")
            if not image:
                continue
            # Extract domain from image reference (everything before first /)
            image_domain = image.split("/")[0] if "/" in image else ""
            if image_domain and image_domain not in registry_domains:
                result.warn(
                    f"Registry config    services.{svc_name} image '{image}' "
                    f"references registry '{image_domain}' but no matching "
                    f"docker_registries entry found"
                )

    # Check Zot role has matching docker_registries entry
    zot_enabled = False
    zot_domain = ""
    for _rel, data in parsed_files.items():
        if not isinstance(data, dict):
            continue
        if "zot_enabled" in data and data["zot_enabled"]:
            zot_enabled = True
        if "zot_domain" in data and data["zot_domain"]:
            zot_domain = str(data["zot_domain"])

    if zot_enabled and zot_domain:
        if zot_domain not in registry_domains:
            result.fail(
                f"Registry config    zot_enabled is true and zot_domain is "
                f"'{zot_domain}' but no matching docker_registries entry — "
                f"add an entry with domain: {zot_domain} to docker_registries"
            )

    _reg_issues = (
        any("Registry config" in f for f in result.failed)
        or any("Registry config" in w for w in result.warnings)
    )
    if not _reg_issues:
        if not registries and not remote_services:
            result.ok("Registry config    no remote services, no registries configured")
        else:
            parts = []
            if registries:
                count = len(registries)
                parts.append(f"{count} registr{'ies' if count != 1 else 'y'} configured")
            if remote_services:
                parts.append(f"{len(remote_services)} remote service{'s' if len(remote_services) != 1 else ''}")
            result.ok(f"Registry config    {', '.join(parts)}")


# ── Deprecation checks ───────────────────────────────────────────────────

def _validate_deprecations(
    parsed_files: dict[str, Any],
    result: ValidationResult,
) -> None:
    """Warn about deprecated configuration variables."""
    console.header("Deprecations")

    has_deprecations = False

    _deprecated = {
        "app_build_strategy": "use per-service build.strategy or git_deploy_build_strategy instead",
        "docker_registry_org": "use docker_registries list in registry.yml instead",
        "docker_registry_username": "use docker_registries list in registry.yml instead",
        "docker_registry_token": "use docker_registries list in registry.yml instead",
    }

    for rel_path, data in parsed_files.items():
        if not isinstance(data, dict):
            continue
        for var_name, guidance in _deprecated.items():
            if var_name in data:
                result.warn(
                    f"Deprecated         {rel_path} sets '{var_name}' -- {guidance}"
                )
                has_deprecations = True

    if not has_deprecations:
        result.ok("Deprecations       no deprecated variables found")


# ── Netplan checks ──────────────────────────────────────────────────────

_PLACEHOLDER_MAC = "92:00:00:00:00:00"


def _validate_netplan(
    root: Path,
    env: str,
    parsed_files: dict[str, Any],
    result: ValidationResult,
) -> None:
    """Check netplan configuration when netplan_enabled is set."""
    console.header("Netplan")

    # Find netplan_enabled across all parsed files
    netplan_enabled = False
    has_network_yml = False
    netplan_vars: dict[str, Any] = {}

    for rel_path, data in parsed_files.items():
        if not isinstance(data, dict):
            continue
        if "netplan_enabled" in data:
            netplan_enabled = bool(data["netplan_enabled"])
        if "network.yml" in rel_path:
            has_network_yml = True
            netplan_vars.update(data)

    if not netplan_enabled:
        result.ok("Netplan             not enabled — skipped")
        return

    if not has_network_yml:
        result.warn(
            "Netplan             netplan_enabled is set but no network.yml found "
            f"— create group_vars/{env}/network.yml or run: bay server inspect {env}"
        )
        return

    has_errors = False
    for required in ("netplan_mac", "netplan_address", "netplan_gateway"):
        val = netplan_vars.get(required)
        if not val:
            result.fail(
                f"Netplan             {required} is not defined "
                f"— set it in group_vars/{env}/network.yml"
            )
            has_errors = True

    mac = netplan_vars.get("netplan_mac", "")
    if str(mac) == _PLACEHOLDER_MAC:
        result.warn(
            f"Netplan             netplan_mac is set to placeholder value ({_PLACEHOLDER_MAC}) "
            f"— run: bay server inspect {env}"
        )
    elif not has_errors:
        result.ok("Netplan             configuration looks good")


def _validate_headscale_oidc(
    parsed_files: dict[str, Any],
    result: ValidationResult,
) -> None:
    """Headscale OIDC must carry an enrolment allowlist.

    Headscale applies no allowlist of its own.  With an issuer configured
    and ``allowed_domains`` / ``allowed_users`` / ``allowed_groups`` all
    empty, every account the issuer will authenticate can enrol a node —
    with a public issuer that is the whole internet.  A mis-set allowlist
    is indistinguishable from an absent one at runtime, so this is a hard
    failure rather than a warning.

    Separately, warns when OIDC is on and ``headscale_acl_policy`` is
    undefined.  Bay ships no default ACL on purpose (a policy flips the
    whole tailnet to default-deny), but open enrolment into an allow-all
    tailnet is the combination worth naming out loud.
    """
    console.header("Headscale OIDC")

    issuer = ""
    allow: dict[str, list[Any]] = {
        "headscale_oidc_allowed_domains": [],
        "headscale_oidc_allowed_users": [],
        "headscale_oidc_allowed_groups": [],
    }
    acl_defined = False

    for _rel_path, data in parsed_files.items():
        if not isinstance(data, dict):
            continue
        if "headscale_oidc_issuer" in data:
            issuer = str(data.get("headscale_oidc_issuer") or "").strip()
        for key in allow:
            if key in data:
                val = data.get(key)
                allow[key] = list(val) if isinstance(val, (list, tuple)) else []
        if "headscale_acl_policy" in data and data.get("headscale_acl_policy"):
            acl_defined = True

    if not issuer:
        result.ok("Headscale OIDC      not enabled — skipped")
        return

    if not any(allow.values()):
        result.fail(
            "Headscale OIDC      headscale_oidc_issuer is set but no enrolment "
            "allowlist is configured — ANY account the issuer will authenticate "
            "can enrol a node onto your tailnet, and with a public issuer "
            "(Google, GitHub, …) that is the whole internet. Set at least one of "
            "headscale_oidc_allowed_domains, headscale_oidc_allowed_users, "
            "headscale_oidc_allowed_groups, then re-render the config with "
            "'bay deploy <env> --tags headscale'. Audit 'bay gateway nodes' for "
            "unexpected entries before you do."
        )
    else:
        _set = ", ".join(k.replace("headscale_oidc_", "") for k, v in allow.items() if v)
        result.ok(f"Headscale OIDC      enrolment allowlist set ({_set})")

    if not acl_defined:
        result.warn(
            "Headscale OIDC      headscale_oidc_issuer is set but "
            "headscale_acl_policy is undefined — the tailnet stays in Headscale's "
            "default ALLOW-ALL mode, so any node that does enrol reaches every "
            "node and port, including every 'access: vpn' service. Bay ships no "
            "default ACL on purpose (a policy flips the tailnet to default-deny "
            "and every required flow must be enumerated first). See "
            "docs/tailnet-ingress.md."
        )


# ── Core validation logic ────────────────────────────────────────────────

def _probe_token_scope(
    services: dict,
    vault_data: dict | None,
    result: ValidationResult,
) -> None:
    """Probe GitHub API to verify each build.token has sufficient scope.

    Only called when --check-token-scope is passed — never runs implicitly.
    Only probes services with strategy == 'remote' (or push) and build.token set.

    The resolved token value NEVER appears in any error message — only the
    vault key name is logged.
    """

    if vault_data is None:
        result.warn("Token scope check skipped — vault not decryptable")
        return

    console.header("Token Scope (GitHub API)")

    to_probe: list[tuple[str, str, str, str]] = []  # (svc_name, key_name, token_value, owner_repo)

    for svc_name, svc in services.items():
        if not isinstance(svc, dict):
            continue
        build = svc.get("build")
        if not isinstance(build, dict):
            continue
        token_ref = build.get("token")
        if not token_ref:
            continue

        # Only probe remote strategy services
        raw_strategy = build.get("strategy", "local")
        if raw_strategy not in ("remote", "push"):
            continue

        # Extract vault key name
        m = _TOKEN_RE.search(str(token_ref))
        if not m:
            continue
        key_name = m.group(1)
        token_value = vault_data.get(key_name)
        if not token_value:
            continue  # Already caught by _validate_build_tokens

        # Parse owner/repo from build.repo
        repo_url = build.get("repo", "")
        owner_repo: str | None = None
        if "github.com" in repo_url:
            # Handles: https://github.com/Org/repo.git  and  git@github.com:Org/repo.git
            if "://" in repo_url:
                # https://github.com/Org/repo.git
                after = repo_url.split("github.com/", 1)[-1]
            else:
                # git@github.com:Org/repo.git
                after = repo_url.split("github.com:", 1)[-1]
            # Strip .git suffix and trailing slashes
            owner_repo = after.rstrip("/").removesuffix(".git")
        else:
            result.warn(
                f"services.{svc_name}.build.token: cannot parse owner/repo from "
                f"'{repo_url}' — only github.com repos are probed"
            )
            continue

        to_probe.append((svc_name, key_name, token_value, owner_repo))

    if not to_probe:
        result.ok("Token scope         no remote build services to probe")
        return

    errors = False
    for svc_name, key_name, token_value, owner_repo in to_probe:
        api_url = f"https://api.github.com/repos/{owner_repo}"
        try:
            resp = requests.get(
                api_url,
                headers={"Authorization": f"Bearer {token_value}"},
                timeout=10,
            )
        except requests.RequestException as exc:
            result.warn(
                f"services.{svc_name}.build.token: GitHub API unreachable "
                f"({type(exc).__name__}) — skipping scope check"
            )
            continue

        if resp.status_code == 200:
            pass  # counted in summary at end of loop
        elif resp.status_code == 403:
            result.fail(
                f"services.{svc_name}.build.token: {key_name} lacks read access "
                f"to {owner_repo} (HTTP 403) — fine-grained PATs need "
                f"'Contents: Read'; classic PATs need 'repo' scope"
            )
            errors = True
        elif resp.status_code == 404:
            result.fail(
                f"services.{svc_name}.build.token: {key_name} cannot see "
                f"{owner_repo} (HTTP 404) — repo not found or token lacks read access"
            )
            errors = True
        else:
            result.fail(
                f"services.{svc_name}.build.token: GitHub API returned "
                f"HTTP {resp.status_code} for {owner_repo} — check token and repo URL"
            )
            errors = True

    if not errors:
        result.ok(f"Token scope         all {len(to_probe)} token(s) verified")



# ── Webhook health probe (opt-in) ─────────────────────────────────────────

def _probe_webhook_health(
    root: Path,
    env: str,
    services_data: dict[str, Any],
    parsed_files: dict[str, Any],
    result: ValidationResult,
) -> None:
    """Probe GitHub API for webhook health for all build services.

    Opt-in via ``--check-webhook-health``. Never runs automatically.

    For each service with ``build.repo`` pointing to github.com:
    - Finds the matching hook whose URL starts with the expected bay pattern.
    - Ignores non-bay hooks (Slack, CI, other tools) on the same repo.
    - Checks last_response.code (200 = ok, null = informational note, other = fail).
    - Flags orphan hooks that belong to this consumer's webhook_domain but have
      no matching service (safe to remove with ``bin/bay service prune-webhooks``).
    """
    from bay_cli.github_webhook import (
        find_orphan_hooks,
        list_repo_hooks,
        parse_repo_owner_name,
        probe_service_webhook,
        resolve_webhook_domain,
    )

    console.header("Webhook Health (GitHub API)")

    # 1. Resolve webhook_domain
    webhook_domain = resolve_webhook_domain(root, env)
    if webhook_domain is None:
        result.warn(
            "Webhook health     webhook_domain not configured — skipping probe"
        )
        return

    # 2. Resolve github_admin_token from vault
    github_token: str | None = None
    token_vault_path = ""
    for rel_path, data in parsed_files.items():
        if rel_path.endswith("secrets.yml") and isinstance(data, dict):
            vault_secrets = (
                data.get("secrets", data)
                if isinstance(data.get("secrets"), dict)
                else data
            )
            github_token = vault_secrets.get("github_admin_token")
            token_vault_path = rel_path
            break

    if not github_token:
        result.warn(
            f"Webhook health     github_admin_token not in vault "
            f"({token_vault_path or 'group_vars/<env>/secrets.yml'}) — "
            f"skipping probe. Add a read-only GitHub PAT with 'repo' scope "
            f"to enable webhook health checks."
        )
        return

    # 3. Collect services with github.com build repos
    services = services_data.get("services", {}) or {}
    build_services: list[tuple[str, str, str]] = []  # (svc_name, owner, name)

    for svc_name, svc in services.items():
        if not isinstance(svc, dict):
            continue
        build = svc.get("build")
        if not isinstance(build, dict):
            continue
        repo_url = build.get("repo", "")
        parsed = parse_repo_owner_name(str(repo_url))
        if parsed is None:
            continue
        owner, repo_name = parsed
        build_services.append((svc_name, owner, repo_name))

    if not build_services:
        result.ok("Webhook health     no GitHub build services configured")
        return

    # 4. Probe each repo (cached per unique owner/repo)
    cache: dict[str, list[dict[str, Any]]] = {}
    orphan_reported: set[str] = set()

    for svc_name, owner, repo_name in build_services:
        repo_key = f"{owner}/{repo_name}"
        hook_url_pattern = f"https://{webhook_domain}/webhook/{svc_name}"

        try:
            hooks = list_repo_hooks(owner, repo_name, github_token, _cache=cache)
        except requests.HTTPError as exc:
            status_code = exc.response.status_code if exc.response is not None else 0
            if status_code in (401, 403):
                result.fail(
                    f"Webhook health     github_admin_token lacks read access to "
                    f"hooks on {repo_key} — token needs 'repo' scope (HTTP {status_code})"
                )
            else:
                result.warn(
                    f"Webhook health     GitHub API error for {repo_key}: "
                    f"HTTP {status_code} — skipping"
                )
            continue
        except requests.RequestException as exc:
            result.warn(
                f"Webhook health     GitHub API unreachable for {repo_key} "
                f"({type(exc).__name__}) — skipping"
            )
            continue

        # Per-service hook probe
        probe = probe_service_webhook(svc_name, hook_url_pattern, hooks)
        if probe.status == "ok":
            result.ok(f"Webhook health     {probe.message}")
        elif probe.status == "note":
            result.warn(f"Webhook health     note: {probe.message}")
        elif probe.status == "fail":
            result.fail(f"Webhook health     {probe.message}")

        # Orphan scan — once per repo
        if repo_key not in orphan_reported:
            orphan_reported.add(repo_key)
            service_names = set(services.keys())
            orphans = find_orphan_hooks(hooks, webhook_domain, service_names)
            for orphan in orphans:
                orphan_url = orphan.get("config", {}).get("url", "(unknown)")
                result.warn(
                    f"Webhook health     orphan hook on {repo_key}: {orphan_url} "
                    f"— run 'bin/bay service prune-webhooks {repo_key}' to remove"
                )


# ── Identifier safety ────────────────────────────────────────────────────

_SERVICE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
_SQL_IDENT_RE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_ROUTE_RE = re.compile(r"^/[A-Za-z0-9._~%+/*-]*$")


def _validate_identifier_safety(
    services_data: dict[str, Any],
    result: ValidationResult,
) -> None:
    """Every consumer-supplied name that Bay interpolates into SQL or a shell.

    The schema (services.schema.json) already rejects these shapes, but it
    reports them as a JSON-pointer path with a raw regex, which is unhelpful
    at 2 a.m. This restates the same contract in words, names the offender,
    and adds the one rule a regex cannot express: a bare ``/`` in
    ``public_routes`` is syntactically fine and semantically catastrophic —
    it makes the entire ``access: vpn`` service public.

    Where these values land:

      * service / accessory key -> container name, Traefik router name,
        the default database and role name, and a shell word in rebuild.sh
      * database.name / database.user -> psql identifiers
      * env.clear / env.secret keys -> env-file lines and, historically,
        shell parameter expansions
      * public_routes / vpn_routes -> inside a Traefik backquoted string,
        which has no escape sequence for a backtick
    """
    console.header("Identifier Safety")

    issues = 0

    def _fail(msg: str) -> None:
        nonlocal issues
        issues += 1
        result.fail(msg)

    def _check_env(kind: str, name: str, block: Any) -> None:
        if not isinstance(block, dict):
            return
        clear = block.get("clear")
        if isinstance(clear, dict):
            for key in clear:
                if not _ENV_KEY_RE.match(str(key)):
                    _fail(
                        f"{kind}.{name}.env.clear: environment variable name "
                        f"'{key}' is not a POSIX name "
                        f"(^[A-Za-z_][A-Za-z0-9_]*$)"
                    )
        secret = block.get("secret")
        secret_keys: list[Any] = []
        if isinstance(secret, dict):
            secret_keys = list(secret.keys())
        elif isinstance(secret, list):
            secret_keys = list(secret)
        for key in secret_keys:
            if not _ENV_KEY_RE.match(str(key)):
                _fail(
                    f"{kind}.{name}.env.secret: environment variable name "
                    f"'{key}' is not a POSIX name "
                    f"(^[A-Za-z_][A-Za-z0-9_]*$)"
                )

    def _check_database(kind: str, name: str, db: Any) -> None:
        if not isinstance(db, dict):
            return
        for field, pattern, label in (
            ("name", _SQL_IDENT_RE, "database name"),
            ("user", _SQL_IDENT_RE, "database user"),
            ("accessory", _SERVICE_NAME_RE, "accessory reference"),
        ):
            value = db.get(field)
            if value is None:
                continue
            if not pattern.match(str(value)):
                _fail(
                    f"{kind}.{name}.database.{field}: {label} '{value}' is "
                    f"not a safe identifier — it is interpolated into SQL run "
                    f"as the postgres superuser"
                )

    def _check_routes(kind: str, name: str, svc: dict[str, Any]) -> None:
        for field in ("public_routes", "vpn_routes"):
            routes = svc.get(field)
            if not isinstance(routes, list):
                continue
            for route in routes:
                text = str(route)
                if not _ROUTE_RE.match(text):
                    _fail(
                        f"{kind}.{name}.{field}: route '{text}' contains a "
                        f"character Traefik cannot escape inside a rule "
                        f"(allowed: ^/[A-Za-z0-9._~%+/*-]*$)"
                    )
                elif field == "public_routes" and text == "/":
                    _fail(
                        f"services.{name}.public_routes: a bare '/' exempts "
                        f"every path from the VPN restriction, which makes "
                        f"the whole service public. If that is what you want, "
                        f"set access: public deliberately; otherwise list the "
                        f"specific paths."
                    )

    for kind, key in (("services", "services"), ("accessories", "accessories")):
        entries = services_data.get(key) or {}
        if not isinstance(entries, dict):
            continue
        for name, entry in entries.items():
            if str(name).startswith("_"):
                continue
            if not _SERVICE_NAME_RE.match(str(name)):
                _fail(
                    f"{kind}: name '{name}' is not a safe identifier "
                    f"(^[a-z0-9][a-z0-9_-]{{0,62}}$) — it becomes a container "
                    f"name, a Traefik router name and a shell word"
                )
            if not isinstance(entry, dict):
                continue
            _check_env(kind, str(name), entry.get("env"))
            _check_database(kind, str(name), entry.get("database"))
            if kind == "services":
                _check_routes(kind, str(name), entry)

    if issues == 0:
        result.ok(
            "Identifier safety  all names, env keys and routes are safe to "
            "interpolate"
        )


def _validate_config_files(
    root: Path,
    services_data: dict[str, Any],
    result: ValidationResult,
) -> None:
    """Every ``config_files`` entry must exist under the consumer's ``files/``.

    deploy_stack copies each entry from ``files/<entry>`` to
    ``<stack_dir>/config/<entry>``, and a service that mounts a config it
    never received starts and immediately dies. This used to be found on
    the server, mid-deploy; the default (Gatus) project shipped that way.
    """
    console.header("Config Files")

    missing: list[str] = []
    total = 0
    for kind in ("services", "accessories"):
        entries = services_data.get(kind) or {}
        if not isinstance(entries, dict):
            continue
        for name, entry in entries.items():
            if not isinstance(entry, dict):
                continue
            config_files = entry.get("config_files")
            if not isinstance(config_files, list):
                continue
            for rel in config_files:
                total += 1
                expected = root / "files" / str(rel)
                if not expected.is_file():
                    missing.append(
                        f"{kind}.{name}.config_files: '{rel}' has no file at "
                        f"{expected.relative_to(root) if expected.is_relative_to(root) else expected}"
                        f" -- create it, or drop the entry"
                    )

    if missing:
        for m in missing:
            result.fail(f"Config files       {m}")
    elif total:
        result.ok(f"Config files       all {total} declared file(s) present")
    else:
        result.ok("Config files       no config_files declared")


def _validate_admin_ssh_keys(
    parsed_files: dict[str, Any],
    result: ValidationResult,
) -> None:
    """Refuse a user in the ``ssh-access`` group with no SSH key.

    Provisioning runs the ``users`` role first, then hardening that sets
    ``PermitRootLogin no`` and ``PasswordAuthentication no``. A user whose
    ``keys`` list is empty is skipped silently by the role (it uses
    ``subelements(..., skip_missing=True)``), so the run stays green and
    the server ends up with no way in at all.
    """
    console.header("Admin Access")

    users_data: list[Any] | None = None
    users_rel = ""
    for rel_path, data in parsed_files.items():
        if isinstance(data, dict) and isinstance(data.get("users"), list):
            users_data = data["users"]
            users_rel = rel_path
            break

    if users_data is None:
        result.warn("Admin SSH keys     no 'users' list found, skipping")
        return

    keyless: list[str] = []
    checked = 0
    for user in users_data:
        if not isinstance(user, dict):
            continue
        groups = user.get("groups") or []
        if not isinstance(groups, list) or "ssh-access" not in groups:
            continue
        checked += 1
        keys = user.get("keys")
        if not isinstance(keys, list) or not [k for k in keys if str(k).strip()]:
            keyless.append(str(user.get("name", "<unnamed>")))

    if keyless:
        for name in keyless:
            result.fail(
                f"Admin SSH keys     {users_rel}: user '{name}' is in the "
                f"ssh-access group with no keys -- provisioning disables root "
                f"login and password auth, so this locks you out of the server"
            )
    elif checked:
        result.ok(f"Admin SSH keys     {checked} ssh-access user(s) have keys")
    else:
        result.warn("Admin SSH keys     no user is in the ssh-access group")


#: Values that parse as a string but are not an address anyone owns. The
#: example tree ships the first one so a copied config fails loudly here
#: instead of silently at the ACME challenge.
_LETSENCRYPT_PLACEHOLDERS = ("change-me@example.com", "changeme@example.com", "you@example.com")


def _validate_letsencrypt_email(
    parsed_files: dict[str, Any],
    result: ValidationResult,
) -> None:
    """Refuse an empty or placeholder ``letsencrypt_email``.

    Traefik uses it unconditionally for the ACME resolver -- there is no
    ``acme_enabled`` opt-out in the framework, so every routed service asks
    Let's Encrypt for a certificate on the first deploy. An empty value is
    always a broken SSL config, never a deliberate choice.
    """
    console.header("SSL Certificate")

    value: Any = None
    origin = ""
    for rel, data in (parsed_files or {}).items():
        if isinstance(data, dict) and "letsencrypt_email" in data:
            value = data["letsencrypt_email"]
            origin = rel

    if not origin:
        result.fail(
            "letsencrypt_email   not set in any group_vars file -- Traefik needs "
            "it for the ACME resolver, so SSL certificates will fail"
        )
        return

    text = str(value).strip() if value is not None else ""
    if not text:
        result.fail(
            f"letsencrypt_email   {origin}: empty -- Traefik needs it for the "
            f"ACME resolver, so SSL certificates will fail"
        )
        return
    if text.lower() in _LETSENCRYPT_PLACEHOLDERS:
        result.fail(
            f"letsencrypt_email   {origin}: still the placeholder {text!r} -- "
            f"replace it with an address you can receive mail on"
        )
        return
    if "@" not in text:
        result.fail(f"letsencrypt_email   {origin}: {text!r} is not an email address")
        return

    result.ok(f"letsencrypt_email   {text} ({origin})")


def run_validation(
    root: Path,
    env: str,
    *,
    bay_dir: Path | None = None,
    show_banner: bool = True,
    check_token_scope: bool = False,
    check_webhook_health: bool = False,
) -> ValidationResult:
    """Run all validation checks and return the result.

    This is the shared entry point used by both ``bay validate`` and
    ``bay deploy`` (pre-deploy gate).

    Args:
        root: Consumer project root directory.
        env: Target environment (e.g., ``"production"``).
        bay_dir: Path to the ``.argo/`` framework directory.  When  # legacy-argo: .argo/.argo-version convention, rename lands in S03
            ``None`` the version drift check is skipped silently (for
            unit tests that don't set up a framework directory).
        show_banner: Whether to print the Bay banner header.
        check_token_scope: When True, probe the GitHub API for each
            remote-strategy service's build.token to verify scope.
            Never runs automatically — caller must opt in.
        check_webhook_health: When True, probe GitHub webhook health for
            all build services. Never runs automatically — caller must opt in.

    Returns:
        A ValidationResult with all checks applied.
    """
    if show_banner:
        console.show_banner(subtitle="Validate")

    result = ValidationResult()

    # 0. Framework version drift check
    if bay_dir is not None:
        console.header("Framework Version")
        _validate_version_drift(root, bay_dir, result)

    # 1. YAML syntax validation
    parsed = _validate_yaml_files(root, env, result)

    # 2. Inventory validation
    _validate_inventory(root, env, result)

    # 3. Schema validation (only if YAML parsed OK)
    services_data = _validate_services_schema(root, parsed, result)

    # 3b. Identifier safety (names that reach SQL and shells)
    if services_data is not None:
        _validate_identifier_safety(services_data, result)

    # 3c. Declared config files exist in the consumer's files/
    if services_data is not None:
        _validate_config_files(root, services_data, result)

    # 3d. The admin account can still get in after hardening
    _validate_admin_ssh_keys(parsed, result)

    # 4. Backup configuration consistency
    if services_data is not None:
        _validate_backup_config(services_data, parsed, result)

    # 5. Registry configuration
    if services_data is not None:
        _validate_registry_config(services_data, parsed, result)

    # 5b. Alert recipients
    _validate_alert_recipients(parsed, result)

    # 5c. Let's Encrypt email (Traefik has no ACME opt-out)
    _validate_letsencrypt_email(parsed, result)

    # 6. Connectivity and reference checks (only if schema passed)
    if services_data is not None:
        _validate_connectivity(root, services_data, parsed, result)

    # 7. Deprecation warnings
    _validate_deprecations(parsed, result)

    # 8. Netplan configuration
    _validate_netplan(root, env, parsed, result)

    # 8b. Headscale OIDC enrolment allowlist
    _validate_headscale_oidc(parsed, result)

    # 9. Token scope probe (opt-in, never runs automatically)
    if check_token_scope and services_data is not None:
        services = services_data.get("services", {}) or {}
        # Resolve vault_data (same extraction as _validate_build_tokens)
        _scope_vault_data: dict[str, Any] | None = None
        for _rel_path, _data in parsed.items():
            if _rel_path.endswith("secrets.yml") and isinstance(_data, dict):
                _scope_vault_data = _data.get("secrets", _data) if isinstance(_data.get("secrets"), dict) else _data
                break
        _probe_token_scope(services, _scope_vault_data, result)

    # 10. Webhook health probe (opt-in, never runs automatically)
    if check_webhook_health and services_data is not None:
        _probe_webhook_health(root, env, services_data, parsed, result)

    return result


# ── Main command ─────────────────────────────────────────────────────────


def _default_env(root: Path) -> str:
    """Pick a sensible default env based on hosts/ contents.

    - If hosts/production exists, use it (covers every multi-env and
      production-default consumer).
    - Otherwise, if hosts/ has exactly one entry (file or dir), use it
      (covers single-env consumers like sandbox).
    - Fall back to "production" so the downstream error message stays
      consistent for consumers with no hosts/ at all.
    """
    hosts_dir = root / "hosts"
    if (hosts_dir / "production").exists():
        return "production"
    if hosts_dir.is_dir():
        entries = [p.name for p in hosts_dir.iterdir() if not p.name.startswith(".")]
        if len(entries) == 1:
            return entries[0]
    return "production"


def validate(
    env: str | None = typer.Option(
        None,
        "--env",
        "-e",
        help=(
            "Target environment. Defaults to 'production' if hosts/production "
            "exists, otherwise the single env under hosts/ if there is one."
        ),
    ),
    check_token_scope: bool = typer.Option(
        False,
        "--check-token-scope",
        help=(
            "Probe the GitHub API for each remote-strategy service with "
            "build.token set. Verifies the token has sufficient scope "
            "('Contents: Read' for fine-grained PATs, 'repo' for classic). "
            "Opt-in only — makes outbound HTTPS calls to api.github.com."
        ),
    ),
    check_config_age: bool = typer.Option(
        False,
        "--check-config-age",
        help=(
            "SSH to the target host and verify git_deploy config files "
            "(rebuild.sh, webhook configs) are newer than the last git_deploy "
            "run. Warns on stale configs caused by partial deploys "
            "(e.g., --tags deploy_stack without git_deploy)."
        ),
    ),
    check_webhook_health: bool = typer.Option(
        False,
        "--check-webhook-health",
        help=(
            "Probe GitHub API for webhook health — checks each build service has "
            "a correctly-configured webhook and flags orphan hooks. "
            "Opt-in only — makes outbound HTTPS calls to api.github.com."
        ),
    ),
    region: str | None = typer.Option(
        None,
        "--region",
        "-r",
        help="Limit --check-config-age to a specific region (ansible --limit).",
    ),
) -> None:
    """Validate configuration files before deploying.

    Runs automatically before every deploy; run it standalone to iterate
    on config. Checks framework version drift, YAML syntax, the services
    schema, inventory, backup/registry config, cross-references, vault
    keys, deprecations, and netplan. The GitHub probes and the remote
    config-age check are opt-in flags — they make network calls.

    Examples:

        bin/bay validate
        bin/bay validate --env testing
        bin/bay validate --check-token-scope --check-webhook-health
        bin/bay validate --check-config-age --region eu
    """
    bay_dir = paths.find_bay_dir()
    root = paths.consumer_root(bay_dir)

    if env is None:
        env = _default_env(root)

    result = run_validation(root, env, bay_dir=bay_dir, check_token_scope=check_token_scope, check_webhook_health=check_webhook_health)

    # ── Optional remote config-age check ────────────────────────────────
    if check_config_age:
        from bay_cli.commands.ops import _resolve_target_host
        limit = _resolve_target_host(bay_dir, region) if region else None
        _check_config_age_remote(root, env, bay_dir, result, limit=limit)

    # ── Summary ──────────────────────────────────────────────────────
    console.header("Summary")

    if console.is_json_mode():
        console.emit_result(result.to_dict(), command="validate")
        if result.total_issues:
            raise typer.Exit(code=1)
        return

    total = len(result.passed) + len(result.failed) + len(result.warnings)
    console.console.print(
        f"  {len(result.passed)} passed, "
        f"{len(result.failed)} failed, "
        f"{len(result.warnings)} warnings "
        f"({total} checks)"
    )
    console.console.print()

    if result.total_issues:
        console.error(
            f"{result.total_issues} issue{'s' if result.total_issues != 1 else ''} found. "
            "Fix the above before deploying."
        )
        raise typer.Exit(code=1)
    else:
        console.success("All validation checks passed!")
