"""Custom Ansible filters for the Bay framework."""

import hashlib
import subprocess


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
            "bay_log_rotation_spec": bay_log_rotation_spec,
        }


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


def bay_log_rotation_spec(override, defaults):
    """Resolve per-container log rotation into docker_container kwargs.

    Mirrors the `logging_block` Jinja macro so the lifecycle role passes the
    same config to the docker_container module that the compose template
    renders. Returns a dict with keys {log_driver, log_options} — or an empty
    dict when the container opts out (in which case the container inherits
    the daemon's log-driver defaults).

    Args:
        override: Per-container `log_rotation` value. May be a dict, False
            (explicit opt-out), or None/missing (fall back to defaults).
        defaults: Global `log_rotation_defaults` — a dict, or False to opt
            out globally.

    Returns:
        dict with docker_container-ready keys, or {} to skip logging config.
    """
    if override is False:
        return {}

    if override is None:
        if not isinstance(defaults, dict):
            return {}
        merged = dict(defaults)
    elif isinstance(override, dict):
        merged = {}
        if isinstance(defaults, dict):
            merged.update(defaults)
        merged.update(override)
    else:
        return {}

    driver = merged.get("driver")
    max_size = merged.get("max_size")
    max_file = merged.get("max_file")
    if driver is None or max_size is None or max_file is None:
        return {}

    return {
        "log_driver": str(driver),
        "log_options": {
            "max-size": str(max_size),
            "max-file": str(max_file),
        },
    }


# ── Traefik label generation ────────────────────────────────────────────


def bay_traefik_labels(svc, name, config):
    """Generate Traefik labels for a service container.

    Ports the label logic from _service.j2 into a pure Python filter.

    Args:
        svc: Service config dict from services.yml (piped value)
        name: Service name
        config: Dict of traefik config variables (defaults, feature flags)

    Returns:
        Dict of label key-value pairs (all string values)
    """
    network = config.get("traefik_docker_network", "services")
    labels = {
        "traefik.enable": "true",
        "traefik.docker.network": network,
    }

    # ── Middleware chain computation ──────────────────────────────────
    mw = svc.get("middleware", {})
    svc_headers = mw.get("security_headers", True)
    svc_compress = mw.get("compress", True)
    needs_custom_chain = (not svc_headers) or (not svc_compress)

    # Per-service middleware names
    per_svc_mw = []
    if "rate_limit" in mw:
        per_svc_mw.append(f"{name}-ratelimit")
    if "in_flight_req" in mw:
        per_svc_mw.append(f"{name}-inflightreq")
    if "basic_auth" in mw:
        per_svc_mw.append(f"{name}-basicauth")
    if "circuit_breaker" in mw:
        per_svc_mw.append(f"{name}-circuitbreaker")
    if "retry" in mw:
        per_svc_mw.append(f"{name}-retry")

    # Build middleware lists for public and VPN routers
    if needs_custom_chain:
        public_mw = [f"{name}-chain"] + per_svc_mw
        vpn_mw = [f"{name}-chain", "vpn-only"] + per_svc_mw

        # Custom chain definition
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
    else:
        public_mw = ["public-chain"] + per_svc_mw
        vpn_mw = ["vpn-chain"] + per_svc_mw

    # ── Per-service middleware definitions ────────────────────────────
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
                salt_seed = f"{config.get('stack_name', 'bay')}{name}{cred['username']}"
                salt = hashlib.md5(salt_seed.encode()).hexdigest()[:8]
                result = subprocess.run(
                    ["openssl", "passwd", "-apr1", "-salt", salt, cred["password"]],
                    capture_output=True, text=True, check=True,
                )
                entries.append(f"{cred['username']}:{result.stdout.strip()}")
            users_str = ",".join(entries)
            labels[f"{pfx}.users"] = users_str
        elif "users" in ba:
            # Pre-hashed users: pass as-is; Docker API stores label values literally (no $$ interpolation)
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

    # ── Router labels ────────────────────────────────────────────────
    access = svc.get("access", "public")
    port = str(svc.get("ports", {}).get("internal", 80))
    domains = svc.get("domains", [])

    # Entrypoints per router class. Mirror _service.j2: VPN routers bind
    # `vpn_entrypoints`, public routers bind `public_entrypoints` — both
    # default to "websecure" so non-split hosts render byte-identically.
    # On a `traefik_split_entrypoints` host these carry "websecure_tailnet"
    # so a VPN router stays reachable on the tailnet listener.
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

    return labels


def _host_rule(domains):
    """Build the Host() match expression for a router.

    Multi-domain: Host(`d1`) || Host(`d2`). Single domain: Host(`d1`).
    """
    if not domains:
        raise ValueError(
            "cannot build a Traefik router rule: service has no domains"
        )
    return " || ".join(f"Host(`{d}`)" for d in domains)


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
    path_rules = " || ".join(f"PathPrefix(`{r}`)" for r in secondary_routes)
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
