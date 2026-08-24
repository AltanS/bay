# Debug Agent

The debug agent creates a narrow-permission SSH user (`debugbot`) so an AI coding assistant like Claude Code can connect to a server and diagnose problems. It is disabled by default — enable it explicitly when you want remote AI-assisted troubleshooting.

## What this account can and cannot do

Be honest with yourself about what you are enabling. This is **not a sandbox**, and it is not fully read-only.

**It can:**

- Read files under the allowlisted log directories (`/var/log`, the stack `logs/` directory), including root-owned ones, through `sudo bay-readlog`.
- Read the journal and unit state through `sudo bay-journal` / `sudo bay-systemctl-ro`.
- List containers, read container logs, and inspect container configuration through `sudo bay-docker-ro`. **Container `inspect` output includes environment variables**, so any secret you pass to a container as an env var is visible to this account.
- Read the CrowdSec decision/alert lists and the nftables ruleset.
- Write into its own home directory (`~/tmp`), and run any unprivileged command a normal shell user can run.

**It cannot (with the defaults):**

- Read `/etc/shadow`, `/root`, anything containing `.ssh`, `secret`, `vault`, `.env`, or a credential file name — `bay-readlog` refuses those paths and resolves symlinks with `realpath -e` first, so a link out of the allowlist does not help.
- Start, stop, enable or edit a systemd unit.
- Run `docker exec`, `docker run`, `docker cp`, or pass `-v` to docker.
- Open a pager under `sudo`. That matters: `less` accepts `!sh`, so a paged `sudo journalctl` is a root shell. Both wrappers force `--no-pager`.

**It becomes root if you:**

- Set `debug_agent_docker_access: true` (the `docker` group is root — see below).
- Add a bare binary such as `/usr/bin/cat` back into `debug_agent_sudoers_commands`. sudoers matches a *command*, never its *argument*, so `/usr/bin/cat` grants `sudo cat /etc/shadow`.

> **The `docker` group is root.** Any member can start a container that bind-mounts `/`, then read or write every file on the host — the vault password included. There is no read-only docker group. Bay therefore keeps `docker` out of the default groups and serves the read-only docker subcommands through the `bay-docker-ro` wrapper instead.

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

This creates the `debugbot` user with the configured SSH keys, installs the wrapper scripts into `/usr/local/bin/`, and writes `/etc/sudoers.d/debugbot`.

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

**The hook is a guard, not a boundary.** It keeps an agent from reaching for `root@` out of habit; it does not stop anything determined, because a hook only sees the command string it is handed. The real boundary is server-side: sshd's `AllowGroups`/`AllowUsers`, the account's own privileges, and the sudoers rules above. Do not enable the debug agent on the strength of the hook alone.

What the hook does: it denies by default. If a command mentions `ssh`, `scp`, `sftp` or `rsync`, it must start with that command (leading `VAR=value` assignments are allowed), every `user@host` in it must be the allowed user, and the command must contain no `;`, `&&`, `||`, `|`, backtick or `$(...)`. `-J`, `-F`, `-e/--rsh`, and `-o ProxyCommand/ProxyJump/User/RemoteCommand/LocalCommand` are refused outright. Set `BAY_DEBUG_AGENT_USER` if you renamed `debug_agent_user`.

## Wrappers

sudoers can only match a command, so every entry that would take a path operand goes through a wrapper that validates its own arguments. All four live in `/usr/local/bin/` and are owned by root.

| Wrapper | Purpose | Refuses |
|---------|---------|---------|
| `bay-readlog <cat\|head\|tail\|grep\|wc\|ls> [flags] <path>` | Reads a file under `debug_agent_readable_paths`. The path is the **last** argument and is resolved with `realpath -e`. | Paths outside the allowlist, escaping symlinks, `/root`, `/proc`, `/sys`, `/etc/shadow`, and any path containing `.ssh`, `secret`, `vault`, `.env`, `credential`, `password`. Also any flag outside a small allowlist (`-n`, `-c`, `-i`, `-E`, `-F`, `-A/-B/-C`, …) — notably `grep -f`. |
| `bay-journal [flags] [unit]` | `journalctl` with `--no-pager` forced. | Every flag outside a read-only allowlist, so `--vacuum-*`, `--rotate`, `--flush`, `--root`, `--file` are all out. |
| `bay-systemctl-ro <status\|is-active\|is-enabled\|is-failed\|list-units\|list-timers\|show\|cat> [unit]` | Unit state with `--no-pager` forced. | Every other verb — `start`, `stop`, `restart`, `enable`, `mask`, `daemon-reload`, `edit`. |
| `bay-docker-ro <ps\|logs\|inspect\|stats\|images\|system df\|compose ps\|compose logs>` | The read-only docker surface **without** the docker group. `stats` always gets `--no-stream`. | Every other subcommand (`exec`, `run`, `cp`, `build`, `rm`, …) and every flag outside `--tail`, `--since`, `--until`, `-n`, `--no-stream`, `--format`, `-a/--all`, `-t/--timestamps`, `--no-trunc`. `-v`/`--volume`/`--mount`/`-u`/`--privileged`/`-H` are named refusals. |

Call them through sudo:

```bash
sudo bay-readlog tail -n 200 /var/log/syslog
sudo bay-journal -u docker -n 100
sudo bay-systemctl-ro status traefik
sudo bay-docker-ro logs --tail 50 traefik
```

## Permissions

The `debugbot` user gets:

**Groups:**

- `ssh-access` — allowed through SSH (required by `AllowGroups` in sshd_config)
- `adm` — read access to system logs (`/var/log/`)
- `docker` — **only** if you set `debug_agent_docker_access: true`, which makes the account root

**Sudo commands** (passwordless, `root` target only):

| Category | Commands |
|----------|----------|
| File inspection | `bay-readlog` (path-validated) |
| System | `bay-journal`, `bay-systemctl-ro` |
| Docker | `bay-docker-ro` |
| CrowdSec | `cscli decisions list`, `cscli alerts list/inspect`, `cscli metrics`, `cscli bouncers/machines/hub list` |
| Firewall | `nft list ruleset` |

The sudoers file also sets `env_reset`, a fixed `secure_path`, and `!use_pty` for the account.

## Customization

Override any default in `group_vars/all/security.yml`:

```yaml
# Change the username
debug_agent_user: mybot

# Add a group (do not add `docker` here — use debug_agent_docker_access)
debug_agent_groups:
  - ssh-access
  - adm
  - mygroup

# Widen what bay-readlog will serve
debug_agent_readable_paths:
  - /var/log
  - "{{ stack_dir }}/logs"
  - /opt/myapp/logs

# Extend the sudoers list. Add wrappers or argument-free subcommands only —
# a bare /usr/bin/cat here is a grant to read every file on the host.
debug_agent_sudoers_commands:
  - /usr/local/bin/bay-readlog *
  - /usr/local/bin/bay-journal *
  - /usr/local/bin/my-health-check

# Disable sudoers entirely (the account keeps plain `adm` group log access)
debug_agent_sudoers: false

# Disable scratch directory creation
debug_agent_create_tmp: false
```

## Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `debug_agent_enabled` | `false` | Master switch — must be `true` to activate |
| `debug_agent_user` | `debugbot` | Username for the debug account |
| `debug_agent_groups` | `[ssh-access, adm]` | Supplementary groups. `docker` is no longer among them — see `debug_agent_docker_access` |
| `debug_agent_docker_access` | `false` | Add the account to the `docker` group. **This makes it root** |
| `debug_agent_readable_paths` | `[/var/log, {{ stack_dir }}/logs]` | Directories `bay-readlog` will serve |
| `debug_agent_keys` | `[]` | Direct SSH public keys |
| `debug_agent_github_users` | `[]` | GitHub usernames (keys fetched automatically) |
| `debug_agent_sudoers` | `true` | Install passwordless sudo rules |
| `debug_agent_sudoers_commands` | _(see Permissions)_ | Allowed sudo commands — wrappers, not bare binaries |
| `debug_agent_create_tmp` | `true` | Create `~/tmp` scratch directory |
