# External Tailscale Control Server — Feasibility Research

> **Superseded / historical (M38 research).** The self-hosted Headscale path shipped and is the supported model — including a tailnet HTTPS ingress (DNS-01 wildcard certs), a default-deny ACL, and per-device identity that external tailscale.com cannot provide. This document is kept for the analysis only; do not treat its plans as live. See [access-gateways.md](access-gateways.md) and [tailnet-ingress.md](tailnet-ingress.md) for the current architecture.
>
> Reference document from M38 research. This design was evaluated but the decision was made to not build first-class tailscale.com support.

---

## Goal

Investigate the viable strategies for connecting Bay-managed servers to an external Tailscale control server instead of a self-hosted Headscale instance. Document trade-offs, limitations, and compatibility with the existing Bay gateway architecture.

---

## Current Architecture (Baseline)

Bay supports two VPN backends via `access_gateway: wireguard | headscale`:

- **WireGuard** — manual peer configuration, static `vpn_allowed_ips`
- **Headscale** — self-hosted Tailscale control server, automatic enrollment, split-DNS, multi-region (M28)

The Headscale path deploys:
1. **Headscale coordination server** — Docker container on the control region, Traefik-routed at `headscale_domain`
2. **Tailscale daemon** — installed on every host, registered via pre-auth key
3. **Headplane** — admin UI on localhost:3000
4. **Split-DNS** — `extra-records.json` maps VPN service domains → tailnet IPs, hot-reloaded by Headscale
5. **Embedded DERP** — region_id 999, STUN on UDP 3478
6. **Traefik IPAllowList** — `vpn_allowed_ips` set to `headscale_tailnet_cidr` (100.64.0.0/10)

Key files:
- `roles/access_gateway/` — orchestrates backend selection
- `roles/headscale/` — provisions coordination server
- `roles/tailscale_node/` — installs tailscale daemon
- `roles/tailscale_register/` — registers host with headscale (local docker exec or remote REST API)
- `roles/headplane/` — admin UI
- `src/bay_cli/commands/gateway.py` — CLI commands for node/user/key/route management

---

## Research Findings

### 1. Tailscale.com as Control Server

**How it works:** `tailscaled` connects to tailscale.com by default (no `--login-server` needed). Authentication via:
- Interactive browser login (`tailscale login`)
- Pre-auth key (`tailscale up --authkey <KEY>`) — ideal for Ansible automation
- Auth keys can be one-off or reusable, with pre-assigned tags (e.g., `tag:server`)

**Tailscale API v2** (`https://api.tailscale.com/api/v2/`):
- Authentication: API access tokens (1-90 day expiry) or OAuth clients (preferred for automation)
- Endpoints: list/create/delete devices, create auth keys, read/write ACL policy
- OAuth clients with `auth_keys` scope can programmatically create tagged auth keys

**Pricing (as of 2026):**

| Plan | Users | Devices | Price | ACL Granularity |
|------|-------|---------|-------|-----------------|
| Personal (Free) | 3 | 100 | Free | Full custom groups/users |
| Starter | varies | 100 + 10/user | $6/user/mo | Autogroups only (admin, member) |
| Premium | varies | 100 + 20/user | $18/user/mo | Full custom ACLs |

Free tier: 3 users, 100 devices, full ACLs, subnet routes, exit nodes, MagicDNS — sufficient for most Bay deployments.

**Custom DERP:** Yes, configurable via `derpMap` in the ACL policy file. Can selectively disable Tailscale's built-in regions.

**Tailscale Funnel/Serve vs Traefik:** Funnel exposes services via Tailscale's relay infrastructure (hides device IP). Not a replacement for Traefik — Traefik handles HTTP routing, middleware, Let's Encrypt, host networking. They are complementary layers.

**Feasibility: HIGH.** The API provides everything needed for Ansible automation. Auth keys with tags replace Headscale's user/preauthkey flow. Free tier is sufficient.

### 2. Remote Headscale Instance

**Connection:** Same `--login-server` flag, just pointing to a different URL:
```bash
tailscale up --login-server https://headscale.external.example.com --authkey <KEY>
```

**API authentication:** REST API at `/api/v1/*` with Bearer token. Same API Bay already uses for multi-region remote registration. gRPC also available on tcp/50443 (TLS required).

**What changes vs. local Headscale:**
- Skip Headscale container deployment (no `headscale` role)
- Skip Headplane deployment
- Use remote REST API for all operations (already implemented for multi-region non-control regions)
- DNS/extra-records managed externally — Bay can't hot-reload split-DNS

**Who manages it?** The external Headscale operator handles upgrades, config, DERP, ACLs. Bay only consumes the API for node registration and key management.

**Feasibility: HIGH.** Multi-region remote registration already uses the REST API pattern. Extending this to "all regions are remote" is straightforward.

### 3. Split DNS and MagicDNS

**Current model:** Headscale role generates `extra-records.json` from all `access: vpn` services across all regions. Headscale hot-reloads this file. Tailnet clients resolve VPN domains → tailnet IPs, so traffic flows through the WireGuard tunnel.

**With tailscale.com:**
- MagicDNS provides automatic `hostname.tailnet-name.ts.net` records
- Split DNS configured via admin console (restricted nameservers per domain)
- **No extra_records equivalent** — tailscale.com does not support injecting custom A records into MagicDNS
- **Workaround:** Run a small DNS server (CoreDNS/dnsmasq) on the tailnet, configure split DNS to route VPN domains to it. Or use Tailscale's `--advertise-routes` to make services reachable by tailnet IP directly.

**With remote Headscale:**
- Extra records managed on the remote server — Bay cannot push `extra-records.json`
- Option A: API endpoint to update extra records (not available in current Headscale API)
- Option B: SSH/rsync the records file to the remote server and signal reload
- Option C: Accept that split-DNS is managed externally, Bay provides a manifest of VPN domains

**Impact:** This is the **biggest gap**. Bay's automatic split-DNS generation is a key feature that breaks when the control server is external.

### 4. ACL Mapping

**Bay's model:** `access: vpn` → Traefik IPAllowList allows `100.64.0.0/10`. Binary: you're on the tailnet or you're not. No per-service ACL granularity at the VPN layer.

**Tailscale.com ACLs:**
```jsonc
{
  "tagOwners": { "tag:server": ["autogroup:admin"] },
  "acls": [
    {"action": "accept", "src": ["autogroup:member"], "dst": ["tag:server:443"]},
    {"action": "accept", "src": ["autogroup:admin"], "dst": ["tag:server:*"]}
  ]
}
```
- Tags (`tag:server`) applied to Bay-managed nodes via auth key
- ACLs control which tailnet members reach which ports — complementary to Traefik IPAllowList
- Could enable per-service VPN access (e.g., admin panel only for `group:admin`)

**Headscale ACLs:** Compatible huJSON format, but no `autoApprovers` or Grants. Tags work via users.

**Assessment:** Current Bay model (binary VPN allow/deny) works unchanged with any backend. Per-service ACLs would be a future enhancement, not a blocker.

### 5. DERP Relay Servers

**Current:** Headscale embeds a DERP server (region_id 999, STUN on UDP 3478). Used as fallback when direct P2P negotiation fails.

**With tailscale.com:** Global DERP network included automatically. No configuration needed. Custom DERP servers can be added via policy file. Latency typically better (geographically distributed relays).

**With remote Headscale:** DERP configuration is on the remote server. If it runs embedded DERP, clients use it. If not, clients need access to some DERP server. Can configure Tailscale's public DERP map as fallback (`derp.urls` in headscale config).

**Assessment:** DERP is transparent to Bay. No role changes needed — DERP is a control server concern.

---

## Component Impact Analysis

| Component | tailscale.com | Remote Headscale | Effort |
|-----------|--------------|-----------------|--------|
| `access_gateway` role | New mode: `tailscale` | New mode: `remote-headscale` | Medium |
| `headscale` role | Skip entirely | Skip entirely | None (conditional) |
| `tailscale_node` role | No change (same daemon) | No change | None |
| `tailscale_register` role | Use `tailscale up --authkey` (no `--login-server`) | Use `tailscale up --login-server <url> --authkey` | Low |
| `headplane` role | Skip entirely | Skip entirely | None (conditional) |
| Split-DNS (`extra-records.json`) | **Lost** — needs workaround (CoreDNS or tailnet IP routing) | **Lost** — needs external management | High |
| Traefik IPAllowList | Same (tailnet CIDR) | Same (tailnet CIDR) | None |
| Docker Compose templates | Skip headscale/headplane services | Skip headscale/headplane services | Low |
| Gateway CLI | Replace `docker exec` with Tailscale API v2 | Replace `docker exec` with remote REST API | High |
| Wizard / onboarding | New prompts for mode selection and external credentials | New prompts | Medium |
| `group_vars` schema | New vars: `tailscale_authkey`, `tailscale_tailnet`, API credentials | New vars: `headscale_external_url`, API key | Low |

---

## Recommendation

### Proposed `access_gateway` Modes

```yaml
access_gateway: wireguard       # Manual WireGuard (existing)
access_gateway: headscale       # Self-hosted Headscale (existing)
access_gateway: tailscale       # Tailscale.com coordination (NEW)
access_gateway: headscale-remote # External Headscale instance (NEW)
```

### Priority

1. **`headscale-remote` — implement first.** The multi-region remote registration path already uses the Headscale REST API. Generalizing "all regions are remote" is the smallest delta from current code. Split-DNS is the only hard problem (push extra-records to external server or accept external management).

2. **`tailscale` — implement second.** Requires a new API client (Tailscale API v2 vs Headscale API v1), new auth flow (OAuth client or API token), and the split-DNS workaround is harder (no extra_records at all). But it's the most appealing for users who don't want to manage Headscale.

### Key Design Decision: Split-DNS

The critical question is how VPN service domains resolve to tailnet IPs when Bay doesn't control the coordination server:

| Strategy | tailscale.com | headscale-remote | Complexity |
|----------|--------------|-----------------|------------|
| **A. Tailnet IP in Traefik** — clients access services by tailnet IP directly, no DNS magic | Works | Works | Low, but poor UX (no domain names) |
| **B. CoreDNS sidecar** — deploy a small DNS server on the tailnet, configure split DNS to route VPN domains to it | Works | Works | Medium — new container, DNS zone generation |
| **C. External DNS management** — document that the operator must configure DNS on the external control server | N/A (tailscale.com has no extra_records) | Works | Low for Bay, high for operator |
| **D. Tailscale Serve** — register each VPN service as a Tailscale Serve endpoint (bypasses Traefik for VPN traffic) | Works (requires Funnel-capable plan) | N/A | High — architectural change |

**Recommendation:** Strategy **B (CoreDNS sidecar)** for both modes. Deploy a lightweight CoreDNS container on the tailnet that serves `extra-records.json` equivalent zone data. Configure the external control server's split DNS to route VPN domains to the CoreDNS instance's tailnet IP. This preserves Bay's automatic DNS generation while decoupling from control server management.

### Components That Need Changes

1. `roles/access_gateway/` — add `tailscale` and `headscale-remote` mode handling
2. `roles/tailscale_register/` — conditional: skip `--login-server` for tailscale.com, use external URL for headscale-remote
3. `roles/deploy_stack/templates/` — conditionally skip headscale/headplane services
4. `src/bay_cli/commands/gateway.py` — new API clients for Tailscale API v2 and remote Headscale
5. New role: `roles/coredns/` (optional, for split-DNS sidecar strategy)
6. Wizard updates for mode selection and credential prompts
7. `group_vars` schema: new variables for external server configuration
8. Documentation: access-gateways.md update
