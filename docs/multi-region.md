# Multi-Region Deployments

Bay supports deploying the same stack to multiple regional servers from a single consumer repo -- with zero framework changes. The CLI passes the `env` argument to Ansible as a host pattern (`-e target_host=<env>`), Ansible resolves groups and merges `group_vars/` by specificity, and Jinja2 lazy evaluation handles per-region configuration. Everything described here is standard Ansible behavior; Bay simply stays out of the way.

## Single-Server Setup

If you are deploying to a single server, multi-region files are unnecessary. Remove them from your consumer project:

```bash
rm hosts/production-multi-region
rm -r group_vars/eu group_vars/na
```

Use `hosts/production` with a single host entry and hardcode domains directly in `services.yml`. You can add multi-region support later by following this guide — no framework changes are needed.

## Inventory Structure

Multi-region uses Ansible's nested group pattern. Each region is its own group, and a parent group (typically `production`) aggregates them:

```ini
# hosts/production

[eu]
eu-server ansible_host=203.0.113.10

[na]
na-server ansible_host=198.51.100.20

[production:children]
eu
na
```

The CLI argument is just an Ansible host pattern passed as `target_host`:

| Command | What it targets |
|---------|-----------------|
| `bin/bay deploy eu` | Only `eu-server` |
| `bin/bay deploy na` | Only `na-server` |
| `bin/bay deploy production` | Both `eu-server` and `na-server` |

This works because the framework playbooks use `hosts: "{{ target_host }}"` -- whatever you pass as the environment argument becomes the Ansible host pattern. There is nothing region-specific in the framework.

## group_vars Layering

Ansible merges variables from all matching groups in the inventory hierarchy. The merge order (least to most specific) is:

```
all/  -->  production/  -->  eu/ (or na/)
```

The most specific value wins. This means you put shared configuration in `all/` or `production/`, and only override what differs per region.

### Directory layout

```
group_vars/
├── all/
│   ├── main.yml          # Shared config (stack_name, app_user, docker_users, etc.)
│   ├── services.yml      # Service definitions (with parameterized domains)
│   └── security.yml      # Firewall rules, CrowdSec config
├── production/
│   ├── main.yml          # Production-wide overrides
│   └── secrets.yml       # Shared secrets (vault-encrypted)
├── eu/
│   ├── main.yml          # domain_base, letsencrypt_email, region-specific vars
│   └── secrets.yml       # EU-specific secrets (optional, vault-encrypted)
└── na/
    ├── main.yml          # domain_base, letsencrypt_email, region-specific vars
    └── secrets.yml       # NA-specific secrets (optional, vault-encrypted)
```

### Example region overrides

**`group_vars/eu/main.yml`:**

```yaml
---
domain_base: eu.example.com
letsencrypt_email: admin@eu.example.com
```

**`group_vars/na/main.yml`:**

```yaml
---
domain_base: na.example.com
letsencrypt_email: admin@na.example.com
```

Everything else -- `stack_name`, `app_user`, `docker_users`, service definitions, security settings -- is inherited from `all/` and `production/` without repetition.

## Domain Parameterization

In a single-region setup, domains in `services.yml` are typically hardcoded:

```yaml
# Before: hardcoded domains (single-region)
services:
  gatus:
    access: vpn
    image: twinproduction/gatus:latest
    domains:
      - gatus.example.com
    ports:
      internal: 8080
```

For multi-region, replace hardcoded domains with a Jinja2 variable:

```yaml
# After: parameterized domains (multi-region)
services:
  gatus:
    access: vpn
    image: twinproduction/gatus:latest
    domains:
      - "gatus.{{ domain_base }}"
    ports:
      internal: 8080
```

With `domain_base` set to `eu.example.com` in the EU region and `na.example.com` in the NA region, the same `services.yml` produces different Traefik routing rules per server.

**Why this works without framework changes:** Ansible lazily evaluates Jinja2 expressions in `group_vars` values. The `{{ domain_base }}` template is not resolved when the YAML file is parsed -- it is resolved later, after all group_vars have been merged, at the point where the variable is actually used. By that time, `domain_base` has been set by the region-specific `group_vars/<region>/main.yml`.

See [services.md](services.md) for the full service schema reference.

## Per-Region Secrets

Each region can have its own vault-encrypted secrets file. This is useful when regions use different database credentials, API keys, or certificates.

```
group_vars/
├── production/
│   └── secrets.yml       # Shared secrets (e.g., secrets.backup_restic_password)
├── eu/
│   └── secrets.yml       # EU-only secrets (e.g., region-specific DB password)
└── na/
    └── secrets.yml       # NA-only secrets
```

Manage region-specific secrets with the vault CLI:

```bash
# Edit EU secrets
bin/bay vault edit eu

# Edit NA secrets
bin/bay vault edit na

# Edit shared production secrets
bin/bay vault edit production
```

Ansible merges secrets the same way it merges any other group_vars -- region-specific values override production-wide values. A secret defined in both `production/secrets.yml` and `eu/secrets.yml` will use the EU value when deploying to the EU server.

## VPN Access Per Region

If your regions need different WireGuard peers (e.g., EU office connects to the EU server, NA office connects to the NA server), set `vpn_allowed_ips` per region:

**`group_vars/eu/vpn_access.yml`:**

```yaml
---
vpn_allowed_ips:
  - 10.0.1.0/24    # EU office VPN range
  - 10.0.100.1/32  # Shared admin
```

**`group_vars/na/vpn_access.yml`:**

```yaml
---
vpn_allowed_ips:
  - 10.0.2.0/24    # NA office VPN range
  - 10.0.100.1/32  # Shared admin
```

Alternatively, if all regions share the same VPN peers, define `vpn_allowed_ips` once in `group_vars/production/` or `group_vars/all/` and skip the per-region files.

## Backup Isolation

Backup repositories are automatically isolated per region with no additional configuration. The backup role constructs restic repository paths using `inventory_hostname`:

```
s3:<endpoint>/<bucket>/<prefix>/<inventory_hostname>/<accessory>/
```

Since each region has a different host in the inventory (`eu-server`, `na-server`), each server gets its own repository path:

```
s3:s3.eu-central-1.amazonaws.com/my-bucket/backups/eu-server/postgres/
s3:s3.eu-central-1.amazonaws.com/my-bucket/backups/na-server/postgres/
```

This provides full isolation -- independent snapshots, independent retention, independent locking. A corrupted repository on one region does not affect the other.

If regions should use different S3 buckets or endpoints (e.g., for data residency), override `secrets.backup_s3_endpoint` or `secrets.backup_s3_bucket` in the region-specific `group_vars/<region>/secrets.yml`. See [backups.md](backups.md) for the full list of backup variables.

## Operational Workflows

### Deploy one region

```bash
bin/bay deploy eu
```

Targets only the EU server. Useful for canary deployments or region-specific maintenance.

### Deploy all regions

```bash
bin/bay deploy production
```

Targets all servers in the `production` group. Ansible runs plays against each host.

### Canary pattern

Deploy to one region first, verify it works, then deploy to the rest:

```bash
# Step 1: Deploy to EU
bin/bay deploy eu

# Step 2: Verify (check health endpoints, logs, monitoring)

# Step 3: Deploy to NA
bin/bay deploy na
```

This is the safest approach for production changes. There is no special canary feature -- you simply deploy to groups one at a time.

### Tag-scoped deploys and dry runs

`bin/bay deploy --help` covers tag filtering (`--tags`), region targeting, and ansible passthrough dry runs (`-- --check --diff`).

### Provision a new region

```bash
bin/bay provision eu
```

Provisions and hardens only the EU server. Add a new region by adding its host to the inventory, creating the region `group_vars/`, and running provision + deploy.

## Headscale Access Gateway in Multi-Region

When using `access_gateway: headscale` with multi-region, a single Headscale instance serves all regions. One region (the **control region**) runs the Headscale coordination server + the Tailscale daemon. All other regions run only the Tailscale daemon and register against the control region's Headscale via its REST API. There is no admin web UI — manage the tailnet with `bin/bay gateway` (it auto-targets the control host).

### How it works

Set `headscale_control_region` once in `group_vars/all/access_gateway.yml` to declare which region runs the Headscale server. All other regions automatically skip Headscale and register via API instead. Single-server setups leave this unset (everything defaults to control).

All hosts join the same tailnet, so VPN-protected services on any region are reachable from any other region's Tailscale interface.

### The control region is best a dedicated, low-surface host

The control region does not have to be one of your app regions, and in production it
should not be. The recommended pattern is a **dedicated control host** — a server that
runs only the tailnet/rig plumbing (Headscale + registry + monitoring + Traefik, and
optionally the [tailnet HTTPS ingress](access-gateways.md#tailnet-https-ingress-for-tailnet-only-services)
+ the per-device identity sidecar) and serves **no public app traffic**. Two reasons:

- **Small blast radius for the control plane.** The Headscale coordinator, its API key,
  and (if you define one) the default-deny [`headscale_acl_policy`](access-gateways.md#hardening-the-tailnet-acl--per-device-identity)
  are the trust root of the whole tailnet. Keeping app surface off that host shrinks
  what an app compromise can reach.
- **Fail-closed ingress wants an empty public socket.** The tailnet HTTPS ingress uses
  a fail-closed `websecure_tailnet` entrypoint; co-locating it with public sites would
  widen the blast radius of a split-entrypoint misconfig. A control host that serves no
  public app keeps that surface near zero.

Declare it the same way — `headscale_control_region: infra` — and put the host in its
own region group with just the rig services.

### Relocating the control region

The framework supports **moving the control region to a different host** (e.g. the
EU→infra migration that split the coordinator off the EU app region). It is not a
config-only flip — the new host has to **re-register** with Headscale and the tailnet IP
map shifts:

- Stand up the new control host and set `headscale_control_region` to its region. Deploy
  it **first** (it starts the coordinator), then re-deploy the former control host as a
  plain remote region so it re-registers via API.
- **Tailnet IPs can get reassigned.** Headscale allocates `100.64.0.x` addresses in
  registration order, so a relocation can renumber nodes. Anything that hardcodes a peer's
  tailnet IP — `headscale_server_tailnet_ip` per region, `tailnet_proxies` upstreams,
  cross-region `links:` env vars, and ACL `hosts:` entries — must be re-checked after the
  move. Prefer ACLs keyed by **node name** over IP so they survive renumbering (see
  [tailnet-ingress.md](tailnet-ingress.md)).
- Re-deploy the `headscale` tag last so split-DNS / `extra-records.json` reflects the new
  IPs, and verify with `bin/bay gateway nodes`.

### Example group_vars layout

Two-region setup with EU as the control region:

**`group_vars/all/access_gateway.yml`** (set once):

```yaml
---
access_gateway: headscale
headscale_domain: hs.eu.example.com
headscale_control_region: eu
```

**`group_vars/eu/main.yml`** (control region):

```yaml
---
region: eu
domain_base: eu.example.com
```

**`group_vars/na/main.yml`** (remote region):

```yaml
---
region: na
domain_base: na.example.com
```

**`group_vars/production/secrets.yml`** (vault-encrypted):

```yaml
---
secrets:
  # ...other secrets...

  # Required for remote region registration
  headscale_api_key: "your-api-key-here"
```

Note how the remote region's `main.yml` has no headscale-specific variables — all routing is derived from `headscale_control_region`.

### Deploy order

Multi-region + headscale requires deploying regions in a specific order:

```bash
# 1. Deploy control region first (starts Headscale server)
bin/bay deploy eu

# 2. Generate API key on control server
bin/bay gateway apikey

# 3. Add the API key to vault secrets
bin/bay vault edit production
# Add headscale_api_key inside the secrets dict

# 4. Deploy remote region (registers via API)
bin/bay deploy na
```

After both regions are deployed, verify connectivity:

```bash
# List all nodes across regions
bin/bay gateway nodes

# Check status
bin/bay gateway status
```

### Troubleshooting

- **"headscale_api_key is required"** — You deployed a remote region before adding the API key to vault. Run `bin/bay gateway apikey` on the control region, add the key inside the `secrets:` dict in `group_vars/production/secrets.yml`, then retry.
- **Remote node not appearing in `gateway nodes`** — Check that the Tailscale daemon on the remote host can reach `https://<headscale_domain>`. The domain must resolve to the control region's IP.
- **Gateway CLI targeting wrong host** — In multi-region, `bin/bay gateway` commands auto-target the control host. Use `--region <name>` to explicitly target a specific region.

See [access-gateways.md](access-gateways.md#multi-region-headscale) for the architecture overview and variable reference.

## Cross-Region Service Communication

When services need to communicate across regions (e.g., an app in NA connecting to a database in EU), use the `links:` field instead of routing through public domains. Links use the Headscale tailnet mesh for direct, encrypted communication between regions.

### Why not use public domains?

| Concern | Public Domain Path | Tailnet Link Path |
|---------|-------------------|-------------------|
| Latency | Internet round-trip | Direct WireGuard tunnel |
| Encryption | Double TLS (service → Traefik → service) | WireGuard tunnel encryption |
| VPN services | Blocked (NA IP not in `vpn_allowed_ips`) | Always reachable |
| DNS dependency | Requires public DNS resolution | Direct IP addressing |
| Exposure | Traffic traverses public internet | Traffic stays on tailnet |

### Network path

```
Region NA                                  Region EU
+------------------+                      +------------------+
| n8n container    |                      | postgres accessory|
| LINKS_DB_HOST=   |                      | expose: tailnet   |
| 100.64.0.3       |                      | bound 100.64.0.3:5432
+--------+---------+                      +--------+---------+
         |                                          |
    Docker bridge                              Host network
         |                                          |
    Host network                              WireGuard tunnel
         |                                          |
    WireGuard tunnel =============================  |
    (100.64.0.0/10)                          100.64.0.3
+------------------+                      +------------------+
```

Containers reach the tailnet via the host's network stack — no special Docker network configuration needed. The host routes tailnet traffic through the WireGuard tunnel.

### Adding a cross-region link

```bash
# Add n8n in NA, linked to postgres in EU
bin/bay service add n8n --region na --link postgres:eu

# Or add links to an existing service
bin/bay service edit n8n --link postgres:eu --link redis:eu
```

The link target must declare host exposure on its own stanza, otherwise it will not be reachable from the tailnet:

```yaml
# Accessory link target — top-level expose:
accessories:
  postgres:
    image: postgres:17
    port: "5432:5432"
    expose: tailnet     # ← required for cross-region link consumers
    regions: [eu]

# Service link target — nested ports.expose:
services:
  platform:
    access: vpn
    image: myorg/platform:latest
    domains: ["platform.example.com"]
    ports:
      internal: 5100
      expose: tailnet   # ← required for cross-region link consumers
    regions: [eu]
```

At deploy time, Ansible resolves the tailnet IP of the EU host and injects environment variables in the consumer container:

```
LINKS_POSTGRES_HOST=100.64.0.3
LINKS_POSTGRES_PORT=5432
LINKS_POSTGRES_URL=http://100.64.0.3:5432
```

Configure your application to use these variables for cross-region connections.

### Security model

- `expose: tailnet` (or `ports.expose: tailnet`) binds the target's port directly on the headscale tailnet interface (`100.64.0.0/10`) — not on `0.0.0.0`. The bind IP is the host's own tailnet address.
- For services this **bypasses Traefik** — no TLS termination, no IPAllowList, no middleware. The tailnet itself is the access boundary; only nodes registered with Headscale can reach the port.
- For accessories this is the same posture — the accessory is unprotected by Traefik anyway, so tailnet is its only ingress.
- Public internet traffic cannot reach a tailnet bind. The host's nftables baseline policy + the tailnet-restricted bind IP both contribute to the boundary.
- Pre-deploy, `bin/bay validate` rejects a cross-region `links:` whose target lacks `expose: tailnet` (or `expose: host`) — the symptom would be the consumer resolving env vars correctly but connecting to nothing.

### Removing links

```bash
# Remove a specific link
bin/bay service edit n8n --unlink postgres

# Deploy to apply changes
bin/bay deploy production
```

After removing a link, redeploy. If the link target served only that one consumer, you should also remove `expose: tailnet` from the target so the host bind goes away.

> **Migration note (framework v0.86.0+):** Previously the framework auto-rewrote a link target's port binding from `127.0.0.1:` to `0.0.0.0:`. A later change severed that rewrite to make exposure declarative; this fix closed the resulting gap by adding `ports.expose:` for services and fixing cross-region port resolution. If you have an accessory or service that's a cross-region link target, declare `expose: tailnet` (or `ports.expose: tailnet` for services) explicitly. Without an explicit `expose:`, the target has no host-port binding on its
> region's host, so the link resolves a port that nothing is listening on.

See [services.md](services.md#cross-region-links) for the full `links:` schema reference.

## When to Use Multi-Region vs Separate Consumers

| Scenario | Approach |
|----------|----------|
| Same stack in EU and NA | Multi-region (single consumer repo) |
| Same stack with minor per-region config differences | Multi-region with group_vars overrides |
| Completely different projects that happen to use Bay | Separate consumer repos |
| Different stacks with different services | Separate consumer repos |
| Staging and production of the same project | Multi-region (staging as a "region" group) |

**Use multi-region** when the service definitions are fundamentally the same and only configuration (domains, secrets, VPN peers) differs per location. The `services.yml` is shared, and per-region `group_vars/` handle the differences.

**Use separate consumer repos** when the projects have different services, different infrastructure requirements, or are managed by different teams. Each consumer repo gets its own `.bay/` clone, its own inventory, and its own `group_vars/` -- they are completely independent.

The dividing line: if two deployments share the same `services.yml` (possibly with parameterized values), they belong in the same consumer repo as a multi-region setup. If they need fundamentally different `services.yml` definitions, they should be separate consumers.
