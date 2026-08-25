# Access Gateways

Bay supports three access-gateway backends, selected with the `access_gateway` variable in `group_vars`:

| Backend | What it is | Node management |
|---|---|---|
| `wireguard` | **Default.** Plain WireGuard on the VPS, peers configured by hand. | None — you edit `vpn_allowed_ips`. |
| `headscale` | Self-hosted Tailscale control plane, automatic tunnels, split-DNS, ACLs. | Full, via `bin/bay gateway`. |
| `none` | No private overlay at all. Everything is public. | None. |

`wireguard` and `headscale` both terminate in a WireGuard tunnel and then feed the same downstream pipeline -- nftables firewall, CrowdSec IDS, Traefik IPAllowList. `none` is for an operator who wants a plain public deploy and runs no overlay network; with it, `access: vpn` is rejected at validation time rather than silently serving nobody.

**If you are adding a backend, read [The adapter contract](#the-adapter-contract) first.** The rest of the framework never asks which backend is active.

## WireGuard Gateway

The default gateway. WireGuard runs directly on the VPS as a kernel module with a `wg0` interface. Peers are configured manually -- you generate key pairs, assign tunnel IPs, and list allowed IPs in `vpn_allowed_ips`. Simple and transparent, but every new client requires server-side configuration and a redeploy.

### Configuration

```yaml
# group_vars/all/main.yml (or vpn_access.yml)

# access_gateway: wireguard    # default, can be omitted

vpn_allowed_ips:
  - 10.0.0.2/32               # alice
  - 10.0.0.3/32               # bob
```

Each peer needs a WireGuard config file on their device with the server's public key, endpoint, and their assigned IP. Adding or removing peers means editing `vpn_allowed_ips` and running `bin/bay provision production`.

### Traffic flow

```
 Client                         VPS
 ┌──────────────┐     ┌──────────────────────────────────────┐
 │ WireGuard app │     │                                      │
 │ (manual keys) ├────►│ wg0 interface (10.0.0.1)             │
 └──────────────┘     │   │                                   │
   WireGuard tunnel   │   ▼                                   │
   (UDP 51820)        │ nftables ──► CrowdSec bouncer         │
                      │   │                                   │
                      │   ▼                                   │
                      │ Traefik (host network)                │
                      │   IPAllowList: vpn_allowed_ips        │
                      │   │                                   │
                      │   ▼                                   │
                      │ service containers (bridge network)   │
                      └──────────────────────────────────────┘
```

1. Client connects via WireGuard app using a manually configured profile
2. Traffic arrives on `wg0` with the client's tunnel IP as the source
3. nftables allows the packet; CrowdSec bouncer checks its blocklist
4. Traefik matches the source IP against `vpn_allowed_ips` via IPAllowList middleware
5. Authorized requests are routed to the target service container

## Headscale Gateway

Available since the first tailnet release. [Headscale](https://github.com/juanfont/headscale) is a self-hosted implementation of the Tailscale coordination server. Instead of manual key exchange, clients install the standard Tailscale app and join your private tailnet. Enrollment is handled via OIDC (self-service) or pre-auth keys (scripted). The VPS itself runs a Tailscale daemon that joins the same tailnet, terminating the tunnel locally.

Two components are deployed:

- **Headscale** -- coordination server, runs behind Traefik with a public domain for client enrollment
- **Tailscale daemon** -- runs on the VPS, joins the tailnet, provides the tunnel interface (`tailscale0`)

There is no admin web UI. All node, user, and key management happens through the
`bin/bay gateway` CLI (which drives the Headscale CLI over the deploy connection)
or, optionally, OIDC self-service enrollment.

### Headscale user model

Bay creates two kinds of Headscale users:

1. **Server node user** (automatic) -- created by the `tailscale_register` role during deploy. Defaults to `stack_name` (e.g., `myapp`). The VPS itself registers under this user. You do not need to manage this user manually.

2. **Device users** (operator-created) -- for enrolling end-user laptops, phones, and other client devices. Created via `bin/bay gateway add-user <name>` (e.g., `alice`, `mobile-devices`). Each device user gets their own pre-auth keys and appears separately in `bin/bay gateway users` / `bin/bay gateway nodes`.

This separation ensures that when multiple Bay projects share one Headscale server, each project's VPS nodes are isolated under their own user (matching `stack_name`), while device users remain project-specific by convention.

The server node hostname in Headscale follows the pattern `{stack_name}` for single-server setups and `{stack_name}-{region}` for multi-region (e.g., `myapp-eu`, `myapp-na`).

To rename Headscale resources after changing `stack_name`, use `bin/bay gateway migrate-namespace --from <old> --to <new>`. Without flags, it migrates from the legacy `server` user to the current `stack_name`.

### Configuration

```yaml
# group_vars/all/main.yml

access_gateway: headscale

headscale_domain: hs.example.com     # public, for client enrollment

# Optional: OIDC self-service enrollment
headscale_oidc_issuer: https://auth.example.com
headscale_oidc_client_id: headscale
headscale_oidc_client_secret: "{{ vault_headscale_oidc_secret }}"
```

Without OIDC, create pre-auth keys via the `bin/bay gateway` CLI (`bin/bay gateway key <user>` or `bin/bay gateway enroll`). With OIDC, users open `https://hs.example.com` in a browser and authenticate through your identity provider.

### Traffic flow

```
 Client                         VPS
 ┌──────────────┐     ┌──────────────────────────────────────┐
 │ Tailscale app ├──┐  │                                      │
 │ (auto-managed)│  │  │ Headscale (coordination server)      │
 └──────────────┘  │  │   domain: hs.example.com              │
                   │  │   behind Traefik (public)              │
   auto-managed    │  │                                       │
   WireGuard       │  │ Tailscale daemon (tunnel termination) │
   tunnel          └─►│   tailscale0 (100.64.x.x)            │
                      │   │                                   │
                      │   ▼                                   │
                      │ nftables ──► CrowdSec bouncer         │
                      │   │                                   │
                      │   ▼                                   │
                      │ Traefik (host network)                │
                      │   IPAllowList: 100.64.0.0/10          │
                      │   │                                   │
                      │   ▼                                   │
                      │ service containers (bridge network)   │
                      └──────────────────────────────────────┘
```

1. Client installs Tailscale and joins the tailnet via Headscale (OIDC or pre-auth key)
2. Tailscale negotiates a WireGuard tunnel automatically -- no manual key management
3. Traffic arrives on `tailscale0` with a CGNAT source IP (100.64.x.x)
4. nftables allows the packet; CrowdSec bouncer checks its blocklist
5. Traefik matches the source against the tailnet CIDR (`100.64.0.0/10`) via IPAllowList
6. Authorized requests are routed to the target service container

There is no admin UI to reach — manage the tailnet from your workstation with the
[`bin/bay gateway` CLI](#gateway-cli) (no SSH to the server needed).

### Headscale quick start

End-to-end setup — from zero to a connected device.

**Prerequisites:** a deployed Bay server, a domain you control, and the Tailscale app on your client device.

#### 1. Configure the gateway

```yaml
# group_vars/all/main.yml
access_gateway: headscale
headscale_domain: hs.example.com
```

#### 2. Point DNS

Create an A record for `hs.example.com` pointing to your server IP. Traefik issues an SSL cert automatically on first request.

#### 3. Provision and deploy

```bash
bin/bay provision production
bin/bay deploy production
```

This deploys Headscale (public, for client enrollment) and the Tailscale daemon (VPS joins its own tailnet). There is no admin UI container.

#### 4. (Multi-region only) Generate a Headscale API key

Single-server setups can skip this step — enrollment below uses pre-auth keys, not
the API key.

```bash
bin/bay gateway apikey
```

This creates a Headscale API key (valid for 1 year by default). Use `--expiration` to change the lifetime (e.g. `--expiration 90d`).

Its sole purpose is **remote-region registration**: store it as `headscale_api_key`
in the vault so non-control regions can register with the control server's Headscale
API during deploy. This must persist — if it expires, deploys to non-control regions
fail at the `tailscale_register` step.

```bash
bin/bay vault edit production
```

Then inside the `secrets:` dict:

```yaml
headscale_api_key: "hskey-api-..."
```

#### 5. Create a user and pre-auth key

```bash
bin/bay gateway add-user alice
bin/bay gateway key alice
```

The pre-auth key is a one-time token that allows a device to join the tailnet without interactive login.

#### 6. Enroll a device

On your client device, install the Tailscale app and connect to your Headscale server:

```bash
tailscale up --login-server https://hs.example.com --authkey <pre-auth-key>
```

On macOS/iOS/Android, enter the login server URL in Tailscale settings before connecting.

#### 7. Verify

```bash
bin/bay gateway nodes
```

The enrolled device should appear in the node list with a green status indicator. VPN-protected services (`access: vpn`) are now accessible from the device.

**Notes:**

- **OIDC alternative** — instead of pre-auth keys, configure `headscale_oidc_*` variables to let users self-enroll through your identity provider. See [Configuration](#configuration-1) above.
- **Revoking access** — remove a node via `bin/bay gateway delete-node <name>`, or delete an entire user and their devices with `bin/bay gateway delete-user <name> --force`.
- **Adding more devices** — repeat steps 5–6 for each user/device. Each pre-auth key is single-use by default.
- **Managing users** — `bin/bay gateway users` lists all users with node counts. `bin/bay gateway user-info <name>` shows a user's devices and online status.
- **API key renewal** — run `bin/bay gateway apikey` again when the current key expires. Old keys stop working immediately after expiration. If you have a multi-region setup, update the vault key too (`bin/bay vault edit production`).

### Enrolling external devices

Not every device that needs tailnet access lives in the Ansible inventory — laptops, phones, or boxes managed elsewhere (another team, a vendor, a homelab) still need to reach `access: vpn` services sometimes. `bin/bay gateway enroll` is the lightweight path for this, and it deliberately does **less** than onboarding an inventory server: no `bin/bay provision`, no hardening, no `tailscale_register` role run — the device only gains a Tailscale client session.

```bash
bin/bay gateway enroll --user external-box
```

This creates a Headscale user (if it doesn't already exist), generates a single-use pre-auth key (`--expiry` and `--reusable` are available — see `bin/bay gateway enroll --help`), and prints the join command to run on the device:

```bash
tailscale up --login-server=https://hs.example.com --authkey=<key> --hostname=external-box
```

`--user` names the **owner** in Headscale, which is not the same thing as the device's tailnet name. Left to itself the device registers under whatever hostname it happens to have locally, so `ssh external-box` resolves nothing. Since `enroll` is one-user-per-device, it defaults the tailnet name to the user name and adds `--hostname` for you. Override with `--hostname <name>`, or opt out with `--no-hostname` to let the device keep its own.

> **Default-deny tailnets:** if the consumer defines `headscale_acl_policy`, enrollment alone leaves the device unreachable — `enroll` does not touch the policy. It detects this and prints the next steps. See [ACL policy](tailnet-ingress.md#locking-the-upstream-headscale-acl-headscale_acl_policy) for why an ungranted node doesn't even appear in other nodes' `tailscale status`.

Contrast with inventory servers: they join the tailnet automatically during `bin/bay provision` / `bin/bay deploy`, via the `tailscale_register` role (see [Headscale user model](#headscale-user-model) above), with full hardening included. `gateway enroll` skips all of that on purpose — it's for boxes Bay doesn't own or manage.

#### Enrolling a class of device: `--tag`

A per-device `hosts:` alias plus a per-device rule is the wrong shape when the device is one of many interchangeable boxes — an agent runner, a CI worker, a burner VM. ACL tags express that class once; `--tag` stamps the tag onto the **pre-auth key**, so the node joins already tagged:

```bash
bin/bay gateway enroll --user ci-runner --tag tag:agent --reusable --expiry 30d
```

`--tag` is repeatable (`--tag tag:agent --tag tag:ci`) and is also available on `bin/bay gateway key` for a user that already exists. Values must be lowercase `tag:name` (letters, digits, hyphens); anything else is rejected locally, before a user is created on the control host.

The point is *when* the tag applies. The alternative — enroll, then `headscale nodes tag -i <id> -t tag:agent` on the control host — leaves the node online-but-ungranted between the two commands, and reassigns its owner to the synthetic `tagged-devices` user after the fact. A key-stamped tag is in force from the node's first packet, so every rule matching that tag applies at join.

Three caveats, which the CLI also prints after a tagged enrollment:

- **A tag no rule names grants nothing.** If `tag:agent` doesn't appear in `tagOwners` *and* in accept rules, the node is exactly as dead on arrival as an untagged one. Tags remove the per-device edit; they don't remove the need for the class rules to exist first.
- **Ownership moves.** On Headscale **v0.29.x** a key-stamped node registers under the synthetic `tagged-devices` user, not the enrollment user — verified end-to-end against v0.29.2. The tag itself still shows in the Tags column of `bin/bay gateway nodes`; only the owner moves, so ACL rules keyed on the *user* will not match this node. Key off the tag. Re-confirm on a different Headscale major.
- **`acl audit` can't verify this.** It deliberately does not resolve `tag:` targets — they resolve against live Headscale state, not the policy file — so a tag-granted node reads as `unknown` there. Verify with a peer probe instead, run **from another node**, never from the node serving the port: a denied flow **times out** (`exit 124`), an allowed flow to a closed port is **refused** (`exit 1`).

#### Recommended join flags for externally-managed boxes

```bash
tailscale up --login-server=https://hs.example.com --authkey=<key> \
  --hostname=external-box --accept-dns=false
```

- `--hostname=<name>` — sets the node name shown in `bin/bay gateway nodes`. Without it, Headscale falls back to the device's own hostname, which may be uninformative or collide with another node. `enroll` now adds this for you (defaulting to the `--user` name), so you only need it by hand when joining a device without the CLI, or when overriding via `enroll --hostname`. To fix a node that already joined under the wrong name, use `bin/bay gateway rename-node`.
- `--accept-dns=false` — stops Tailscale/MagicDNS from rewriting the box's `/etc/resolv.conf`. An externally-managed box's DNS isn't Bay's to change just because it joined the tailnet — reach tailnet services by their raw `100.64.0.x` address instead of MagicDNS names.

#### Finding the assigned tailnet IP

`bin/bay gateway nodes` shows each node's tailnet IP. New nodes get the next free `100.64.0.x` address. (The key-display quirk is documented in `bin/bay gateway key --help`.)

#### ACL implications under default-deny

If the deployment defines `headscale_acl_policy` (see [Hardening the tailnet](#hardening-the-tailnet-acl--per-device-identity) above), Headscale is in default-deny mode. Tailscale/Headscale ACLs are **accept-only** — a rule can only grant traffic, never deny it — so adding a rule for the new node is purely additive and cannot regress any existing flow. In practice:

- **Outbound** from the new node to any destination already covered by a permissive `src: ["*"]` rule works immediately, no policy edit needed.
- **Inbound** to the new node is denied until the policy adds a `hosts:` alias and an `accept` rule naming it as `dst`:

  ```yaml
  # group_vars/<control-region>/main.yml
  headscale_acl_policy:
    hosts:
      external-box: 100.64.0.7/32   # match whatever --hostname registered
    acls:
      - { action: accept, src: [laptop], dst: ["external-box:22"] }
  ```

  Alias the *real* registered node name — whatever `--hostname` set — or the rule silently fails to match the device's traffic.

#### Applying the policy change

```bash
bin/bay deploy production --tags headscale --region <control-region>
```

This re-templates `policy.hujson` on the control host and restarts the `headscale` container. Verify in order: grep the rendered policy on the control host for the new alias/rule, confirm `headscale nodes list` shows the node online, then live-test the flow you just granted. Rollback is `git revert` of the policy commit followed by the same deploy command — **never** delete `headscale_acl_policy` to back out, since removing it reverts the tailnet to allow-all (a far larger blast radius than the edit, and it strips the only source restriction left wherever the ACL replaced host-level sshd pins).

#### Removing a device later

Teardown uses the same commands as any other node or user:

```bash
bin/bay gateway delete-node external-box
bin/bay gateway delete-user external-box --force   # also drops the user
```

### Multi-region Headscale

In multi-region deployments, a single Headscale instance is shared across all regions so that every server belongs to one tailnet. The **control region** runs the Headscale coordination server and the Tailscale daemon. All other regions run only the Tailscale daemon and register via the Headscale REST API.

Production typically dedicates a **low-surface control host** to the control region — a server that runs only the tailnet plumbing (Headscale + registry + monitoring + Traefik, and optionally the [tailnet HTTPS ingress](#tailnet-https-ingress-for-tailnet-only-services) + identity sidecar) and serves no public app traffic. That keeps the blast radius of the control plane (and of a fail-closed ingress misconfig) small. See [multi-region.md](multi-region.md#headscale-access-gateway-in-multi-region) for the dedicated-control-host model and how to relocate the control region.

#### Architecture

```
Control region (region == headscale_control_region)
  ├── Headscale coordination server (public, for client enrollment)
  └── Tailscale daemon (joins tailnet, terminates tunnel)

Remote region (any other region)
  └── Tailscale daemon (registers via API, joins same tailnet)
```

All servers share one tailnet — devices and services in any region can reach VPN-protected services in any other region through the mesh. There is no admin UI; the tailnet is managed via `bin/bay gateway` (which auto-targets the control host).

#### Variables

| Variable | Default | Where to set | Purpose |
|----------|---------|-------------|---------|
| `headscale_control_region` | — | `group_vars/all/access_gateway.yml` | Names the region that runs the Headscale coordination server |
| `secrets.headscale_api_key` | — | `group_vars/production/secrets.yml` (vault, inside `secrets:` dict) | API key for remote region registration |

The wizard sets `headscale_control_region` automatically when you choose multi-region + headscale (first region entered). Single-server setups leave it unset.

#### Deploy order

Multi-region + headscale requires a specific deploy order:

1. `bin/bay deploy <control-region>` — deploys Headscale server and registers the control host
2. `bin/bay gateway apikey` — generates an API key on the control server
3. `bin/bay vault edit production` — add `headscale_api_key` inside the `secrets:` dict
4. `bin/bay deploy <remote_region>` — remote host registers via API using the key

If you deploy a remote region before the control region, the deploy fails with a clear error message indicating that `headscale_api_key` is required.

See [multi-region.md](multi-region.md#headscale-access-gateway-in-multi-region) for example group_vars layout and full setup guide.

## Split-DNS for VPN Services

When a tailnet client visits a VPN-protected domain (e.g., `status.example.com`), normal DNS resolves to the server's public IP. The request traverses the internet and Traefik sees the client's real public IP — not a tailnet IP — so the IPAllowList rejects it with 403.

Bay solves this automatically with Headscale's MagicDNS split-DNS. Tailnet clients resolve VPN service domains to the server's tailnet IP instead of the public IP, so requests travel through the WireGuard tunnel and arrive with a `100.64.x.x` source address.

### How it works

Three pieces cooperate:

1. **Extra records** — the headscale role templates `extra-records.json` with an A record for every `access: vpn` service domain, mapping it to the server's tailnet IP. Headscale watches this file and pushes the records to all clients via the netmap.

2. **Split DNS routes** — the headscale config adds each VPN domain to `nameservers.split`, routing those DNS queries to MagicDNS (`100.100.100.100`) on the client. Without this, the client's OS resolver would query public DNS and never see the extra records.

3. **Traefik host networking** — Traefik listens on `0.0.0.0:443`, which includes the `tailscale0` interface. Requests arriving via the tailnet IP are served with the same Let's Encrypt certificate and routed to the correct service.

### Traffic flow

```
Tailnet client                     VPS
┌──────────────┐         ┌──────────────────────────────────┐
│ Browser:     │         │                                  │
│ status.x.com │         │ Headscale                        │
│      │       │         │   extra_records: status.x.com    │
│      ▼       │         │                  → 100.64.0.1    │
│ MagicDNS     │         │                                  │
│ resolves to  │         │ Tailscale daemon                 │
│ 100.64.0.1   │         │   tailscale0 (100.64.0.1)       │
│      │       │         │      │                           │
│      ▼       │         │      ▼                           │
│ TLS via      ├────────►│ Traefik (host network, :443)    │
│ tailnet      │ tunnel  │   IPAllowList: 100.64.0.0/10    │
└──────────────┘         │   source: 100.64.0.x → ✓ pass   │
                         │      │                           │
                         │      ▼                           │
                         │ service container → 200          │
                         └──────────────────────────────────┘
```

Non-tailnet clients resolve the domain to the server's public IP via normal DNS. Traefik sees their real public IP and rejects with 403.

### Multi-region

The templates iterate all hosts in the inventory via `hostvars`, so a single Headscale instance serves split-DNS records for every region. Each region's VPN services map to that region's tailnet IP:

```
status.eu.example.com → 100.64.0.1  (EU server)
status.na.example.com → 100.64.0.2  (NA server)
```

Set `headscale_server_tailnet_ip` per region in `group_vars/<region>/main.yml`. The default is `100.64.0.1` (first node in sequential allocation).

```yaml
# group_vars/na/main.yml
headscale_server_tailnet_ip: "100.64.0.2"
```

### No configuration needed

Split-DNS is fully automatic for Headscale deployments. Any service with `access: vpn` (or with `vpn_routes`) in `services.yml` gets a split-DNS override generated at deploy time. Adding or removing VPN services triggers a file watcher reload — no Headscale restart required.

### Manual DNS records (`headscale_extra_dns_records`)

The two sources above cover everything Bay can derive from inventory: `access: vpn` services and `tailnet_proxies`. Sometimes neither applies — an externally-managed tailnet node (a box outside the Ansible inventory, [enrolled](#enrolling-external-devices) rather than provisioned) serves a domain that publicly resolves elsewhere, and tailnet devices should resolve it to that node's tailnet IP instead. `headscale_extra_dns_records` adds a manual mapping for exactly this case:

```yaml
# group_vars/<control-region>/main.yml
headscale_extra_dns_records:
  - name: example.com      # domain
    value: 100.64.0.7      # tailnet IP
    # type: A              # optional, defaults to A
```

Entries here are merged into `extra-records.json` and the `dns.nameservers.split` block alongside the auto-generated records, using the same domain dedup — if a domain is already claimed by a VPN service or a `tailnet_proxies` entry, the manual record for that domain is skipped. A deployment whose *only* split-DNS source is `headscale_extra_dns_records` still gets the full split-DNS block and `extra_records_path` — the gate isn't tied to VPN services or proxies specifically, just to there being at least one record.

Redeploy the `headscale` tag after changing this var, same as any other split-DNS source (see [Partial deploys and stale records](#partial-deploys-and-stale-records) below).

**Interaction with `--accept-dns=false`:** nodes enrolled with the [recommended external-device join flags](#recommended-join-flags-for-externally-managed-boxes) ignore MagicDNS entirely (including these manual overrides) — deliberate, since an externally-managed box's DNS isn't Bay's to change. `headscale_extra_dns_records` only affects how *other* tailnet clients (that do accept MagicDNS) resolve that node's domain, not the node's own resolution.

### Hardcoded domains in multi-region

When a service uses `{{ domain_base }}` for its domain, each region generates a unique DNS name (e.g., `status.eu.example.com`, `status.na.example.com`). The split-DNS templates handle this correctly.

If a service uses a hardcoded domain (e.g., `blog.example.com`), the templates filter by `svc.regions` so the record is only generated for the host that actually deploys the service. This avoids duplicate YAML keys in the Headscale config, which would crash Headscale's strict Go YAML parser.

### Partial deploys and stale records

The headscale role runs only on the control region host. If you add or change a VPN service and deploy only that service's region, the split-DNS records are **not updated**. After changing VPN services, always also deploy the headscale tag:

```bash
bin/bay deploy production --tags headscale
```

### DNS override behavior

By default, Bay sets `override_local_dns: false` in the Headscale config. This means Tailscale clients use their own local DNS for public domains and only route tailnet and VPN service domains through MagicDNS. This is the correct behavior for Bay — VPS nodes and developer machines have their own working DNS, and overriding it creates a single point of failure.

When `override_local_dns` is `true` (Headscale's upstream default), the Tailscale client sets `~.` (catch-all) on the `tailscale0` interface in systemd-resolved, routing **all** DNS through MagicDNS. If MagicDNS becomes unreachable (e.g., tunnel disruption, Headscale restart), all DNS on the client dies — including domains that have nothing to do with the tailnet.

To opt into global DNS override (e.g., to force all clients through a Pi-hole):

```yaml
# group_vars/all/access_gateway.yml
headscale_dns_override_local: true
headscale_dns_global_nameservers:
  - 10.64.0.1    # your Pi-hole / AdGuard on the tailnet
```

Both variables must be set together — Headscale requires `nameservers.global` when `override_local_dns` is `true`.

If a client's DNS breaks due to an existing `override_local_dns: true` config, the quickest client-side fix is:

```bash
# Disable Tailscale DNS override on this machine
sudo tailscale set --accept-dns=false

# Re-enable after the server-side config is fixed
sudo tailscale set --accept-dns
```

### Certificate handling

Let's Encrypt certs are issued via HTTP-01 challenge against the domain's public IP. Since the cert is stored on disk and served by SNI, it works regardless of which interface the request arrives on. Tailnet clients get the same valid certificate as public clients.

HTTP-01 requires the service to have a public IP and domain. For **tailnet-only services** — ones with no public socket, that need a wildcard cert, or that live on another tailnet node — Let's Encrypt can't validate over the public internet. Those use a DNS-01 wildcard cert terminated on the control host instead; see the next section.

## Tailnet HTTPS Ingress (for tailnet-only services)

Split-DNS (above) gets a *public-IP-backed* VPN service to resolve to a tailnet
address so it travels through the tunnel. But some services have **no public socket
at all** — a note-taking app on a laptop, a homelab box, an admin tool you never want on
the public internet. On real Tailscale, `tailscale serve --https` would mint a
`*.ts.net` cert for these; self-hosted Headscale has no cert authority, so they fall
back to plain HTTP (an *insecure context* — no service workers, PWA install, or Web
Push).

The **tailnet HTTPS ingress** reproduces Tailscale's model with infrastructure you
control: the control host issues **one wildcard cert via the DNS-01 challenge**
(Cloudflare token) and terminates TLS for tailnet services — including services that
live on **other tailnet nodes**. The moving parts:

- **`tailnet_proxies`** — a top-level map (sibling of `services:`), set only on the
  control host. Each entry renders a Traefik **file-provider** router pointing at any
  tailnet URL (`upstream: http://100.64.0.x:port`). This reaches backends on other
  nodes — something Docker-label routing (local containers only) cannot express.
- **DNS-01 wildcard on the control host** — one `*.ts.example.com` cert
  (`tailnet_ingress_cert_domain`) covers every proxy route; hostnames resolve only
  inside the tailnet (MagicDNS), never in public DNS.
- **Fail-closed `websecure_tailnet` entrypoint** — with `traefik_split_entrypoints`,
  public entrypoints bind the public IP and a dedicated `websecure_tailnet` binds the
  tailnet IP. Tailnet routes bind `websecure_tailnet` only, so a misconfig makes the
  service unreachable, never public.
- **`pass_host_header`** — for backends still fronted by `tailscale serve` (which only
  answers for the node's own MagicDNS name), set `pass_host_header: false` and point
  `upstream` at the MagicDNS name.

Run it on the dedicated control host (small blast radius for the fail-closed split).
Full setup, variable reference, and the `tailscale serve` 404 gotcha are in
[tailnet-ingress.md](tailnet-ingress.md).

### Hardening the tailnet: ACL + per-device identity

Two related controls live on the control host and are documented in full in
[tailnet-ingress.md](tailnet-ingress.md):

- **`headscale_acl_policy`** — by default Headscale is **allow-all** (every node can
  reach every other node's every port). Defining this var renders a file-mode HuJSON
  policy and flips Headscale to **default-deny**. ⚠️ **Blast radius:** the moment it is
  defined, anything not explicitly `accept`-ed is blocked — you must enumerate *every*
  flow (cross-region links, rig/monitoring, operator SSH, per-peer SSH) or you cut
  production traffic. Lock the proxied upstream so only the ingress host can reach it.
- **`X-Tailnet-Device` identity** — `tailnet_identity_enabled` runs a Traefik
  ForwardAuth sidecar on the ingress host that resolves the client tailnet IP →
  Headscale device name and injects `X-Tailnet-Device` onto opted-in routes
  (`identity_inject: true`). The app may trust that header **only** in combination with
  the ACL above (upstream reachable from the ingress host only) — the two are a pair.

## Gateway CLI

All Headscale management is available through `bin/bay gateway` subcommands — no SSH to the server needed. The CLI help is the reference:

```bash
bin/bay gateway --help              # all subcommands
bin/bay gateway enroll --help       # per-command details, quirks, and examples
```

Every subcommand accepts `--env` (default: `production`) and `--region` to target a specific region in multi-region setups; `bin/bay gateway` auto-targets the control host.

For enrolling boxes that live outside the Ansible inventory (contractor laptops, vendor-managed servers, homelab devices), see [Enrolling external devices](#enrolling-external-devices) above. On a default-deny tailnet, `bin/bay gateway acl audit` is the post-enrollment check that catches nodes the policy never names (see `--help` for the status taxonomy).

## Choosing a gateway

| | WireGuard | Headscale |
|---|---|---|
| **Client setup** | Manual config file per device | Install Tailscale, join tailnet |
| **Peer management** | Edit `vpn_allowed_ips`, redeploy | OIDC self-service or pre-auth keys |
| **Key rotation** | Manual | Automatic |
| **IP assignment** | Manual (you pick IPs) | Automatic (CGNAT range) |
| **Management** | Edit YAML + redeploy | `bin/bay gateway` CLI (no SSH) + optional OIDC |
| **Dependencies** | WireGuard kernel module | Headscale container + Tailscale daemon |
| **Best for** | Small teams, static peers | Growing teams, self-service onboarding |

Both gateways feed into the same downstream pipeline:

```
gateway tunnel → nftables → CrowdSec → Traefik IPAllowList → service
```

Services do not change between gateways. A service with `access: vpn` works identically regardless of which gateway is active -- only the tunnel establishment and IP allocation differ.

## Tailnet Routing for Cross-Region Links

When services use `links:` for cross-region communication, containers reach remote services through the host's tailnet interface. This section explains the network path and security model.

> **`links:` vs `tailnet_proxies` — which do I reach for?** Both ride the tailnet, but
> they solve different problems. **`links:`** is *container → container*: one service's
> backend dials another service/accessory directly over the tailnet (raw TCP, no TLS,
> no Traefik) — used for app-to-DB / app-to-cache wiring across regions. **`tailnet_proxies`**
> ([tailnet-ingress.md](tailnet-ingress.md)) is *browser → service ingress*: it puts a
> trusted-HTTPS Traefik front door on the control host for a tailnet-only web UI, including
> one that lives on another node. Rule of thumb: if a human's browser needs a padlock,
> use `tailnet_proxies`; if one container needs to open a socket to another, use `links:`.

### How containers reach the tailnet

Docker containers use the host's network stack as their default gateway. When a container sends traffic to a tailnet IP (100.64.0.0/10 range), the packet follows this path:

1. Container → Docker bridge network (default gateway is the host)
2. Host network stack routes the packet to the `tailscale0` interface
3. Tailscale/WireGuard encrypts and tunnels the packet to the remote host
4. Remote host receives the packet on its `tailscale0` interface
5. nftables allows the traffic (source IP in 100.64.0.0/10, destination port allowed)
6. Docker forwards to the container's exposed port

No special Docker network configuration is needed. Standard bridge networking works because containers inherit the host's routing table via the default gateway.

### Port binding and nftables

When a service or accessory becomes a link target:

1. **Port binding**: Changed from `127.0.0.1:<port>:<port>` to `0.0.0.0:<port>:<port>` in the generated compose file. This allows traffic from any interface (including `tailscale0`) to reach the container. The original `services.yml` is not modified.

2. **Firewall rule**: An nftables rule is added to the `inet filter input` chain:
   ```
   ip saddr 100.64.0.0/10 tcp dport <port> accept
   ```
   This ensures only tailnet traffic can reach the port. Public internet traffic is still dropped by the default `drop` policy.

3. **Automatic cleanup**: When a link is removed and the stack is re-deployed, the port binding reverts to `127.0.0.1` and the nftables rule is removed. This happens because both are template-driven from the current `link_targets` fact.

### Requirements

- Headscale access gateway must be configured (`access_gateway: headscale`)
- Both regions must have Tailscale registered and connected to Headscale
- The tailnet mesh must be established (deploy control region first, then remote regions)

## Using Tailscale.com Instead of Self-Hosted Headscale

If you already have a Tailscale account and prefer to use tailscale.com as your coordination server, you can integrate manually using the WireGuard gateway mode. This gives you VPN access control without Bay managing the control plane.

### Why Bay recommends self-hosted Headscale

Headscale's `extra_records` feature enables automatic split-DNS — Bay generates DNS records from `services.yml` so VPN service domains resolve to tailnet IPs without any manual configuration. Tailscale.com does not offer this capability. The Tailscale client protocol has supported custom DNS records since 2021, but tailscale.com has never exposed it server-side ([tailscale/tailscale#1543](https://github.com/tailscale/tailscale/issues/1543), 869+ upvotes, open since 2021).

With self-hosted Headscale, you also get the `bay gateway` CLI for user/node/key management and the tailnet HTTPS ingress (DNS-01 wildcard certs for tailnet-only services) — none of which are available with tailscale.com integration.

### Setup

1. **Install Tailscale on your server** and join your tailnet:

   ```bash
   curl -fsSL https://tailscale.com/install.sh | sh
   tailscale up --authkey=tskey-auth-...
   ```

2. **Configure Bay** to use the WireGuard gateway with your tailnet CIDR:

   ```yaml
   # group_vars/all/main.yml
   access_gateway: wireguard

   vpn_allowed_ips:
     - 100.64.0.0/10    # Tailscale CGNAT range
   ```

3. **Deploy normally:**

   ```bash
   bin/bay deploy production
   ```

   Services with `access: vpn` are now protected by Traefik's IPAllowList — only requests from tailnet IPs are allowed.

4. **Manage DNS yourself.** Since tailscale.com has no `extra_records`, you need to handle split-DNS manually so VPN service domains resolve to tailnet IPs for tailnet clients. Options:

   - **Split DNS with your own resolver** — run Pi-hole, AdGuard Home, or CoreDNS on the tailnet. Add A records mapping VPN service domains to tailnet IPs. Configure a restricted nameserver in the Tailscale admin console pointing those domains at your resolver.
   - **Public DNS with tailnet IPs** — add A records for VPN service domains pointing to tailnet IPs (e.g., `100.64.0.1`) in your public DNS. Only reachable from within the tailnet, but hostnames are publicly discoverable.
   - **`/etc/hosts`** — add entries on each client device. Simple but doesn't scale.

### What works

- VPN access control via Traefik IPAllowList (same `access: vpn` semantics)
- All service routing, SSL certificates, CrowdSec, nftables
- Multi-region (install Tailscale on each server, same tailnet)

### What you lose vs. self-hosted Headscale

- Automatic split-DNS (must manage DNS manually)
- `bay gateway` CLI commands (user/node/key management)
- Tailnet HTTPS ingress + per-device identity (DNS-01 wildcard, `tailnet_proxies`, `X-Tailnet-Device`)
- OIDC self-service enrollment
- Embedded DERP relay server

### Further reading

For the full feasibility analysis of external Tailscale control server support, including CoreDNS sidecar design, credential lifecycle, and migration paths, see:
- [external-tailscale-research.md](external-tailscale-research.md)
- [external-tailscale-implementation-plan.md](external-tailscale-implementation-plan.md)

## The adapter contract

This section documents the *contract*, not the two backends' config surface. A
new backend is written against what is below; nothing else in the framework
should need editing.

The boundary is deliberately **two artifacts joined by one config key**, not one
unified interface. The Ansible half is evaluated at every deploy render,
including `--tags deploy_stack` runs that never touch the CLI. The CLI half runs
at operator time against a live control host. They share nothing but the
backend's name, and forcing shared code between them would manufacture coupling
rather than remove it.

### 1. Ansible: five contract vars

Declared in `roles/access_gateway/defaults/main.yml`, with the backend dispatch
inline in each var. That one file is the only place in the framework permitted
to branch on which backend is active.

| Var | Meaning | `headscale` | `wireguard` | `none` |
|---|---|---|---|---|
| `gateway_enabled` | any private overlay exists | `true` | `true` | `false` |
| `gateway_bind_ip` | this host's overlay IP — the bind target for `expose: gateway`, the Traefik overlay entrypoint, zot's self-pin, the CrowdSec self-ban exemption | the configured overlay IP | the configured overlay IP | `''` |
| `gateway_cidrs` | overlay CIDR(s) for allowlists | tailnet CIDR | `[]` (consumer supplies `vpn_allowed_ips` directly) | `[]` |
| `gateway_identity_supported` | per-request identity injection available | `tailnet_identity_enabled` | `false` | `false` |
| `gateway_requires_node_registration` | hosts must register with a control server after their containers start | `true` | `false` | `false` |

Consuming roles — `traefik`, `zot`, `crowdsec_allowlist`, `container_lifecycle`,
`git_deploy`, `deploy_stack` and `deploy.yml`'s multi-region link resolution —
read only these. `tests/test_gateway_ratchet.py` enforces that mechanically
against a shrinking allowlist of backend-owned files.

`vpn_allowed_ips` keeps its name. It was already backend-neutral and already the
consumed contract for Traefik's IPAllowList; renaming a working neutral var is
churn, not architecture.

**Empty string means "no overlay".** A backend that provides no bind IP must
return `''`, and every consumer must treat `''` as *nothing to bind, nothing to
exempt* — never as an address.

### Why defaults, and the one place they do not reach

The contract vars are lazy-evaluated **defaults**, not facts set by the role's
tasks. A `set_fact` fact only exists in another host's `hostvars` after the role
has actually executed on that host in that play, which is fragile under
`--limit`, `--tags` and rig-skip runs. That exact bug already shipped once: the
`vpn_allowed_ips` flip-flop documented in
`roles/access_gateway/tasks/main.yml`, where a fact set on rig-included runs but
skipped on `--tags deploy_stack` runs made Traefik's sourcerange oscillate
between deploys.

There is one real limitation, **verified rather than assumed**: Ansible role
defaults — and play `vars:` — are absent from `hostvars[<other_host>]`
entirely. Only inventory vars, `group_vars`, `host_vars`, gathered facts and
`set_fact` facts land there.

Two call sites need *another* host's overlay IP: `roles/crowdsec_allowlist`
(which walks every inventory host to build the self-ban exemption list) and
`deploy.yml`'s multi-region `links:` resolution (which needs the link target's
IP). Those two go through a resolver instead:

```jinja
{{ hostvars[h] | bay_gateway_bind_ip }}
```

`bay_gateway_bind_ip` reads `gateway_bind_ip` if the consumer set it in
`group_vars`/`host_vars`, falls back to the incumbent
per-host overlay-IP var, and otherwise returns `''`. It is duplicated in
`roles/crowdsec_allowlist/library/crowdsec_allowlist_sync.py` because an Ansible
library module runs in its own interpreter on the target and cannot import a
filter plugin; `tests/test_gateway_contract.py` asserts the two copies agree.

### 2. CLI: the `GatewayBackend` Protocol

`src/bay_cli/commands/gateway_backend.py` defines a `typing.Protocol` minted
from `LocalHeadscaleBackend`'s existing method list — that class was already the
de facto interface, so the Protocol only writes down what `bin/bay gateway` had
always assumed.

- A backend with **equivalent semantics** (a remote Headscale, or a
  tailscale.com control plane) is a new class and no change to `gateway.py`'s
  command logic.
- A backend with **lesser capabilities** is not forced to fake a node database.
  `wireguard` and `none` get `NullGatewayBackend`, whose every operation raises
  one uniform, actionable error naming the active backend and what to do
  instead. The bar is: every command either works or explains why it cannot —
  never a traceback, never a silently-wrong assumption that Headscale is there.

ACL and tag commands stay Headscale-only **by declaration, not by abstraction**.
Generalising "ACL audit" over backends that have no ACL concept would be
speculative generality with zero second implementations.

### 3. `expose: gateway`, and `tailnet` as a permanent synonym

`ports.expose` accepts `gateway`. `tailnet` remains accepted **indefinitely** as
a documented synonym — both resolve through `gateway_bind_ip`. `services.yml` is
consumer-facing API, so there is no breaking rename; existing files never need
editing. New docs and `example/` use `gateway`.

Validation is now a live guard rather than a dead one. It used to test the
backend's bind-IP var for `is not defined`, which never fired: that var carries
a play-wide default, so it is *always* defined. A role left unconverted did not
error under `access_gateway: none` — it quietly bound a phantom `100.64.0.1`
belonging to nobody. The guard now tests `gateway_bind_ip`, which is genuinely
empty, and `access: vpn` under `access_gateway: none` fails outright.

### Writing a new backend

1. Add a branch to each contract var in
   `roles/access_gateway/defaults/main.yml`. Do not add branches anywhere else.
2. Add any tunnel-establishment tasks to `roles/access_gateway/tasks/main.yml`,
   or a role it includes.
3. If it manages nodes, implement `GatewayBackend` and add a case to
   `_make_backend`. If it does not, do nothing — it inherits
   `NullGatewayBackend`.
4. Add the backend to the table at the top of this document.

If step 1 makes you want to touch a consuming role, the contract is missing a
var. Add the var — do not add the branch.

## Backward compatibility

The `access_gateway` variable defaults to `wireguard`. Existing deployments that do not set this variable continue to work without changes.

Switching from one gateway to the other requires reprovisioning (`bin/bay provision production`) since the tunnel interface, firewall rules, and IPAllowList configuration all change. The switch is not disruptive to service definitions -- only the infrastructure layer is replaced.
