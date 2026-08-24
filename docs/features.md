---
# Bay -- Features & Advantages

Bay is an Ansible framework for provisioning hardened Docker servers with VPN-aware reverse proxy, declarative service deployment, and self-hosted access gateway management.

## Declarative Service Management

A single `services.yml` file is the source of truth for the entire stack. From this one file, Bay generates:

- Docker Compose definitions (images, volumes, networks, healthchecks)
- Traefik routing rules and SSL certificates
- Per-service environment files (clear + vault-encrypted secrets)
- Access control policy (public, VPN-only, admin-only)
- DNS records for VPN split-DNS
- Backup configuration (auto-detected dump strategy per database engine)
- Container update policy (monitor-only or auto-update via Watchtower)

Services and accessories are distinct concepts:

- **Services** are application containers that receive Traefik routing, automatic SSL, and access control.
- **Accessories** are infrastructure containers (PostgreSQL, Redis, MariaDB) deployed alongside services without external routing.

See [services.md](services.md) for the full schema reference.

## VPN-Aware Access Control

Three access modes control who can reach each service:

| Mode | Behavior |
|------|----------|
| `public` | Open to all traffic |
| `vpn` | Restricted to VPN clients only |
| `admin` | Restricted to VPN clients, no public route exceptions |

Protection is enforced at multiple layers:

1. **nftables** -- firewall-level IP filtering
2. **CrowdSec** -- intrusion detection with automatic banning
3. **Traefik IPAllowList** -- middleware-level enforcement using VPN CIDR ranges

Path-level access control works in both directions:

- **`public_routes`** on a VPN service -- specific paths that remain publicly accessible (useful for webhook endpoints or health checks)
- **`vpn_routes`** on a public service -- specific paths restricted to VPN users only (useful for admin panels or sensitive endpoints)

### Automatic split-DNS

When Headscale is the access gateway, VPN service domains automatically resolve to the server's tailnet IP for enrolled clients. This means:

- VPN clients access `app.example.com` through the tunnel -- requests pass the IPAllowList transparently
- Non-VPN clients hitting the same domain get a 403
- No `/etc/hosts` hacks, no tailnet IP bookmarks, no separate internal domains

DNS records are generated from `services.yml` into `extra-records.json` and hot-reloaded by Headscale -- zero manual DNS configuration for VPN services.

## Self-Hosted Headscale

Bay provisions and manages its own [Headscale](https://github.com/juanfont/headscale) instance as a Docker container, providing a self-hosted Tailscale-compatible coordination server.

### The `extra_records` advantage

Headscale's `extra_records` feature enables the automatic split-DNS described above. This is something **Tailscale.com cannot offer**:

- The Tailscale client protocol has supported custom DNS records since 2021
- Tailscale.com has never exposed this capability server-side
- GitHub issue [tailscale/tailscale#1543](https://github.com/tailscale/tailscale/issues/1543) has 869+ upvotes and has been open for 5+ years with no progress
- Self-hosted Headscale is the only way to use this feature today

Bay generates these DNS records automatically from `services.yml` -- adding a VPN service means its domain is immediately resolvable on the tailnet.

### Gateway management

The `bin/bay gateway` CLI provides full Headscale administration — status, nodes, users, keys, enrollment, routes, and ACL auditing — with no SSH to the server. `bin/bay gateway --help` is the command reference.

### Additional capabilities

- **Multi-region support** -- one control region runs Headscale, other regions join via REST API
- **OIDC support** -- self-service VPN enrollment through identity providers
- **Embedded DERP relay** -- NAT traversal without relying on Tailscale's public DERP servers

## Multi-Region Support

Deploy the same stack to multiple regional servers from a single consumer repo:

- Regions are standard Ansible inventory groups with per-region `group_vars/` overrides
- Cross-region service connectivity via the tailnet (Headscale coordinates all regions)
- Per-region domain configuration (e.g., `eu.example.com`, `na.example.com`)
- Shared Headscale coordination server on the control region
- Target individual regions or all at once: `bin/bay deploy eu`, `bin/bay deploy production`

See [multi-region.md](multi-region.md) for the full setup guide.

## Security Stack

- **Traefik v3.6 with host networking** -- `network_mode: host` preserves real client IPs through the entire request chain (no Docker NAT)
- **CrowdSec IDS/IPS** -- reads Traefik access logs and SSH auth logs, shares ban decisions with the nftables bouncer
- **nftables firewall** -- kernel-level packet filtering with CrowdSec blocklist integration and VPN IP whitelisting
- **Automated SSL** -- Let's Encrypt HTTP-01 challenge, per-service certificates issued on first request
- **Ansible Vault** -- encrypted secrets in `group_vars/<env>/secrets.yml`, resolved at deploy time into per-service `.env` files
- **SSH hardening** -- key-only auth, no root login, `MaxStartups 10:30:60` rate limiting, 30s `LoginGraceTime` (via `sshd_hardening` role supplementing geerlingguy.security)
- **Swap provisioning** -- configurable swap file (default 2G, swappiness 10) prevents OOM-kills on memory-constrained servers
- **CrowdSec bouncer binding** -- systemd drop-in auto-restarts the nftables bouncer when the CrowdSec agent restarts, preventing stale/empty blocklist sets after OOM recovery
- **Container memory limits** -- optional `mem_limit` per service/accessory prevents runaway containers from OOM-killing the host
- **Deploy lock** -- file-based mutex prevents concurrent deploys; stale locks (>1 hour) are automatically ignored
- **Deploy privilege separation** -- root bootstrap creates directories, then all deployment runs as unprivileged `app` user with Docker group membership

## Developer Experience

- **`bin/bay` CLI** -- single entry point wrapping deploy, provision, validate, gateway, vault, backup, test, and framework management
- **`bay validate`** -- pre-deploy checks: YAML syntax, schema validation, SSH connectivity, vault password, DNS resolution
- **`bay setup`** -- interactive wizard for project scaffolding (also supports `--defaults` and fully non-interactive flag-driven mode)
- **`bay guide`** -- context-aware setup guide for the current project state
- **Framework versioning** -- consumers pin to semver tags via `.bay-version`; `install`/`update`/`status` commands manage the lifecycle
- **`dev-link` / `dev-unlink`** -- symlink `.bay/` to a local framework checkout for rapid iteration without commit+push+tag cycles
- **Config change detection** -- only redeploy services whose configuration has actually changed
- **Dry runs** -- `bin/bay deploy production -- --check --diff` passes extra args through to Ansible

## Infrastructure as Code

- **Pure Ansible** -- no custom runtime, no daemon, no agent on target servers; standard SSH + Python
- **Consumer model** -- the framework is cloned into `.bay/` inside the consumer repo; consumer provides only configuration (`group_vars/`, `hosts/`, `services.yml`)
- **Idempotent deploys** -- run `deploy` repeatedly; only changed resources are updated
- **Restic backups** -- deduplicated, encrypted backups to S3-compatible storage with per-accessory repositories, systemd timers, configurable retention, and one-command restore
- **Watchtower** -- container image update monitoring with Telegram notifications; opt-in auto-update per service
- **Pluggable alerting** -- crash/build/deploy/disk/backup alerts to Telegram and/or a generic webhook sink (Campfire, Slack, or plain text), fail-open by design; see [alerting.md](alerting.md)
- **`bootstrap.sh`** -- one-command project scaffolding from the framework's `example/` template

---

This document is updated as features are added. See also: [access-gateways.md](access-gateways.md), [multi-region.md](multi-region.md), [services.md](services.md), [design-decisions.md](design-decisions.md)
