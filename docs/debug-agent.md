# Debug Agent

The debug agent creates a limited-permission SSH user (`debugbot`) for AI coding assistants like Claude Code to connect to servers for read-only debugging. It's disabled by default — enable it explicitly when you want remote AI-assisted troubleshooting.

## Why

When debugging production issues, Claude Code can SSH into servers to inspect logs, check container status, review firewall rules, and diagnose problems — without needing your admin credentials or full root access. The debug agent user gets read-only sudo commands and Docker/log access, nothing more.

## Setup

### 1. Enable the debug agent

Add to `group_vars/all/security.yml`:

```yaml
debug_agent_enabled: true
debug_agent_github_users:
  - YourGitHubUsername
```

SSH keys are fetched automatically from `https://github.com/<username>.keys`. You can also provide keys directly:

```yaml
debug_agent_keys:
  - "ssh-ed25519 AAAA... claude-code"
```

### 2. Provision

```bash
bin/bay provision production --tags users,agent-debug
```

This creates the `debugbot` user with the configured SSH keys and sudo rules.

### 3. Set up the Claude Code SSH hook

Copy the hook script to your consumer project:

```bash
cp .bay/files/hooks/validate-ssh.sh .claude/hooks/
```

Add to `.claude/settings.json`:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash",
        "hooks": [
          {
            "type": "command",
            "command": "bash .claude/hooks/validate-ssh.sh"
          }
        ]
      }
    ]
  }
}
```

This restricts Claude Code to only SSH as `debugbot@` — it cannot connect as any other user.

## Permissions

The `debugbot` user gets:

**Groups:**
- `ssh-access` — allowed through SSH (required by `AllowGroups` in sshd_config)
- `adm` — read access to system logs (`/var/log/`)
- `docker` — run `docker` commands (inspect containers, read logs)

**Sudo commands** (passwordless, read-only):

| Category | Commands |
|----------|----------|
| File inspection | `tail`, `head`, `cat`, `grep`, `wc`, `ls` |
| System | `journalctl`, `systemctl status`, `systemctl status *` |
| CrowdSec | `cscli decisions list`, `cscli alerts list/inspect`, `cscli metrics`, `cscli bouncers/machines/hub list` |
| Docker | `docker compose logs/ps/top`, `docker stats --no-stream` |
| Firewall | `nft list ruleset` |

## Customization

Override any default in `group_vars/all/security.yml`:

```yaml
# Change the username
debug_agent_user: mybot

# Add or replace groups
debug_agent_groups:
  - ssh-access
  - adm
  - docker
  - mygroup

# Extend or replace sudoers commands
debug_agent_sudoers_commands:
  - /usr/bin/tail
  - /usr/bin/cat
  - /usr/bin/journalctl
  # Add your own...
  - /usr/local/bin/my-health-check

# Disable sudoers entirely (Docker group access still works)
debug_agent_sudoers: false

# Disable scratch directory creation
debug_agent_create_tmp: false
```

## Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `debug_agent_enabled` | `false` | Master switch — must be `true` to activate |
| `debug_agent_user` | `debugbot` | Username for the debug account |
| `debug_agent_groups` | `[ssh-access, adm, docker]` | Supplementary groups |
| `debug_agent_keys` | `[]` | Direct SSH public keys |
| `debug_agent_github_users` | `[]` | GitHub usernames (keys fetched automatically) |
| `debug_agent_sudoers` | `true` | Install passwordless sudo rules |
| `debug_agent_sudoers_commands` | _(see Permissions)_ | Allowed sudo commands |
| `debug_agent_create_tmp` | `true` | Create `~/tmp` scratch directory |
