# Onboarding Guide

## Quick start

Your first project takes three steps.

```bash
# 1. Install Bay once on this machine
git clone https://github.com/AltanS/bay ~/.local/share/bay/framework
~/.local/share/bay/framework/bootstrap.sh

# 2. Make a fleet (it gets its first commit), then check bay.fleet.toml
bay fleet init prod

# 3. In your app repo, write bay.toml
cd my-app
bay init --fleet prod
```

Step 1 is covered in full in **[install.md](install.md)**. It also shows how to update Bay
and how Bay picks a fleet.

A *fleet* is the repo that holds your boxes, their config and their secrets. Bay keeps it
at `~/.config/bay/fleets/<name>`. To clone a fleet you already have, use
`bay fleet init prod --from <git url>`. To list the fleets on this machine, use
`bay fleet ls`.

## Your first project

Run `bay init` inside the app repo. It drafts a `bay.toml` there and registers the app in
the fleet. By default it names the project after the repo directory. Use `--name`, `--box`
and `--domain` to set them. The file format is in **[bay-toml.md](bay-toml.md)**.

Then:

```bash
bay plan      # compare WANTED (bay.toml), PINNED (the lock) and RUNNING (the box)
bay up        # pin this commit in the fleet and deploy it
```

`bay plan` prints the steps, the risk of each, and a verdict. `bay up` deploys the whole box environment, not one project. It refuses a plan that
needs approval until you run `bay approve`. See **[plan.md](plan.md)**.

If you already have a fleet in the older YAML layout, `bay import --fleet <path> --out
<new path>` writes the new files without changing the old ones. `bay compile` turns
`bay.fleet.toml` and every `bay.toml` into `services.yml`. Both commands list their flags
with `--help`.

Fleet config that Bay does not write for you (the boxes, domains, secrets and the access
gateway) lives in the fleet's `group_vars/` and `hosts/` files. The sections below cover
them.

## Pre-flight Doctor Check

Before your first deploy, run `bay doctor` to validate your environment (DNS, vault password, SSH connectivity, gateway config — `bay doctor --help` lists the checks). Then run `bay validate` to check your config files (YAML syntax, the services schema, inventory, vault keys) — this also runs automatically before every deploy, so running it here just lets you fix issues before the provision step. Fix any reported issues before running `bay provision` and `bay deploy`.

For the very first provision, the target server usually only has a `root`
account. `ansible_user` in `group_vars/all/main.yml` defaults to
`bay-admin`, an account provisioning itself creates. You do not need to
override the SSH user. `bay provision` first tests the connection as
`ansible_user`. When the host is unreachable as that user, the playbook
falls back to `root` for that run:

```bash
bay provision production
```

Later provisions and deploys use `bay-admin` as normal.

## Gateway Paths

### Headscale

Headscale is a self-hosted Tailscale coordination server. Devices join your private tailnet and get automatic, encrypted tunnels to your VPN-protected services.

**When to choose**: You want a modern VPN with easy device enrollment, mobile client support, and optional OIDC self-service.

**Setup walkthrough**:

1. Set `headscale_domain` (e.g., `hs.example.com`) in `group_vars/all/access_gateway.yml`
2. Create a DNS A record: `hs.example.com → your-server-ip`
3. Run `bay provision production && bay deploy production`
4. After first deploy, a panel shows enrollment steps:
   - Enroll a device: `bay gateway enroll` (creates the user, mints a key, prints the join command)
   - On your device: `tailscale up --login-server=https://hs.example.com --authkey=KEY`
5. Manage nodes/users/keys via the CLI: `bay gateway --help`
   (there is no admin web UI; OIDC self-service enrollment is optional). See
   `docs/access-gateways.md`.

**Files involved**:
- `group_vars/all/access_gateway.yml` — `access_gateway: headscale`, `headscale_domain`
- `group_vars/all/vpn_access.yml` — tailnet CIDR `100.64.0.0/10`
- `group_vars/all/security.yml` — UDP ports 41641 (DERP relay) and 3478 (STUN)

### WireGuard

Manual VPN with static peer configuration. You manage peer keys and IPs yourself.

**When to choose**: You already have a WireGuard setup, or you prefer full manual control.

**Setup walkthrough**:

1. Collect the peer IPs (your devices' WireGuard addresses)
2. Add them to `group_vars/all/vpn_access.yml`
3. Configure your devices with the server's WireGuard public key

**Files involved**:
- `group_vars/all/access_gateway.yml` — `access_gateway: wireguard`
- `group_vars/all/vpn_access.yml` — your peer IPs in `vpn_allowed_ips`

### None (No Gateway)

All services are publicly accessible. No VPN.

**When to choose**: All your services are public, or you'll add VPN later. It is the shortest path to a working first deploy. Set `access_gateway: none` in `group_vars/all/access_gateway.yml` to choose it.

**The default is not `none`.** A fleet that does not set `access_gateway` gets `wireguard`, the default in `roles/access_gateway/defaults/main.yml`.

**Note**: With `none`, the deploy stops with an error if a service sets `access: vpn`. Use `access: public` for all services, or add a gateway later by setting `access_gateway` in `group_vars/all/access_gateway.yml`.

## Service Catalog

Bay ships a curated catalog of self-hosted services:

| Service | Image | Default Access | Dependencies |
|---------|-------|---------------|-------------|
| Gatus | `twinproduction/gatus:latest` | public | — |
| Vaultwarden | `vaultwarden/server:latest` | vpn | — |
| n8n | `n8nio/n8n:latest` | vpn | PostgreSQL |
| Plausible | `ghcr.io/plausible/community-edition:latest` | public | PostgreSQL |
| Umami | `ghcr.io/umami-software/umami:postgresql-latest` | public | PostgreSQL |

| Accessory | Image | Description |
|-----------|-------|-------------|
| PostgreSQL | `postgres:17` | Relational database with pg_dump backup |
| Redis | `redis:7-alpine` | In-memory cache |
| MariaDB | `mariadb:11` | MySQL-compatible database with mysqldump backup |

List the catalog with `bay service catalog`. To run one of these, write a `bay.toml` for it (an app in the fleet lives in `projects/<name>/bay.toml`) and put a shared database in `bay.fleet.toml` as a resource; see **[bay-toml.md](bay-toml.md)**. Do not edit `group_vars/all/services.yml`: `bay compile` writes it, and it refuses a file that was edited by hand. The removed service write verbs wrote that file directly; write the `bay.toml` instead. **[services.md](services.md)** describes the compiled form.

## Fleet Files

The fleet keeps its config in these files. Edit them by hand, except `services.yml`.

| File | Purpose |
|------|---------|
| `hosts/production` | Server inventory (IP addresses) |
| `group_vars/all/main.yml` | Project identity (`stack_name`, users, Docker config) |
| `bay.fleet.toml` | Boxes, domains, shared resources (`bay fleet init` writes a first one) |
| `group_vars/all/services.yml` | Generated by `bay compile` from `bay.fleet.toml` and the `bay.toml` files. Never edit it |
| `group_vars/all/users.yml` | SSH keys for server access |
| `group_vars/all/security.yml` | Firewall rules, CrowdSec, SSH hardening |
| `group_vars/all/vpn_access.yml` | VPN IP whitelist |
| `group_vars/all/access_gateway.yml` | Gateway type and config |
| `group_vars/production/main.yml` | Build strategy, registry credentials |
| `group_vars/production/domains.yml` | Domain and Let's Encrypt email |
| `group_vars/production/secrets.yml` | Vault-encrypted credentials |

For a multi-region setup, add `group_vars/<region>/main.yml` for each region (with a
`domain_base` override). The inventory uses `[production:children]` grouping.

The `example/` directory in the framework checkout holds a copy of each file above, except
`bay.fleet.toml`. Copy what you need into the fleet, then edit it. Do not copy
`example/group_vars/all/services.yml`: it shows the compiled form, and `bay compile` refuses a
`services.yml` that it did not write. These are the fields to change:

### `hosts/production`

```ini
[production]
your-server-ip
```

### `group_vars/all/main.yml`

```yaml
stack_name: my-project            # CHANGE: your project name
admin_user: bay-admin
app_user: bay
docker_users:
  - "{{ admin_user }}"
  - "{{ app_user }}"
```

`app_user` is the app account. The deploy runs as this account and it owns the stack directory.
No role sets a default, so the fleet must set it. The example fleet uses `bay`.

### `group_vars/all/services.yml`

Do not write this file. `bay compile` writes it from `bay.fleet.toml` and the `bay.toml` of each
app. An app gets its `bay.toml` from `bay init` (in its repo) or as `projects/<name>/bay.toml` in
the fleet. A shared database is a resource in `bay.fleet.toml`. The keys of `bay.toml` are in
**[bay-toml.md](bay-toml.md)**. Scenario 5 of [layout-scenarios.md](layout-scenarios.md#5-several-boxes-shared-resources-mixed-apps)
shows a shared resource in `bay.fleet.toml`.

### `group_vars/all/access_gateway.yml`

```yaml
# Choose one: headscale, wireguard, or none
access_gateway: headscale

# Required for headscale:
headscale_domain: hs.example.com  # CHANGE: your headscale domain
```

Ansible reads every file in `group_vars/all/`, so the deploy would also find these keys in
`main.yml`. Keep them in `access_gateway.yml` anyway. `bay gateway`, `bay region` and `bay doctor`
read the gateway settings from this file by name.

### `group_vars/production/domains.yml`

```yaml
domain_base: example.com          # CHANGE: your domain
letsencrypt_email: admin@example.com
```

### `group_vars/production/secrets.yml`

```yaml
secrets:
  POSTGRES_PASSWORD: "changeme"   # CHANGE: generate with bay secret
```

Encrypt with: `bay vault encrypt production`
