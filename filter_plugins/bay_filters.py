"""Custom Ansible filters for the Bay framework."""

import hashlib
import importlib.util
import re
from pathlib import Path

# The alert adapters are loaded from their canonical location rather than
# reimplemented here. The same file is included into docker-monitor.py.j2 and
# imported by the webhook receiver, so the control-node `uri` tasks that send
# "deploy complete"/"deploy failed" adapt their bodies with identical rules.
# Two implementations that drift is the failure mode this avoids.
_ALERT_MODULE_PATH = (
    Path(__file__).resolve().parent.parent
    / "roles"
    / "alert_channel"
    / "files"
    / "bay_alert.py"
)
_alert_spec = importlib.util.spec_from_file_location(
    "bay_alert", _ALERT_MODULE_PATH
)
_bay_alert = importlib.util.module_from_spec(_alert_spec)
_alert_spec.loader.exec_module(_bay_alert)

bay_alert_body = _bay_alert.bay_alert_body
bay_alert_content_type = _bay_alert.bay_alert_content_type
bay_desugar_legacy = _bay_alert.bay_desugar_legacy
bay_recipient_alert_ids = _bay_alert.bay_recipient_alert_ids
bay_recipient_target = _bay_alert.bay_recipient_target
bay_transform_body = _bay_alert.bay_transform_body

# The alert registry is the render-time input to routing: Jinja resolves each
# recipient's min_level against it and emits a flat `case` per recipient, so
# the host never compares levels. Loaded once at import — it is a framework
# file, not consumer config, and cannot change mid-run.
_REGISTRY_PATH = Path(__file__).resolve().parent.parent / "alerts" / "registry.yml"


def _load_registry():
    """Read alerts/registry.yml. Never raises — a broken registry must not
    take down a deploy, and an empty one simply routes nothing new."""
    try:
        from ruamel.yaml import YAML

        with _REGISTRY_PATH.open() as handle:
            data = YAML(typ="safe").load(handle)
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001 - fail-open by design
        return {}


_ALERT_REGISTRY = _load_registry()


def bay_alert_registry(_=None):
    """The parsed alert registry, for templates and the CLI."""
    return _ALERT_REGISTRY


def bay_alert_recipients(
    recipients,
    webhook_url="",
    webhook_format="campfire",
    telegram_token="",
    telegram_chat="",
):
    """Resolve explicit recipients + legacy vars into one normalized list."""
    return bay_desugar_legacy(
        recipients=recipients,
        webhook_url=webhook_url,
        webhook_format=webhook_format,
        telegram_token=telegram_token,
        telegram_chat=telegram_chat,
    )


def bay_alert_recipient(recipient):
    """Normalize one recipient: adapter defaults + webhook preset resolution."""
    return _bay_alert.bay_normalize_recipient(recipient)


def bay_alert_ids_for(recipient, disabled=None, enabled=None):
    """The alert IDs this recipient receives — the render-time routing table.

    `disabled` is alerts_disabled (force off), `enabled` is alerts_enabled
    (force on, and the only way back for an alert the registry ships as
    enabled_by_default: false).
    """
    return bay_recipient_alert_ids(_ALERT_REGISTRY, recipient, disabled, enabled)


_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def bay_env_name(name):
    """Validate a shell environment-variable NAME before it is rendered.

    `_notify.sh.j2` looks recipient credentials up by name at run time. The
    name comes from consumer config (`token_env`, `chat_id_env`, `url_env`),
    and the emitters that include the snippet run from root cron and root
    systemd units, so a name is as good as code if it reaches a parameter
    expansion unchecked. Fail the render, not the host.
    """
    text = str(name)
    if not _ENV_NAME_RE.match(text):
        raise ValueError(
            f"invalid environment variable name {text!r}: must match "
            "^[A-Za-z_][A-Za-z0-9_]*$ (alert recipient token_env / chat_id_env "
            "/ url_env)"
        )
    return text


def bay_env_value(value):
    """Escape a value for a docker `env_file` line, refusing what cannot be.

    env.j2 doubles `$` so Docker Compose does not interpolate it. A NEWLINE
    has no escape in the env-file format at all: the parser reads one
    KEY=VALUE per line, so a secret containing a newline used to inject a
    second, attacker-chosen environment assignment into the container.
    There is nothing to escape it to, so it is refused at render time.
    """
    text = str(value)
    if "\n" in text or "\r" in text:
        raise ValueError(
            "environment value contains a newline: docker env files are one "
            "KEY=VALUE per line, so this would inject a second assignment. "
            "Re-encode the value (base64) or drop the newline."
        )
    return text.replace("$", "$$")


class FilterModule:
    """Bay filter plugins."""

    def filters(self):
        return {
            "parse_env_file": parse_env_file,
            "to_docker_networks": to_docker_networks,
            "bay_traefik_labels": bay_traefik_labels,
            "bay_traefik_global_labels": bay_traefik_global_labels,
            "bay_watchtower_labels": bay_watchtower_labels,
            "bay_healthcheck": bay_healthcheck,
            "bay_prefix_volumes": bay_prefix_volumes,
            "bay_repo_slug": bay_repo_slug,
            "bay_repo_groups": bay_repo_groups,
            "bay_build_dedup_map": bay_build_dedup_map,
            "bay_token_url": bay_token_url,
            "bay_hexkey": bay_hexkey,
            "bay_image_consumers": bay_image_consumers,
            "bay_image_region_map": bay_image_region_map,
            "bay_spec_hash": bay_spec_hash,
            "bay_port_binding_tuple": bay_port_binding_tuple,
            "bay_port_spec_tuple": bay_port_spec_tuple,
            "bay_alert_body": bay_alert_body,
            "bay_alert_content_type": bay_alert_content_type,
            "bay_alert_recipients": bay_alert_recipients,
            "bay_alert_ids_for": bay_alert_ids_for,
            "bay_alert_recipient": bay_alert_recipient,
            "bay_alert_registry": bay_alert_registry,
            "bay_recipient_target": bay_recipient_target,
            "bay_transform_body": bay_transform_body,
            "bay_gateway_bind_ip": bay_gateway_bind_ip,
            "bay_env_name": bay_env_name,
            "bay_env_value": bay_env_value,
        }


# ── Port-drift detection helpers ──────────────────────────────────────────
#
# Normalize both sides of the "is the running container bound where we want
# it?" comparison in container_lifecycle/tasks/deploy_accessory.yml. Both
# return strings "<host_ip>:<host_port>".
#
# bay_port_binding_tuple accepts a single entry from
#   _acc_info.container.HostConfig.PortBindings (a dict with HostIp/HostPort
#   keys). An empty HostIp defaults to "0.0.0.0" — Docker's implicit
#   all-interfaces bind.
# bay_port_spec_tuple accepts a compose-style port string ("5432:5432",
#   "127.0.0.1:5432:5432", or "100.64.0.1:5432:5432"). A spec with no
#   host-IP prefix defaults to "0.0.0.0" so it compares correctly against
#   a running container Docker bound to 0.0.0.0.
#
# Previously, this comparison was inlined in deploy_accessory.yml with a
# jinja filter chain that stripped the port and normalized missing IPs to
# "0.0.0.0" via regex. That silently false-equalled "5432:5432" (no prefix)
# with a running 0.0.0.0 binding — so a 127.0.0.1→100.64.0.1 migration
# via `expose: tailnet` never triggered container recreation.


def bay_port_binding_tuple(entry):
    """Convert a PortBindings entry dict to '<ip>:<port>'.

    {"HostIp": "100.64.0.1", "HostPort": "5432"} → "100.64.0.1:5432"
    {"HostIp": "",            "HostPort": "5432"} → "0.0.0.0:5432"
    """
    if not isinstance(entry, dict):
        return ""
    host_ip = (entry.get("HostIp") or "").strip() or "0.0.0.0"
    host_port = str(entry.get("HostPort") or "").strip()
    return f"{host_ip}:{host_port}"


def bay_port_spec_tuple(spec):
    """Convert a compose-style port spec string to '<ip>:<host_port>'.

    "5432:5432"               → "0.0.0.0:5432"
    "127.0.0.1:5432:5432"     → "127.0.0.1:5432"
    "100.64.0.1:5432:5432"    → "100.64.0.1:5432"
    5432                      → "0.0.0.0:5432"  (int coerced)
    ""                        → ""
    None                      → ""
    """
    if spec is None:
        return ""
    s = str(spec).strip()
    if not s:
        return ""
    parts = s.split(":")
    if len(parts) == 1:
        return f"0.0.0.0:{parts[0]}"
    if len(parts) == 2:
        return f"0.0.0.0:{parts[0]}"
    return f"{parts[0]}:{parts[1]}"


# ── Existing filters ────────────────────────────────────────────────────


def parse_env_file(content):
    """Parse KEY=VALUE env file content into a dict.

    Handles comments (#), blank lines, and values containing '='.
    """
    env = {}
    for line in content.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            # Unescape $$ -> $ (env.j2 double-escapes for Docker Compose
            # variable interpolation; Docker API expects literal values)
            env[key.strip()] = value.replace("$$", "$")
    return env


def bay_prefix_volumes(volumes, stack_name):
    """Prefix named volumes with stack_name to match Docker Compose behavior.

    Compose creates volumes as {project}_{volume_name}. Direct container
    management must use the same names to preserve existing data.

    Named volumes (no leading / or .) get prefixed.
    Bind mounts (starting with / or .) are left unchanged.
    """
    result = []
    for vol in volumes or []:
        parts = vol.split(":")
        name = parts[0]
        if name and not name.startswith("/") and not name.startswith("."):
            parts[0] = f"{stack_name}_{name}"
        result.append(":".join(parts))
    return result


def to_docker_networks(names):
    """Convert network name list to community.docker.docker_container format.

    ['services'] -> [{'name': 'services'}]
    """
    return [{"name": n} for n in (names or [])]


# ── Traefik label generation ────────────────────────────────────────────


def bay_traefik_labels(svc, name, config):
    """Generate Traefik labels for a service container.

    Args:
        svc: Service config dict from services.yml (piped value)
        name: Service name
        config: Dict of traefik config variables (defaults, feature flags)

    Returns:
        Dict of label key-value pairs (all string values)
    """
    labels = {
        "traefik.enable": "true",
        "traefik.docker.network": config.get("traefik_docker_network", "services"),
    }

    mw = svc.get("middleware", {})
    public_mw, vpn_mw = _compute_middleware_chains(labels, name, mw, config)
    _add_middleware_labels(labels, name, mw, config)
    _add_router_labels(labels, name, svc, public_mw, vpn_mw, config)

    return labels


def _compute_middleware_chains(labels, name, mw, config):
    """Compute public and VPN middleware chain lists.

    If the service overrides security_headers or compress, a custom
    per-service chain is created. Otherwise uses the global chains.

    Returns (public_mw, vpn_mw) lists.
    """
    svc_headers = mw.get("security_headers", True)
    svc_compress = mw.get("compress", True)
    needs_custom_chain = (not svc_headers) or (not svc_compress)

    # Per-service middleware names
    _MW_KEYS = [
        ("rate_limit", "ratelimit"),
        ("in_flight_req", "inflightreq"),
        ("basic_auth", "basicauth"),
        ("circuit_breaker", "circuitbreaker"),
        ("retry", "retry"),
    ]
    per_svc_mw = [f"{name}-{suffix}" for key, suffix in _MW_KEYS if key in mw]

    if needs_custom_chain:
        # Build custom chain members
        members = []
        if svc_headers and _bool(
            config.get("traefik_security_headers_enabled", True)
        ):
            members.append("secure-headers")
        if svc_compress and _bool(config.get("traefik_compress_enabled", True)):
            members.append("compress")
        if _bool(config.get("traefik_error_pages_enabled", False)):
            members.append("errors")
        labels[
            f"traefik.http.middlewares.{name}-chain.chain.middlewares"
        ] = ",".join(members)
        return [f"{name}-chain"] + per_svc_mw, [f"{name}-chain", "vpn-only"] + per_svc_mw

    return ["public-chain"] + per_svc_mw, ["vpn-chain"] + per_svc_mw


def _add_middleware_labels(labels, name, mw, config):
    """Add per-service middleware definition labels (rate_limit, basic_auth, etc.)."""
    if "rate_limit" in mw:
        rl = mw["rate_limit"]
        pfx = f"traefik.http.middlewares.{name}-ratelimit.ratelimit"
        labels[f"{pfx}.average"] = str(
            rl.get("average", config.get("traefik_rate_limit_average", 100))
        )
        labels[f"{pfx}.burst"] = str(
            rl.get("burst", config.get("traefik_rate_limit_burst", 50))
        )
        labels[f"{pfx}.period"] = str(
            rl.get("period", config.get("traefik_rate_limit_period", "1s"))
        )
        labels[f"{pfx}.sourceCriterion.ipStrategy.depth"] = "1"

    if "in_flight_req" in mw:
        ifr = mw["in_flight_req"]
        pfx = f"traefik.http.middlewares.{name}-inflightreq.inflightreq"
        labels[f"{pfx}.amount"] = str(
            ifr.get("amount", config.get("traefik_in_flight_req_amount", 100))
        )
        labels[f"{pfx}.sourceCriterion.ipStrategy.depth"] = "1"

    if "basic_auth" in mw:
        ba = mw["basic_auth"]
        pfx = f"traefik.http.middlewares.{name}-basicauth.basicauth"
        if "credentials" in ba:
            entries = []
            for cred in ba["credentials"]:
                hashed = bay_basic_auth_hash(
                    config.get("stack_name", "bay"),
                    name,
                    cred["username"],
                    cred["password"],
                )
                entries.append(f"{cred['username']}:{hashed}")
            users_str = ",".join(entries)
            labels[f"{pfx}.users"] = users_str
        elif "users" in ba:
            users_str = ",".join(ba["users"])
            labels[f"{pfx}.users"] = users_str
        if "realm" in ba:
            labels[f"{pfx}.realm"] = ba["realm"]
        labels[f"{pfx}.removeheader"] = str(
            ba.get("removeheader", True)
        ).lower()

    if "circuit_breaker" in mw:
        cb = mw["circuit_breaker"]
        labels[
            f"traefik.http.middlewares.{name}-circuitbreaker.circuitbreaker.expression"
        ] = cb.get(
            "expression",
            config.get("traefik_circuit_breaker_expression", "NetworkErrorRatio() > 0.5"),
        )

    if "retry" in mw:
        rt = mw["retry"]
        labels[
            f"traefik.http.middlewares.{name}-retry.retry.attempts"
        ] = str(rt.get("attempts", config.get("traefik_retry_attempts", 4)))


def _add_router_labels(labels, name, svc, public_mw, vpn_mw, config=None):
    """Add router and service labels based on access mode."""
    access = svc.get("access", "public")
    port = str(svc.get("ports", {}).get("internal", 80))
    domains = svc.get("domains", [])

    # Entrypoints per router class. Mirror _service.j2: VPN routers bind
    # `vpn_entrypoints`, public routers bind `public_entrypoints` — both
    # default to "websecure" so non-split hosts render byte-identically.
    # On a `traefik_split_entrypoints` host these carry "websecure_tailnet"
    # so a VPN router stays reachable on the tailnet listener.
    config = config or {}
    vpn_ep = config.get("vpn_entrypoints", "websecure")
    public_ep = config.get("public_entrypoints", "websecure")

    if access == "public":
        vpn_routes = svc.get("vpn_routes", [])
        if vpn_routes:
            _add_dual_router_labels(
                labels, name, domains, port,
                primary_routes=None,
                secondary_routes=vpn_routes,
                primary_mw=public_mw,
                secondary_mw=vpn_mw,
                primary_suffix="",
                secondary_suffix="-vpn",
                primary_priority="10",
                secondary_priority="20",
                primary_entrypoints=public_ep,
                secondary_entrypoints=vpn_ep,
            )
        else:
            _add_single_router_labels(
                labels, name, domains, port, public_mw, suffix="",
                entrypoints=public_ep,
            )
    elif access == "vpn":
        public_routes = svc.get("public_routes", [])
        if public_routes:
            _add_dual_router_labels(
                labels, name, domains, port,
                primary_routes=None,
                secondary_routes=public_routes,
                primary_mw=vpn_mw,
                secondary_mw=public_mw,
                primary_suffix="-vpn",
                secondary_suffix="-public",
                primary_priority="10",
                secondary_priority="20",
                primary_entrypoints=vpn_ep,
                secondary_entrypoints=public_ep,
            )
        else:
            _add_single_router_labels(
                labels, name, domains, port, vpn_mw, suffix="-vpn",
                entrypoints=vpn_ep,
            )


def _rule_literal(value):
    """Validate a value that is about to sit inside a Traefik backquoted string.

    Traefik's rule syntax has no escape sequence for a backtick inside a
    backquoted string: a domain or route containing one closes the matcher
    early and everything after it is parsed as further rule syntax. So the
    only correct treatment is to refuse the value rather than to "escape" it
    into something Traefik would still mis-parse. Newlines are refused for the
    same reason (a label value is single-line).

    Schema validation (services.schema.json) rejects these at the door; this
    is the second layer, for roles run straight from ansible-playbook.
    """
    text = str(value)
    for bad, label in (("`", "a backtick"), ("\n", "a newline"), ("\r", "a carriage return")):
        if bad in text:
            raise ValueError(
                f"cannot build a Traefik router rule: {label} in {text!r}. "
                "Traefik backquoted strings have no escape sequence, so such a "
                "value would break out of the matcher."
            )
    return text


def _host_rule(domains):
    """Build the Host() match expression for a router.

    Multi-domain: Host(`d1`) || Host(`d2`). Single domain: Host(`d1`).
    """
    if not domains:
        raise ValueError(
            "cannot build a Traefik router rule: service has no domains"
        )
    return " || ".join(f"Host(`{_rule_literal(d)}`)" for d in domains)


def _add_single_router_labels(
    labels, name, domains, port, mw_list, suffix="", entrypoints="websecure"
):
    """Add router + service labels for a single-router service."""
    router = f"{name}{suffix}"

    labels[f"traefik.http.routers.{router}.rule"] = _host_rule(domains)
    labels[f"traefik.http.routers.{router}.entrypoints"] = entrypoints
    labels[f"traefik.http.routers.{router}.tls.certresolver"] = "letsencrypt"
    labels[f"traefik.http.routers.{router}.middlewares"] = ",".join(mw_list)
    labels[f"traefik.http.services.{router}.loadbalancer.server.port"] = port


def _add_dual_router_labels(
    labels, name, domains, port,
    primary_routes, secondary_routes,
    primary_mw, secondary_mw,
    primary_suffix, secondary_suffix,
    primary_priority, secondary_priority,
    primary_entrypoints="websecure", secondary_entrypoints="websecure",
):
    """Add labels for a dual-router service (public + vpn split).

    Both routers match every entry in `domains` — pre-GH#31 only domains[0]
    was routed and the rest silently 404'd with no cert.
    """
    # Shared service backend
    labels[f"traefik.http.services.{name}.loadbalancer.server.port"] = port

    host_expr = _host_rule(domains)
    # `&&` binds tighter than `||` in Traefik rules, so the OR-group must be
    # parenthesised — otherwise Host(a) || Host(b) && PathPrefix(p) parses as
    # Host(a) || (Host(b) && PathPrefix(p)) and the secondary router matches
    # the whole of domain a instead of just its paths. For a vpn service with
    # public_routes that serves the entire first domain publicly. Single
    # domain stays unwrapped so existing labels are byte-identical and don't
    # trigger a spurious config-hash recreation.
    sec_host = f"({host_expr})" if len(domains) > 1 else host_expr

    # Secondary router (higher priority, path-matched)
    sec = f"{name}{secondary_suffix}"
    path_rules = " || ".join(
        f"PathPrefix(`{_rule_literal(r)}`)" for r in secondary_routes
    )
    labels[f"traefik.http.routers.{sec}.rule"] = (
        f"{sec_host} && ({path_rules})"
    )
    labels[f"traefik.http.routers.{sec}.service"] = name
    labels[f"traefik.http.routers.{sec}.priority"] = secondary_priority
    labels[f"traefik.http.routers.{sec}.entrypoints"] = secondary_entrypoints
    labels[f"traefik.http.routers.{sec}.tls.certresolver"] = "letsencrypt"
    labels[f"traefik.http.routers.{sec}.middlewares"] = ",".join(secondary_mw)

    # Primary router (lower priority, catch-all)
    pri = f"{name}{primary_suffix}"
    labels[f"traefik.http.routers.{pri}.rule"] = host_expr
    labels[f"traefik.http.routers.{pri}.service"] = name
    labels[f"traefik.http.routers.{pri}.priority"] = primary_priority
    labels[f"traefik.http.routers.{pri}.entrypoints"] = primary_entrypoints
    labels[f"traefik.http.routers.{pri}.tls.certresolver"] = "letsencrypt"
    labels[f"traefik.http.routers.{pri}.middlewares"] = ",".join(primary_mw)


def _bool(value):
    """Coerce Ansible-style booleans to Python bool."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() in ("true", "yes", "1")
    return bool(value)


# ── Traefik global middleware labels ──────────────────────────────────────


def bay_traefik_global_labels(config):
    """Generate global middleware labels for the Traefik container.

    These define vpn-only, secure-headers, compress, error-pages,
    public-chain, and vpn-chain middlewares used by all services.

    Args:
        config: Dict of traefik/security config variables (piped value)

    Returns:
        Dict of label key-value pairs
    """
    labels = {
        "traefik.enable": "true",
        "traefik.http.services.traefik-noop.loadbalancer.server.port": "0",
        "com.centurylinklabs.watchtower.enable": "false",
    }

    # VPN IP allowlist
    vpn_ips = config.get("vpn_allowed_ips", ["127.0.0.1"])
    if isinstance(vpn_ips, str):
        vpn_ips = [vpn_ips]
    labels["traefik.http.middlewares.vpn-only.ipallowlist.sourcerange"] = ",".join(
        str(ip) for ip in vpn_ips
    )

    # Security headers
    headers_enabled = _bool(config.get("traefik_security_headers_enabled", True))
    sh = config.get("traefik_security_headers", {})
    if headers_enabled:
        pfx = "traefik.http.middlewares.secure-headers.headers"
        labels[f"{pfx}.stsSeconds"] = str(sh.get("sts_seconds", 63072000))
        labels[f"{pfx}.stsIncludeSubdomains"] = str(sh.get("sts_include_subdomains", True)).lower()
        labels[f"{pfx}.stsPreload"] = str(sh.get("sts_preload", True)).lower()
        labels[f"{pfx}.contentTypeNosniff"] = str(sh.get("content_type_nosniff", True)).lower()
        labels[f"{pfx}.frameDeny"] = str(sh.get("frame_deny", True)).lower()
        labels[f"{pfx}.referrerPolicy"] = sh.get("referrer_policy", "strict-origin-when-cross-origin")
        labels[f"{pfx}.permissionsPolicy"] = sh.get("permissions_policy", "camera=(), microphone=(), geolocation=(), payment=()")
        labels[f"{pfx}.browserXssFilter"] = str(sh.get("browser_xss_filter", True)).lower()
        for k, v in sh.get("custom_response_headers", {}).items():
            labels[f"{pfx}.customResponseHeaders.{k}"] = str(v)

    # Compression
    compress_enabled = _bool(config.get("traefik_compress_enabled", True))
    if compress_enabled:
        labels["traefik.http.middlewares.compress.compress"] = "true"

    # Error pages
    errors_enabled = _bool(config.get("traefik_error_pages_enabled", False))
    if errors_enabled:
        labels["traefik.http.middlewares.errors.errors.service"] = "error-pages@docker"
        labels["traefik.http.middlewares.errors.errors.status"] = config.get("traefik_error_pages_status", "400-599")
        labels["traefik.http.middlewares.errors.errors.query"] = config.get("traefik_error_pages_query", "/{status}.html")

    # Middleware chains
    pub = []
    vpn = []
    if headers_enabled:
        pub.append("secure-headers")
        vpn.append("secure-headers")
    if compress_enabled:
        pub.append("compress")
        vpn.append("compress")
    vpn.append("vpn-only")
    if errors_enabled:
        pub.append("errors")
        vpn.append("errors")
    labels["traefik.http.middlewares.public-chain.chain.middlewares"] = ",".join(pub)
    labels["traefik.http.middlewares.vpn-chain.chain.middlewares"] = ",".join(vpn)

    return labels


# ── Watchtower labels ───────────────────────────────────────────────────


def bay_watchtower_labels(update_mode, enabled=True):
    """Generate Watchtower labels for a container.

    Args:
        update_mode: 'auto', 'monitor', or false (piped value)
        enabled: Whether watchtower is globally enabled

    Returns:
        Dict of watchtower label key-value pairs
    """
    if not _bool(enabled):
        return {}

    if update_mode == "auto":
        return {
            "com.centurylinklabs.watchtower.enable": "true",
            "com.centurylinklabs.watchtower.monitor-only": "false",
        }
    elif update_mode is False or str(update_mode).lower() in ("false", "no", "0"):
        return {
            "com.centurylinklabs.watchtower.enable": "false",
        }
    else:  # 'monitor' or default
        return {
            "com.centurylinklabs.watchtower.enable": "true",
            "com.centurylinklabs.watchtower.monitor-only": "true",
        }


# ── Healthcheck conversion ──────────────────────────────────────────────


def bay_healthcheck(hc, port=80):
    """Convert services.yml healthcheck to docker_container format.

    Args:
        hc: Healthcheck config dict from services.yml (piped value)
        port: Container port for default wget test

    Returns:
        Dict with test, interval, timeout, retries, start_period
    """
    if not hc:
        return None

    result = {}

    if "test" in hc:
        result["test"] = hc["test"]
    elif "path" in hc:
        result["test"] = [
            "CMD", "wget", "--spider", "-q",
            f"http://localhost:{port}{hc['path']}",
        ]

    result["interval"] = hc.get("interval", "30s")
    result["timeout"] = hc.get("timeout", "10s")
    result["retries"] = hc.get("retries", 3)

    if "start_period" in hc:
        result["start_period"] = hc["start_period"]

    return result


# ── Repo deduplication ─────────────────────────────────────────────────


# ── Basic-auth hashing ───────────────────────────────────────────────────
#
# bcrypt, with a salt derived from the credentials themselves.
#
# It used to shell out to `openssl passwd -apr1 -salt <salt> <password>`, which
# put the password on the argument list of a child process — visible in `ps` on
# the control machine — and produced an MD5-crypt hash.
#
# The salt stays DETERMINISTIC, and that is load-bearing rather than lazy: the
# hash ends up in a Traefik container label, so a random salt would change the
# label on every render and the reconciler would recreate every basic-auth
# protected container on every deploy. Seeding it from sha256 of
# (stack, service, username, password) means the same credentials always
# produce the same hash, and two services sharing a username still get
# different salts. Unlike the old seed, the password is part of it, so a
# password change also changes the salt.
_BCRYPT_B64 = b"./ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
_STD_B64 = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
_BCRYPT_COST = 10


def bay_basic_auth_hash(stack_name, service_name, username, password):
    """Return a deterministic bcrypt hash for one basic-auth credential."""
    import base64

    import bcrypt

    seed = f"{stack_name}|{service_name}|{username}|{password}".encode("utf-8")
    raw = hashlib.sha256(seed).digest()[:16]
    salt_chars = base64.b64encode(raw)[:22].translate(
        bytes.maketrans(_STD_B64, _BCRYPT_B64)
    )
    salt = b"$2b$%02d$" % _BCRYPT_COST + salt_chars
    secret = password.encode("utf-8")
    if len(secret) > 72:
        raise ValueError(
            f"basic-auth password for {username!r} is longer than bcrypt's "
            f"72-byte limit ({len(secret)} bytes); shorten it"
        )
    return bcrypt.hashpw(secret, salt).decode("ascii")


def _repo_slug(repo, branch="main"):
    """Compute a deterministic, filesystem-safe slug for a (repo, branch) tuple.

    Uses a human-readable prefix from the repo name plus a short hash suffix
    to prevent collisions from different hosts with the same repo path.
    """
    key = f"{repo}|{branch}"
    h = hashlib.sha1(key.encode()).hexdigest()[:8]
    name = repo.rstrip("/").rsplit("/", 1)[-1].replace(".git", "")
    name = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return f"{name}-{branch}-{h}"


def _compute_clone_url(repo, token=None):
    """Convert a repo URL to the plain HTTPS URL git should be pointed at.

    The token is deliberately NOT embedded. It used to be
    `https://x-access-token:<PAT>@host/path`, which put the PAT in
    /proc/<pid>/cmdline for the life of every clone and fetch, and wrote it
    permanently into .git/config the first time `git remote set-url` ran.
    Authentication now goes through a GIT_ASKPASS helper
    (roles/git_deploy/templates/git-askpass.sh.j2) instead.

    `token` is kept in the signature because it still selects the transport:
    a repo with a token is cloned over HTTPS, one without over SSH with a
    deploy key. Mirrors the URL transformation in remote_build.yml and
    rebuild.sh.j2.
    """
    if not token:
        return repo
    if repo.startswith("git@"):
        parts = repo.split(":")
        host = parts[0].replace("git@", "")
        path = parts[1]
        return f"https://{host}/{path}"
    elif "://" in repo:
        rest = repo.split("://")[1]
        return f"https://{rest}"
    return repo


def bay_hexkey(value):
    """Jinja2 filter: hex-encode a secret for `openssl dgst -macopt hexkey:`.

    openssl's only way to take a MAC key off the command line without putting
    it on the argument list is `-macopt hexkey:<hex>` read from a file. The
    hex is of the secret's UTF-8 bytes, which is what the webhook receiver
    signs with (hmac.new(secret.encode(), ...) in files/webhook/app.py).
    """
    if isinstance(value, bytes):
        return value.hex()
    return str(value).encode("utf-8").hex()


def bay_token_url(repo, token):
    """Jinja2 filter: the HTTPS clone URL for a token-authenticated repo.

    Historic name. It no longer produces a token-bearing URL — see
    _compute_clone_url.
    """
    return _compute_clone_url(repo, token)


def bay_repo_slug(repo, branch="main"):
    """Jinja2 filter: compute a filesystem-safe repo slug from (repo, branch)."""
    return _repo_slug(repo, branch)


def bay_repo_groups(services, service_names):
    """Group services by (build.repo, build.branch) for clone deduplication.

    Returns a list of dicts, each with:
      slug, repo, branch, services, has_token, token, clone_url, auth_svc
    """
    groups = {}
    for svc_name in service_names:
        svc = services[svc_name]
        build = svc.get("build", {})
        repo = build.get("repo", "")
        branch = build.get("branch", "main")
        slug = _repo_slug(repo, branch)
        if slug not in groups:
            has_token = "token" in build
            token = build.get("token", "")
            clone_url = _compute_clone_url(repo, token if has_token else None)
            groups[slug] = {
                "slug": slug,
                "repo": repo,
                "branch": branch,
                "services": [svc_name],
                "has_token": has_token,
                # Consumed only by the GIT_ASKPASS helper task, which renders
                # with no_log. It is deliberately NOT part of clone_url.
                "token": token,
                "clone_url": clone_url,
                "auth_svc": svc_name,
            }
        else:
            groups[slug]["services"].append(svc_name)
    return list(groups.values())


def _build_identity(build):
    """Compute a deterministic identity for a build config.

    Services with the same identity produce identical Docker images
    (same repo, branch, dockerfile, context, and build args).
    """
    import json

    key = json.dumps(
        {
            "repo": build.get("repo", ""),
            "branch": build.get("branch", "main"),
            "dockerfile": build.get("dockerfile", "Dockerfile"),
            "context": build.get("context", "."),
            "args": dict(sorted(build.get("args", {}).items())),
        },
        sort_keys=True,
    )
    return hashlib.sha1(key.encode()).hexdigest()[:12]


def bay_build_dedup_map(services, service_names):
    """Map each service to its build dedup role (primary or alias).

    Groups services by build identity — (repo, branch, dockerfile, context, args).
    The first service in each group is the "primary"; others are "aliases" that
    can re-tag from the primary's image instead of rebuilding.

    Returns a dict: {svc_name: {primary: bool, primary_svc: str, group_size: int}}
    """
    identity_groups = {}
    for svc_name in service_names:
        build = services[svc_name].get("build", {})
        identity = _build_identity(build)
        if identity not in identity_groups:
            identity_groups[identity] = [svc_name]
        else:
            identity_groups[identity].append(svc_name)

    result = {}
    for group in identity_groups.values():
        primary = group[0]
        for svc_name in group:
            result[svc_name] = {
                "primary": svc_name == primary,
                "primary_svc": primary,
                "group_size": len(group),
            }
    return result


# ── Image consumer mapping ────────────────────────────────────────────


def bay_image_consumers(services, service_names):
    """Map Docker image refs to services that use them.

    Given the full services dict and a list of active service names,
    returns a dict mapping each image ref to the sorted list of service
    names (from service_names) that reference it.

    Only includes images that are used by at least one service in
    service_names. Services without an ``image`` field are skipped.

    Args:
        services: Full services dict from services.yml (piped value)
        service_names: List of service names to consider (active for host)

    Returns:
        Dict mapping image ref to list of service names, e.g.:
        {"registry.../storefront:latest": ["storefront-de", "storefront-es", ...]}
    """
    image_map = {}
    for svc_name in service_names:
        svc = services.get(svc_name, {})
        image = svc.get("image")
        if not image:
            continue
        if image not in image_map:
            image_map[image] = []
        image_map[image].append(svc_name)
    # Sort service names within each group for deterministic output
    for image in image_map:
        image_map[image].sort()
    return image_map


def bay_image_region_map(services, build_service_names):
    """Map built image refs to the set of regions that need pull signals.

    For each service in build_service_names that has both a build block
    (with strategy remote/push) and an image field, find ALL services in
    the full services dict that reference the same image. Collect the union
    of regions from all consumers of that image.

    This is used on the build server to know which regions to send
    image-level pull signals to after a successful build+push.

    Args:
        services: Full services dict from services.yml (piped value)
        build_service_names: List of build service names (remote strategy)

    Returns:
        Dict mapping image ref to sorted list of region names, e.g.:
        {"registry.../storefront:latest": ["eu", "na"]}
    """
    # First, collect image refs produced by build services
    built_images = set()
    for svc_name in build_service_names:
        svc = services.get(svc_name, {})
        build = svc.get("build")
        if not build:
            continue
        strategy = build.get("strategy", "local")
        if strategy in ("remote", "push"):
            image = svc.get("image")
            if image:
                built_images.add(image)

    if not built_images:
        return {}

    # For each built image, find all services using it and collect regions
    result = {}
    for image in built_images:
        regions = set()
        for svc_name, svc in services.items():
            if svc.get("image") == image:
                for r in svc.get("regions", []):
                    regions.add(r)
        if regions:
            result[image] = sorted(regions)
    return result


# ── Infra container spec hash ─────────────────────────────────────────


def bay_spec_hash(spec, env_digest=None):
    """Compute a stable SHA-256 hash of a container spec.

    Covers the fields that docker_container acts on: image, volumes, env,
    labels (excluding the config-hash label itself), networks, network_mode,
    ports, command, restart_policy, mem_limit.  Fields that change between
    deploys but are not part of the running container config are excluded
    (e.g. ``build``, ``type``).

    Stable across Python versions because json.dumps(sort_keys=True) is
    used rather than repr() or dict ordering.

    Args:
        spec: Container spec dict (piped value)
        env_digest: Optional non-reversible digest of the rendered env file
            (for service/accessory specs whose env lives outside the spec in
            an external env_file). When falsy, the hash is identical to
            omitting it — so the rig/infra path and its tests are unaffected.

    Returns:
        Lowercase hex SHA-256 string (64 chars)
    """
    import json

    HASH_LABEL = "com.bay.config-hash"
    # The pre-1.0 spelling is excluded too, so a spec that still carries it
    # hashes identically to one that does not.
    LEGACY_HASH_LABEL = "com.argo.config-hash"  # legacy-argo: dual-read, remove in a future major release

    # Exclude transient / meta fields that should not drive recreation
    _EXCLUDED = {"type", "build", "zero_downtime", "health_check_timeout", "env_file"}

    # Labels: exclude the hash label itself to avoid a circular dependency
    labels = {
        k: v
        for k, v in (spec.get("labels") or {}).items()
        if k not in (HASH_LABEL, LEGACY_HASH_LABEL)
    }

    stable = {
        k: v
        for k, v in spec.items()
        if k not in _EXCLUDED and k != "labels"
    }
    stable["labels"] = labels

    # Fold in a non-reversible env/secrets digest for service and
    # accessory specs whose environment lives in an external env_file (and is
    # therefore absent from spec["env"]). The digest is a one-way sha256 of the
    # rendered env content, so secret rotation busts the hash without the label
    # ever being derived from plaintext. Falsy => identical to omitting it
    # (keeps the rig/infra hash and its existing tests unchanged).
    if env_digest:
        stable["__env_digest__"] = env_digest

    serialised = json.dumps(stable, sort_keys=True, default=str)
    return hashlib.sha256(serialised.encode()).hexdigest()


# ── Access-gateway adapter: cross-host bind-IP resolver ──────────────────
#
# The gateway contract vars live in roles/access_gateway/defaults/main.yml.
# That covers every SAME-host read, but not cross-host reads, and this was
# verified rather than assumed: Ansible role defaults — and play `vars:` —
# are absent from `hostvars[<other_host>]` entirely. Only inventory vars,
# group_vars, host_vars, gathered facts and set_facts land there.
#
# Two call sites need ANOTHER host's overlay IP:
#   - roles/crowdsec_allowlist, which walks every inventory host to build the
#     self-ban exemption list.
#   - deploy.yml's multi-region `links:` resolution, which needs the link
#     TARGET host's overlay IP.
#
# Reading `hostvars[h].gateway_bind_ip` at those sites yields nothing, which
# would drop the overlay self-exemption and re-open the 2026-07-01 infra
# self-ban incident. Reading the backend's own var name there instead would
# re-couple neutral roles to headscale. So the resolution is encapsulated
# HERE, in one backend-aware function the ratchet test explicitly allowlists.
#
# Precedence:
#   1. `gateway_bind_ip` if a consumer set it in group_vars/host_vars — the
#      forward-looking, backend-neutral input name.
#   2. `headscale_server_tailnet_ip` — the incumbent name, honoured
#      indefinitely so this migration is zero-config-change. Cross-host that
#      only ever resolved to an explicitly configured group_vars value, which
#      is exactly the behaviour that shipped before this adapter was added.
#   3. "" — no overlay. Callers must treat "" as "nothing to exempt / nothing
#      to bind", never as a usable address.
def bay_gateway_bind_ip(hostvars_entry):
    """Resolve one host's access-gateway overlay IP from its hostvars.

    Takes a hostvars mapping for a single host, returns a string. Returns ""
    when that host has no overlay address — the correct answer for
    `access_gateway: none` and for any host that never configured one.
    """
    hv = hostvars_entry or {}
    for key in ("gateway_bind_ip", "headscale_server_tailnet_ip"):
        value = hv.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""
