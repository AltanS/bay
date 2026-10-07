---
# Bay -- Features & Advantages

Bay is a command-line tool and an Ansible framework for provisioning hardened Docker servers with VPN-aware reverse proxy, declarative app deployment, and self-hosted access gateway management.

## Declarative Service Management

Two kinds of file are the source of truth for the stack: a `bay.toml` per app and one `bay.fleet.toml` for the fleet (boxes, domains, shared resources). `bay up` and `bay compile` write `group_vars/all/services.yml` from them. Nobody hand-edits that file, and the next compile refuses a hand edit. From the compiled file, Bay generates:

- A rendered compose file that describes the stack (images, volumes, networks, healthchecks). The reconciler and `rebuild.sh` create the containers with `docker run`, not with `docker compose`.
- Traefik routing rules and SSL certificates
- Per-service environment files (clear values, plus secrets from the encrypted secrets file)
- Access control policy (public or tailnet-only, plus per-path `open` and `locked` lists)
- DNS records for VPN split-DNS
- Backup configuration (the dump `method` per shared resource, set in `bay.fleet.toml`)
- Container update policy (monitor-only or auto-update via Watchtower)

The compiled file has two kinds of container:

- **Services** are routed app containers. They receive Traefik routing, automatic SSL, and access control.
- **Accessories** are containers with no route: the shared resources of `bay.fleet.toml` (PostgreSQL, Redis) and the `internal` containers of a `bay.toml`.

See [bay-toml.md](bay-toml.md) for the files you write, and [services.md](services.md) for the schema of the compiled `services.yml`.

## VPN-Aware Access Control

`access.mode` in `bay.toml` controls who can reach each app. The compiler turns it into the `access` key of the compiled `services.yml` (compiled form, do not edit):

| `bay.toml` mode | Compiled `access` | Behavior |
|------|------|----------|
| `public` | `public` | Open to all traffic |
| `tailnet` | `vpn` | Restricted to VPN clients only, with optional `open` path exceptions |
| `internal` | no route (an accessory) | No route; only containers that need it can reach it |

Protection is enforced at multiple layers:

1. **nftables** -- firewall-level IP filtering
2. **CrowdSec** -- intrusion detection with automatic banning
3. **Traefik IPAllowList** -- middleware-level enforcement using VPN CIDR ranges

Path-level access control works in both directions:

- **`open`** on a `tailnet` app (compiled: `public_routes`) -- specific paths that remain publicly accessible (useful for webhook endpoints or health checks)
- **`locked`** on a `public` app (compiled: `vpn_routes`) -- specific paths restricted to VPN users only (useful for admin panels or sensitive endpoints)

### Automatic split-DNS

When Headscale is the access gateway, VPN service domains automatically resolve to the server's tailnet IP for enrolled clients. This means:

- VPN clients access `app.example.com` through the tunnel -- requests pass the IPAllowList transparently
- Non-VPN clients hitting the same domain get a 403
- No `/etc/hosts` hacks, no tailnet IP bookmarks, no separate internal domains

DNS records are generated from the compiled `services.yml` into `extra-records.json` and hot-reloaded by Headscale -- zero manual DNS configuration for VPN services.

## Self-Hosted Headscale

Bay provisions and manages its own [Headscale](https://github.com/juanfont/headscale) instance as a Docker container, providing a self-hosted Tailscale-compatible coordination server.

### The `extra_records` advantage

Headscale's `extra_records` feature enables the automatic split-DNS described above. This is something **Tailscale.com cannot offer**:

- The Tailscale client protocol has supported custom DNS records since 2021
- Tailscale.com has never exposed this capability server-side
- GitHub issue [tailscale/tailscale#1543](https://github.com/tailscale/tailscale/issues/1543) has 869+ upvotes and has been open for 5+ years with no progress
- Self-hosted Headscale is the only way to use this feature today

Bay generates these DNS records automatically from the compiled `services.yml` -- a deploy of a new `tailnet` app makes its domain resolvable on the tailnet.

### Gateway management

The `bay gateway` CLI provides full Headscale administration (status, nodes, users, keys, enrollment, routes, and ACL auditing) with no manual SSH session. `bay gateway --help` is the command reference.

### Additional capabilities

- **Multi-region support** -- one control region runs Headscale, other regions join via REST API
- **OIDC support** -- self-service VPN enrollment through identity providers
- **Embedded DERP relay** -- NAT traversal without relying on Tailscale's public DERP servers

## Multi-Region Support

Deploy the same stack to multiple regional servers from a single fleet:

- Regions are standard Ansible inventory groups (the `group` of a box in `bay.fleet.toml`) with per-region `group_vars/` overrides
- Cross-region service connectivity via the tailnet (Headscale coordinates all regions)
- Per-region domain configuration (e.g., `eu.example.com`, `na.example.com`)
- Shared Headscale coordination server on the control region
- Target one region or all of them: `bay deploy production -- --limit eu`, `bay deploy production`. Keep the box env (`production`) and add `--limit`. A deploy with the group name as the env (`bay deploy eu`) writes the receipt as `eu.json`, which `bay status` and `bay plan` do not read

See [multi-region.md](multi-region.md) for the full setup guide.

## Security Stack

- **Traefik v3.6 with host networking** -- `network_mode: host` preserves real client IPs through the entire request chain (no Docker NAT)
- **CrowdSec IDS/IPS** -- reads Traefik access logs and SSH auth logs, shares ban decisions with the nftables bouncer
- **nftables firewall** -- kernel-level packet filtering with CrowdSec blocklist integration and VPN IP whitelisting
- **Automated SSL** -- Let's Encrypt HTTP-01 challenge, per-domain certificates issued on first request (the optional tailnet ingress uses a DNS-01 wildcard cert, see [tailnet-ingress.md](tailnet-ingress.md))
- **Ansible Vault** -- encrypted secrets in `group_vars/<env>/secrets.yml`, resolved at deploy time into per-service `.env` files
- **SSH hardening** -- key-only auth, no root login, `MaxStartups 10:30:60` rate limiting, 30s `LoginGraceTime` (via `sshd_hardening` role supplementing geerlingguy.security)
- **Swap provisioning** -- configurable swap file (default 2G, swappiness 10) prevents OOM-kills on memory-constrained servers
- **CrowdSec bouncer binding** -- systemd drop-in auto-restarts the nftables bouncer when the CrowdSec agent restarts, preventing stale/empty blocklist sets after OOM recovery
- **Container memory limits** -- optional `memory` per app, service or shared resource prevents runaway containers from OOM-killing the host. Bay compiles it into `mem_limit` and an equal `memswap_limit`, so the container gets no swap and its memory pages never reach disk
- **Deploy lock** -- file-based mutex prevents concurrent deploys; stale locks (>1 hour) are automatically ignored
- **Deploy privilege separation** -- root bootstrap creates directories, then the deploy runs as the unprivileged app account (`app_user`, `bay` in the example fleet) with Docker group membership; only tasks that write system files switch to root

## Developer Experience

- **`bay` CLI** -- single entry point wrapping plan, up, deploy, provision, validate, gateway, vault, backup, test, and framework management
- **`bay plan` / `bay approve` / `bay up`** -- the daily flow: `bay plan` compares WANTED with PINNED (and with RUNNING on the box, with `--remote`) and gives a verdict, and `bay up` pins, compiles, commits and deploys the whole box environment (see [plan.md](plan.md))
- **`bay status --json`** -- the deploy receipt of every box (see [deploy-receipt.md](deploy-receipt.md)); plain `bay status` shows the version, the fleet and the feature flags
- **`bay validate`** -- pre-deploy config checks: YAML syntax, `services.yml` schema, inventory, vault keys (also runs automatically before every deploy)
- **`bay doctor`** -- environment probes: the fleet it picked, the installed CLI, SSH reachability, vault password, DNS resolution, box receipts, app repos
- **`bay fleet init`** -- make a fleet, or clone one with `--from`; `bay init` then writes a `bay.toml` in an app repo
- **Framework versioning** -- Bay releases are semver tags; `bay self update [--to <tag>]` and `bay self version` manage the installed copy
- **`bay --fleet <path>`** -- point any command at a fleet; the editable install runs a framework change at once, with no tag to cut
- **Config change detection** -- only redeploy services whose configuration has actually changed
- **Dry runs** -- `bay deploy production -- --check --diff` passes extra args through to Ansible and prints the reconciler's container plan (see [reconciler.md](reconciler.md#check-mode----check---diff))

## Infrastructure as Code

- **Ansible over SSH** -- no control-plane daemon or agent on the boxes; each deploy ships the reconciler (`bay_reconcile`) and runs it over SSH with the box's Python
- **Fleet model** -- Bay is installed once per machine; the fleet repo provides only configuration and records (`bay.fleet.toml`, `projects/<name>/` with its `bay.lock` and, for an app with no repo, its `bay.toml`, `plans/`, `group_vars/`, `hosts/`, and the compiled `services.yml`)
- **Idempotent deploys** -- run `bay up` or `bay deploy` repeatedly; only changed resources are updated
- **Restic backups** -- deduplicated, encrypted backups to S3-compatible storage with per-accessory repositories, systemd timers, configurable retention, and one-command restore (`bay backup restore <env> <accessory>`)
- **Watchtower** -- container image update monitoring with Telegram notifications; opt-in auto-update per service
- **Pluggable alerting** -- crash/build/deploy/disk/backup alerts to a list of recipients (Telegram, or a webhook in Campfire, Slack or plain-text format), each with its own severity floor, fail-open by design; see [alerting.md](alerting.md)
- **`bootstrap.sh`** -- one-command install of the `bay` command on a machine (see [install.md](install.md))

---

This document is updated as features are added. See also: [access-gateways.md](access-gateways.md), [multi-region.md](multi-region.md), [services.md](services.md), [design-decisions.md](design-decisions.md)
