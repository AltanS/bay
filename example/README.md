# My Infrastructure

Consumer project powered by [Bay](https://github.com/AltanS/bay).

## Prerequisites

- [uv](https://docs.astral.sh/uv/) — installs Python, Ansible, and all dependencies automatically
- A target server running Ubuntu (22.04/24.04)
- SSH access to the target server (root for first provision)

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

## Setup

```bash
# Clone the framework and pin the version (equivalent to make bay:setup below)
git clone git@github.com:AltanS/bay.git .bay
.bay/bootstrap.sh

# OR, equivalent, if you're starting from this scaffold's Makefile:
make bay:setup

# Run the interactive setup wizard — walks through project name, server
# address, domain, SSH keys, access gateway, services, and the vault
# password (generated or entered manually; see below for the manual path)
bin/bay setup

# Pre-flight checks — environment, then config
bin/bay doctor      # DNS, SSH, vault password reachability
bin/bay validate    # YAML/schema, inventory, vault keys (also runs on every deploy)
```

`bin/bay setup` generates every file under `group_vars/`, `hosts/`, and the
project root — including the vault password if you chose "generate" in the
wizard. See [docs/onboarding.md](.bay/docs/onboarding.md) for the full
wizard walkthrough and the file list it produces.

#### Without the wizard

If you'd rather skip the wizard and edit the scaffold by hand:

```bash
make bay:setup

# Create vault password (used to encrypt secrets) manually
echo '<your-vault-password>' > .vault_pass
```

Then edit the files under **Configure** below directly.

## Configure

Edit these files for your environment:

| File | What to change |
|------|---------------|
| `hosts/production` | Your server IP |
| `group_vars/all/main.yml` | Stack name, admin/app users |
| `group_vars/all/services.yml` | Your service definitions |
| `group_vars/all/users.yml` | SSH keys (GitHub URLs) |
| `group_vars/all/security.yml` | GitHub usernames for debug agent |
| `group_vars/production/domains.yml` | Let's Encrypt email |
| `group_vars/production/secrets.yml` | Credentials (encrypt with vault) |

### Build strategies

The framework supports building services from source or pulling pre-built images from a registry. Services with a `build:` block are built locally on the target server by default. For more advanced patterns (dedicated build servers, registry-only production), see [docs/services.md](.bay/docs/services.md).

### Single-server setup

The scaffold includes multi-region example files. If you are deploying to a single server, remove them to keep your project clean:

```bash
rm hosts/production-multi-region
rm -r group_vars/eu group_vars/na
```

Use `hosts/production` as your inventory and hardcode domains directly in `services.yml`. See [docs/multi-region.md](.bay/docs/multi-region.md) if you need multi-region later.

### DNS

Set a wildcard DNS record pointing to your server:

```
*.example.com  →  <server-ip>
```

## Deploy

```bash
# Provision server (first time — hardens SSH, installs Docker, firewall, etc.)
bin/bay provision production
# If the server only has root (no bay-admin account yet), override the SSH user:
bin/bay provision production -- -u root

# Deploy services (auto-detects if infrastructure rig is needed)
bin/bay deploy production

# Force full deploy including infrastructure roles
bin/bay deploy --rig production

# Deploy only specific tags
bin/bay deploy production --tags traefik
bin/bay deploy production --tags deploy_stack

# Dry run
bin/bay deploy production -- --check --diff
```

## Secrets

```bash
# Edit encrypted secrets
bin/bay vault edit production

# View secrets
bin/bay vault view production
```

## Update framework

```bash
# Install the version pinned in .bay-version
bin/bay install

# Update to the latest framework release (bumps .bay-version)
bin/bay update

# Check current version status
bin/bay status
```

## Run tests

```bash
bin/bay test
```

## More info

See the [Bay README](https://github.com/AltanS/bay) for full documentation on services, accessories, access modes, and architecture.
