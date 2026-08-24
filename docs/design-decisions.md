---
# Design Decisions

Evaluated features and architectural choices that were explicitly decided against (or deferred), with reasoning. This document exists so we don't re-investigate the same questions.

## DD-1: External Tailscale Control Server (decided: not prioritized)

**Question:** Should Bay support pointing to a different Tailscale/Headscale coordination server instead of self-hosting one?

**Status:** Not building first-class support. Manual integration path documented.

### What already works

The `headscale_domain` variable is fully configurable (not hardcoded). Three scenarios:

| Scenario | Status |
|----------|--------|
| Self-hosted local Headscale | Works, default path |
| Multi-region (one control region, others register via API) | Works |
| Fully external Headscale (`headscale_server: false` globally) | Partially works, untested, undocumented |
| Tailscale.com coordination server | Manual via WireGuard mode (documented in [access-gateways.md](access-gateways.md#using-tailscalecom-instead-of-self-hosted-headscale)) |

### Why we're not building it

1. **Self-hosted Headscale is the competitive advantage.** The `extra_records` + hot-reload split-DNS is a feature tailscale.com cannot offer ([tailscale/tailscale#1543](https://github.com/tailscale/tailscale/issues/1543), 869+ upvotes, open 5+ years). Building around external control servers means giving up the best feature.

2. **The WireGuard mode covers 80% of the use case.** Users with an existing Tailscale account can set `access_gateway: wireguard` with `vpn_allowed_ips: [100.64.0.0/10]` and get VPN access control immediately. They lose automatic split-DNS and the gateway CLI, but the core access control works.

3. **Remaining 20% has high engineering cost.** Full external support requires:
   - Gateway CLI adapter refactor (~800 lines of `docker exec` calls → REST API backend)
   - CoreDNS sidecar role (split-DNS when no local Headscale manages `extra-records.json`)
   - Parameterize hardcoded tailnet CIDR (`100.64.0.0/10` in nftables template)
   - Validate external server reachability in `bay validate`
   - Estimated: 12-18 days across 3 phases (see [external-tailscale-implementation-plan.md](external-tailscale-implementation-plan.md))

4. **Target user is already self-hosting.** Running one more auto-managed container (Headscale) is negligible overhead for someone deploying a full Docker stack via Ansible.

### If demand appears

The implementation plan is preserved in [external-tailscale-implementation-plan.md](external-tailscale-implementation-plan.md) and can be picked up as-is. The variable plumbing (`headscale_domain`, `headscale_server`, multi-region API registration) already exists.

---

## DD-2: Usage Without a Custom Domain (decided: not supported)

**Question:** Can Bay be used without owning a custom domain, relying on Headscale magic DNS instead?

**Status:** Not supported. A custom domain is a hard requirement.

### Why a custom domain is required

The framework's TLS model is built entirely around Let's Encrypt HTTP-01 challenge:

```
# roles/traefik/templates/traefik.yml.j2
certificatesResolvers:
  letsencrypt:
    acme:
      httpChallenge:
        entryPoint: web
```

HTTP-01 requires:
1. **Domain publicly resolvable** via DNS (A record → server IP)
2. **Port 80 reachable** from the internet (Let's Encrypt posts the challenge)

Without a real domain, cert issuance fails and every service gets browser certificate warnings.

### What depends on `domain_base`

| Component | How it uses `domain_base` | Breaks without it? |
|-----------|--------------------------|-------------------|
| Service routing | `Host(\`app.{{ domain_base }}\`)` in Traefik labels | Yes — no Host match, no routing |
| TLS certificates | Let's Encrypt HTTP-01 against public domain | Yes — cert issuance fails |
| Headscale enrollment | `headscale_domain` must be public for clients to reach coordination server | Yes — clients can't join tailnet |
| Webhook receiver | Public domain for GitHub/GitLab webhooks | Yes — no external delivery |
| `bay doctor` | Validates DNS resolution of `domain_base` | Yes — reports failure |
| Split-DNS extra records | Maps service domains → tailnet IPs | No — this part works with any domain string |
| Traefik Host matching | Matches incoming `Host` header | No — works with any string if client sends it |

### Why magic DNS doesn't solve it

Headscale magic DNS (`*.tailnet.internal`) provides name resolution **within the tailnet only**:

- Tailnet clients can resolve `app.internal.local` → tailnet IP via split-DNS `extra_records`
- Traefik Host rules match on any string — they don't care if it's a real domain
- IPAllowList enforcement works regardless of domain ownership

But:
- **Let's Encrypt can't issue certs** for domains that don't resolve publicly — self-signed fallback means browser warnings on every page load
- **Public services are impossible** — external clients get NXDOMAIN
- **Headscale enrollment requires a public endpoint** — clients need to reach `https://hs.example.com` to join the tailnet in the first place
- **Webhook delivery fails** — GitHub/GitLab can't POST to a domain that doesn't exist

A "VPN-only mode" with self-signed certs is theoretically possible but would require changes in ~4-5 places (conditional certresolver in service/headscale/webhook templates, self-signed cert generation task, doctor validation bypass). The result is a degraded experience with constant browser warnings.

### Pragmatic alternative

A domain costs $2-5/year and gives full automatic HTTPS with zero framework changes. This is the recommended path for all users, including those running purely internal/VPN infrastructure.

### What would need to change (preserved for reference)

If demand justified a "domain-free" mode:

1. Add `tls_mode: self-signed | letsencrypt` variable (default: `letsencrypt`)
2. Conditional `tls.certresolver` in `_service.j2`, `_headscale.j2`, `_webhook_receiver.j2`
3. Self-signed cert generation task in the traefik role
4. Skip domain validation in `doctor.py` when `tls_mode: self-signed`
5. Update wizard to handle domain-free setup path
6. Headscale enrollment would still need *some* reachable endpoint — either a public IP with self-signed cert (client must `--accept-risk`) or a pre-existing tailnet

Estimated effort: ~3-5 days of framework development + testing.

---

## Index

| ID | Decision | Status | Date |
|----|----------|--------|------|
| DD-1 | External Tailscale control server | Not prioritized | 2025 |
| DD-2 | Usage without custom domain | Not supported | 2026-03 |
