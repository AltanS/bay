# External Tailscale Control Server — Implementation Plan

> **Superseded / historical (M38 research).** This plan was never built — the self-hosted Headscale path shipped instead and is the supported model (tailnet HTTPS ingress with DNS-01 wildcard certs, default-deny ACL, per-device identity). Kept for reference only; do not treat its steps as live work. See [access-gateways.md](access-gateways.md) and [tailnet-ingress.md](tailnet-ingress.md) for the current architecture.
>
> Reference document from M38 research. This design was evaluated but the decision was made to not build first-class tailscale.com support.

---

## Goal

Concrete implementation plan incorporating S1 research findings and peer review feedback from two independent DevOps reviewers (infra architect + security SRE).

## Final Decision

**Do NOT build `access_gateway: tailscale` as a first-class mode.**

### Rationale

- Bay's target user is already self-hosting — running Headscale is not additional burden (one auto-managed container).
- Self-hosted Headscale is Bay's competitive advantage: `extra_records` + hot-reload is a feature tailscale.com can't match ([tailscale/tailscale#1543](https://github.com/tailscale/tailscale/issues/1543), 869 upvotes, open 5 years).
- The existing `access_gateway: wireguard` mode with tailnet CIDR in `vpn_allowed_ips` already covers tailscale.com users at 80% functionality.
- The remaining 20% (automatic DNS, gateway CLI) isn't worth the engineering cost for a user base that may not exist.
- No architectural changes to the framework are needed.

### What WILL be done

1. **P0 fixes** (benefit all users): `no_log: true` on auth key tasks, parameterize nftables CIDR.
2. **Gateway CLI adapter refactor**: split into `LocalHeadscaleBackend` / `RemoteHeadscaleBackend` (improves multi-region, sets foundation if tailscale support is ever needed).
3. **Documentation**: add a doc explaining the manual tailscale.com integration path ("install tailscale, join tailnet, set `vpn_allowed_ips` to tailnet CIDR, manage DNS yourself").
4. **Preserve this spec as a design record** — if demand appears, the implementation plan is ready.

### What will NOT be built

- `access_gateway: tailscale` mode
- CoreDNS sidecar role
- `TailscaleBackend` for gateway CLI
- Tailscale OAuth token exchange in `tailscale_register` role
- Wizard tailscale.com option

---

## P0 — Pre-existing Issues (Fix Before Any New Code)

These are bugs/gaps found during review that exist in the current codebase. Fix them independently of the external control server feature.

### 1. `no_log: true` on auth key tasks

`tailscale_register/tasks/main.yml` leaks auth keys to Ansible stdout and CI logs. Tasks at lines ~64-80 (local path) and ~138-164 (remote path) that handle pre-auth keys, API keys, and `tailscale up --authkey` must have `no_log: true`. This is a **pre-existing secret leak** that external control makes worse (longer-lived, more powerful keys).

### 2. Hardcoded tailnet CIDR in nftables

`nftables.conf.j2` line ~44: `ip saddr 100.64.0.0/10` is hardcoded instead of using `vpn_allowed_ips` or `headscale_tailnet_cidr`. Works by accident today (Tailscale uses the same CGNAT range), but breaks for any custom prefix. Replace with `{{ vpn_allowed_ips | join(', ') }}` or equivalent.

### 3. Gateway CLI prints secrets to terminal

`gateway.py` — pre-auth keys and API keys are printed to stdout. With external control, these may be longer-lived and more powerful. Add `[sensitive]` markers or redaction for log-safe output.

---

## Design Decisions (Informed by Reviewer Feedback)

### D1: Mode naming — use existing `headscale` mode, not a new enum

Reviewer #1 identified that `headscale-remote` may not need a new mode. The `headscale_server` variable is already derived and triggers the remote API path when `false`. Setting `headscale_server: false` for ALL hosts already works for multi-region non-control regions.

**Decision:** Two modes, not four:

```yaml
access_gateway: headscale        # Self-hosted OR external Headscale
access_gateway: tailscale        # Tailscale.com (NEW)
```

For external Headscale, the existing `headscale` mode with explicit config:
```yaml
access_gateway: headscale
headscale_server: false          # No local Headscale container
headscale_external_url: https://headscale.external.example.com
# secrets.headscale_api_key required for all operations
```

This reduces conditional branches and leverages existing code paths.

### D2: Gateway CLI — adapter/strategy pattern

The CLI rewrite is the **single biggest engineering effort**. Every command in `gateway.py` (~800 lines) calls `docker exec headscale headscale <subcommand>`. For external control, this must become API calls.

**Decision:** Refactor to a backend protocol:

```python
class GatewayBackend(Protocol):
    def list_nodes(self) -> list[Node]: ...
    def create_user(self, name: str) -> User: ...
    def generate_key(self, user: str, expiry: str, reusable: bool) -> str: ...
    def delete_node(self, name: str) -> None: ...
    # ... etc

class LocalHeadscaleBackend(GatewayBackend):    # docker exec
class RemoteHeadscaleBackend(GatewayBackend):   # REST API /api/v1/*
class TailscaleBackend(GatewayBackend):         # Tailscale API v2
```

### D3: Binary trust model — add per-service IP restriction for external control

With self-hosted Headscale, "on tailnet = allowed" is acceptable because you own the control plane. With external control, a compromised admin account means rogue nodes pass IPAllowList for ALL VPN services.

**Decision:** Keep the binary model as default but add an optional `vpn_restrict_ips` per-service field:

```yaml
services:
  admin-panel:
    access: vpn
    vpn_restrict_ips:          # Optional: tighter than tailnet CIDR
      - 100.64.0.1             # Only the server's own tailnet IP
      - 100.64.0.42            # Specific admin device
```

When omitted, falls back to full tailnet CIDR (current behavior). This is additive, not breaking.

### D4: CoreDNS — conditional component, not always-on sidecar

Research confirmed that CoreDNS and Traefik have zero feature overlap — they operate at entirely different layers (DNS L3/L4 vs HTTP L7). The question is not whether CoreDNS conflicts with Traefik, but whether it is needed at all for a given architecture.

**When CoreDNS IS deployed:**

- `access_gateway: tailscale` — CoreDNS is required. Tailscale.com split-DNS needs a restricted nameserver on the tailnet, and there is no self-hosted control plane to provide it.
- `access_gateway: headscale` with `headscale_server: false` — CoreDNS is required. An external Headscale cannot manage `extra-records.json` on the local server, so there is no MagicDNS source for VPN domain records.

**When CoreDNS is NOT deployed:**

- `access_gateway: headscale` with `headscale_server: true` — No CoreDNS. Self-hosted Headscale already solves split-horizon DNS via MagicDNS + `extra-records.json` (generated from `services.yml`). Adding CoreDNS would duplicate functionality and add operational overhead.
- `access_gateway: wireguard` — No CoreDNS. Plain WireGuard has no tailnet and no split-DNS concept.

**Rationale:** For the majority of users (self-hosted Headscale), split-DNS is already solved. Docker's built-in DNS handles container-to-container resolution. Adding CoreDNS as an always-on sidecar would introduce operational overhead with zero benefit: port 53 conflicts with systemd-resolved, an extra container to monitor, and a new failure mode for something that already works.

**CoreDNS role design (kept simple):**

- `hosts` plugin with a file generated from `services.yml` (same data source as `extra-records.json.j2`)
- `bind` to tailnet IP only (never `0.0.0.0`)
- `forward` unmatched queries to `1.1.1.1` / `9.9.9.9`
- `health` endpoint for monitoring
- No custom plugins, no Docker socket access, no system resolver changes
- Gated in `access_gateway` role: `when: access_gateway == 'tailscale' or (access_gateway == 'headscale' and not headscale_server)`

**Multi-region:** When CoreDNS is deployed, one "primary" instance (on the first/control region) aggregates VPN records from ALL regions. Zone template loops `groups.get('all')` — identical pattern to `extra-records.json.j2`.

**Custom domains outside the wildcard** (e.g., `blog.example.com` alongside `*.example.com`): CoreDNS serves records for all VPN domains regardless of base domain. The only extra work is the split-DNS config — each unique base domain with VPN services needs a restricted nameserver entry. This is a one-time manual step per new base domain; `bay validate` detects missing entries.

**M39 (Multi-Domain Support) is absorbed into M38.** The `services.yml` schema already accepts any domain string. Traefik already routes by Host header and auto-issues per-domain certs via HTTP-01. No schema or template changes needed — the only gap was split-DNS, which CoreDNS solves (when deployed).

**Asymmetry for tailscale.com:** The Tailscale API v2 does not expose split-DNS configuration programmatically. Users must manually add restricted nameservers in the Tailscale admin console.

**Decision:** CoreDNS is conditional — deployed only when the architecture requires it. Post-deploy outputs the exact nameserver entries needed (when applicable). `bay validate` checks resolution and tells you what's missing.

### D5: Credential lifecycle

| Credential | Storage | Rotation | Expiry Detection |
|-----------|---------|----------|-----------------|
| Headscale API key | `secrets.headscale_api_key` (vault) | Manual: `bay gateway apikey` → re-vault | `bay validate` tests API call |
| Tailscale OAuth client secret | `secrets.tailscale_client_secret` (vault) | Manual: regenerate in Tailscale console → re-vault | `bay validate` tests token exchange |
| Tailscale auth keys | Generated at deploy time (short-lived, 10min) | N/A (ephemeral) | N/A |

- OAuth tokens are exchanged at runtime (short-lived), client secret is long-lived in vault
- `bay validate` must test API connectivity with stored credentials
- `bay gateway rotate-credentials` as a future convenience command (not v1)

### D6: Failure modes and monitoring

| Failure | Impact | Mitigation |
|---------|--------|-----------|
| External control plane down | New deploys blocked, existing tunnels survive | `bay validate` pre-check; node keys valid 180 days |
| CoreDNS down | VPN domains unreachable, services look healthy | Health check in `bay gateway status`; Gatus probe |
| API rate limiting (tailscale.com) | Partial deploy failure | Generate one reusable auth key per deploy, not one per server |
| Node key expiry (180 days) | Silent connectivity loss | `bay gateway check-expiry` command; validate warning |
| Config drift (external ACLs changed) | VPN services silently break | `bay validate` reads ACL policy via API, warns on mismatch |
| Auth key expiry race | Key expires before `tailscale up` runs | Use 10-minute expiry (not 5), configurable |

---

## Implementation Phases

### Phase 0: Pre-existing fixes (independent of M38)

1. Add `no_log: true` to auth key tasks in `tailscale_register`
2. Parameterize tailnet CIDR in `nftables.conf.j2`
3. Redact sensitive output in gateway CLI

### Phase 1: External Headscale (config of existing mode)

1. Allow `headscale_server: false` globally with `headscale_external_url`
2. Skip headscale/headplane container deployment (already conditional)
3. Gateway CLI: extract `RemoteHeadscaleBackend` from existing remote API code
4. CoreDNS role (conditional): generate zone from `services.yml`, bind to tailnet interface — deployed only when `headscale_server: false` (external Headscale cannot manage `extra-records.json` locally)
5. `bay validate`: check external Headscale API connectivity + credentials
6. Wizard: "External Headscale" option with URL + API key prompts
7. Documentation update

### Phase 2: Tailscale.com

1. New `access_gateway: tailscale` mode in `access_gateway` role
2. `tailscale_register` role: skip `--login-server`, use `--authkey` from OAuth-generated key
3. `TailscaleBackend` for gateway CLI (Tailscale API v2 + OAuth token exchange)
4. CoreDNS deployment (conditional): always deployed for `tailscale` mode — Tailscale.com has no server-side mechanism for hosting custom DNS records on the tailnet
5. Post-deploy: output required split-DNS entries per unique base domain
6. `bay validate` — Tailscale split-DNS verification:
   - Collect all `access: vpn` domains from `services.yml`
   - Group by base domain (e.g., `example.com`, `other.example.com`)
   - Query CoreDNS directly (`dig @<tailnet-ip> <domain>`) to confirm zone has the record
   - Query system DNS to confirm split-DNS routes through tailnet (resolves to tailnet IP, not public IP)
   - On failure: output exact Tailscale admin console instructions (nameserver IP + domain list)
   - Skip check gracefully if tailscale not connected on validate machine
7. `bay validate`: test Tailscale API connectivity, check device count vs. plan limits
8. Wizard: "Tailscale.com" option with OAuth client ID/secret prompts, trust model warning
9. Documentation update

### Phase 3: Hardening (before production use)

1. Per-service `vpn_restrict_ips` support in Traefik templates
2. CoreDNS health monitoring in `bay gateway status`
3. `bay gateway check-expiry` — warn on nodes approaching key expiry
4. ACL drift detection via API during `bay validate`
5. `--accept-dns=false` on server nodes (prevent external DNS overriding resolv.conf)

---

## Migration Path

### Self-hosted Headscale -> External Headscale

1. Deploy CoreDNS sidecar on current setup (test in parallel)
2. Set up external Headscale, create users + API key
3. Change config: `headscale_server: false`, set `headscale_external_url`
4. On each host: `tailscale up --reset --login-server <new-url> --authkey <key>`
5. Verify node registration, DNS resolution, VPN access
6. Remove old Headscale container (`docker rm headscale headplane`)
7. Update `extra-records` / split-DNS on external server

**Critical:** `--reset` flag is required to clear old control server state from `/var/lib/tailscale/`. Without it, `tailscale up` fails silently.

### Self-hosted Headscale -> Tailscale.com

Same as above, except:
- Step 4: `tailscale up --reset --authkey <key>` (no `--login-server`, defaults to tailscale.com)
- Step 7: Manual split-DNS config in Tailscale admin console

### Rollback

1. Re-enable `headscale_server: true`
2. `tailscale up --reset --login-server <old-url> --authkey <key>` on each host
3. Headscale container redeploys on next `bay deploy`
4. CoreDNS can remain or be removed

---

## Effort Estimate

| Phase | Scope | Effort |
|-------|-------|--------|
| P0 | Pre-existing fixes | 1 day |
| Phase 1 | External Headscale | 3-5 days |
| Phase 2 | Tailscale.com | 5-7 days |
| Phase 3 | Hardening | 3-5 days |
| **Total** | | **12-18 days** |

The gateway CLI adapter refactor (Phase 1-2) is the single biggest line item at ~5-7 days across both phases.
