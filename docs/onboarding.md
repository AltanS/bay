# Onboarding Guide

## Interactive Setup Wizard

The `bin/bay setup` command includes an interactive wizard that walks you through project configuration and generates a tailored scaffold.

### Running the wizard

After bootstrapping a project with `.bay/bootstrap.sh`, run the wizard:

```bash
bin/bay setup
```

The wizard uses arrow-key selection for choices and checkbox pickers for services. It asks:

1. **Project name** — used as `stack_name` and the stack directory under `/opt/`. Must be lowercase alphanumeric with hyphens (DNS-safe, max 63 chars).

2. **Single server or multi-region?** — determines inventory structure and domain strategy.

3. **Server address** — for single-server: one IP or hostname. For multi-region: a name and IP for each region (minimum 2).

4. **Base domain** — e.g., `example.com`. Used for service subdomains (`status.example.com`). In multi-region mode, each region gets a prefix (`eu.example.com`, `na.example.com`).

5. **Let's Encrypt email** — for automatic SSL certificates. Defaults to `admin@<domain>`.

6. **SSH keys** — fetch from GitHub (by username) or paste a public key. You can add multiple keys. Skip if you prefer to add them later.

7. **Access gateway** — how VPN-protected services are secured:
   - **Headscale** (recommended) — self-hosted Tailscale, automatic device enrollment
   - **WireGuard** — manual peer configuration with static IPs
   - **None** — no VPN, all services are publicly accessible

8. **Services** — pick from the catalog using checkbox selection (space to toggle):
   - **Services**: Gatus, Vaultwarden, n8n, Plausible, Umami
   - **Accessories**: PostgreSQL, Redis, MariaDB
   - Dependencies are auto-selected (e.g., Plausible → PostgreSQL)

9. **Vault password** — choose to generate, enter manually, or skip.

A summary panel shows all collected values before generating files. Existing files are never overwritten (unless using `--force`).

### CLI Flags

All wizard steps can be pre-filled or fully automated with CLI flags; partial flags pre-fill the wizard, a complete set skips it entirely (for agent and script automation). `bin/bay setup --help` is the flag reference and carries copy-pasteable examples.

### Non-TTY detection

If `bin/bay setup` detects a non-interactive terminal (e.g., piped input, CI environment), it automatically falls back to `--no-interactive` mode with a warning. No explicit flag needed.

### Edit / Resume Mode

Running `bin/bay setup` on an existing project enters **edit mode**:

- Current values are loaded from the existing config files
- Each wizard step shows the current value as the default
- Press Enter to keep a value, or type a new one to change it
- Modified files are backed up with `.bak` suffix before overwriting
- Only changed sections are re-rendered

This is useful for changing your gateway type, adding services, or updating your domain without regenerating everything from scratch.

```bash
# Edit existing project — current values shown as defaults
bin/bay setup

# Force overwrite without edit mode detection
bin/bay setup --force
```

## Pre-flight Doctor Check

Before your first deploy, run `bin/bay doctor` to validate your environment (DNS, vault password, SSH connectivity, gateway config — `bin/bay doctor --help` lists the checks). Then run `bin/bay validate` to check your config files (YAML syntax, the services schema, inventory, vault keys) — this also runs automatically before every deploy, so running it here just lets you fix issues before the provision step. Fix any reported issues before running `bin/bay provision` and `bin/bay deploy`.

For the very first provision, the target server usually only has a `root`
account — `ansible_user` in `group_vars/all/main.yml` defaults to
`bay-admin`, an account provisioning itself creates. Override the SSH user
for that one run:

```bash
bin/bay provision production -- -u root
```

Subsequent provisions/deploys use `bay-admin` as normal.

## Gateway Paths

### Headscale (Recommended)

Headscale is a self-hosted Tailscale coordination server. Devices join your private tailnet and get automatic, encrypted tunnels to your VPN-protected services.

**When to choose**: You want a modern VPN with easy device enrollment, mobile client support, and optional OIDC self-service.

**Setup walkthrough**:

1. The wizard asks for a Headscale domain (e.g., `hs.example.com`)
2. Create a DNS A record: `hs.example.com → your-server-ip`
3. Run `bin/bay provision production && bin/bay deploy production`
4. After first deploy, a panel shows enrollment steps:
   - Enroll a device: `bin/bay gateway enroll` (creates the user, mints a key, prints the join command)
   - On your device: `tailscale up --login-server=https://hs.example.com --authkey=KEY`
5. Manage nodes/users/keys via the CLI: `bin/bay gateway --help`
   (there is no admin web UI; OIDC self-service enrollment is optional). See
   `docs/access-gateways.md`.

**Generated files**:
- `group_vars/all/access_gateway.yml` — `access_gateway: headscale`, `headscale_domain`
- `group_vars/all/vpn_access.yml` — tailnet CIDR `100.64.0.0/10`
- `group_vars/all/security.yml` — UDP ports 41641 (DERP relay) and 3478 (STUN)

### WireGuard

Manual VPN with static peer configuration. You manage peer keys and IPs yourself.

**When to choose**: You already have a WireGuard setup, or you prefer full manual control.

**Setup walkthrough**:

1. The wizard asks for peer IPs (your devices' WireGuard addresses)
2. Peers are added to `group_vars/all/vpn_access.yml`
3. Configure your devices with the server's WireGuard public key

**Generated files**:
- `group_vars/all/access_gateway.yml` — `access_gateway: wireguard`
- `group_vars/all/vpn_access.yml` — your peer IPs in `vpn_allowed_ips`

### None (No Gateway)

All services are publicly accessible. No VPN.

**When to choose**: All your services are public, or you'll add VPN later.

**Note**: Services with `access: vpn` in `services.yml` will still be tagged for VPN access, but without a gateway they'll be unreachable. Use `access: public` for all services, or add a gateway later by re-running `bin/bay setup`.

## Service Catalog

The wizard includes a curated catalog of self-hosted services:

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

Dependencies are auto-selected — choosing n8n automatically adds PostgreSQL.

You can always add more services later by editing `group_vars/all/services.yml` directly. See **[services.md](services.md)** for the full schema reference.

## What Gets Generated

| File | Purpose |
|------|---------|
| `hosts/production` | Server inventory (IP addresses) |
| `group_vars/all/main.yml` | Project identity (`stack_name`, users, Docker config) |
| `group_vars/all/services.yml` | Service and accessory definitions |
| `group_vars/all/users.yml` | SSH keys for server access |
| `group_vars/all/security.yml` | Firewall rules, CrowdSec, SSH hardening |
| `group_vars/all/vpn_access.yml` | VPN IP whitelist |
| `group_vars/all/access_gateway.yml` | Gateway type and config |
| `group_vars/production/main.yml` | Build strategy, registry credentials |
| `group_vars/production/domains.yml` | Domain and Let's Encrypt email |
| `group_vars/production/secrets.yml` | Vault-encrypted credentials |
| `ansible.cfg` | Ansible configuration |
| `deploy.yml` / `provision.yml` / `restore.yml` | Wrapper playbooks |
| `Makefile` | Bootstrap aliases |
| `.gitignore` | Ignores `.bay/`, `.vault_pass`, etc. |
| `README.md` | Project-specific getting started guide |
| `tests/test_infra.sh` | Infrastructure test suite |

In multi-region mode, additional files are created:
- `group_vars/<region>/main.yml` for each region (with `domain_base` override)
- The inventory uses `[production:children]` grouping

## Manual Setup (Without the Wizard)

If you prefer to configure everything manually, use `bin/bay setup --no-interactive` to copy example files, then edit them:

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

### `group_vars/all/services.yml`

```yaml
services:
  gatus:
    access: public
    image: twinproduction/gatus:latest
    domains:
      - status.example.com        # CHANGE: your domain
    ports:
      internal: 8080

accessories:
  postgres:
    image: postgres:17
    port: "127.0.0.1:5432:5432"
    volumes:
      - pg_data:/var/lib/postgresql/data
    env:
      clear:
        POSTGRES_DB: app
        POSTGRES_USER: app
      secret:
        - POSTGRES_PASSWORD
    backup:
      method: pg_dump
      schedule: "0 3 * * *"
      retain: 7
```

### `group_vars/all/access_gateway.yml`

```yaml
# Choose one: headscale, wireguard, or none
access_gateway: headscale

# Required for headscale:
headscale_domain: hs.example.com  # CHANGE: your headscale domain
```

### `group_vars/production/domains.yml`

```yaml
domain_base: example.com          # CHANGE: your domain
letsencrypt_email: admin@example.com
```

### `group_vars/production/secrets.yml`

```yaml
secrets:
  POSTGRES_PASSWORD: "changeme"   # CHANGE: generate with bin/bay secret
```

Encrypt with: `bin/bay vault encrypt production`

## Using HTTPS Instead of SSH

The generated Makefile defaults to SSH for cloning the framework. To use HTTPS instead, override `BAY_REPO`:

```bash
make bay:setup BAY_REPO=https://github.com/AltanS/bay.git
```

## Bootstrap Script

The bootstrap script scaffolds the project and installs dependencies:

```bash
mkdir my-infra && cd my-infra
git clone git@github.com:AltanS/bay.git .bay
.bay/bootstrap.sh
```

After it completes, run `bin/bay setup` for the interactive wizard.
