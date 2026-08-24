# Changelog

Notable changes to Bay, newest first.

Consumers pin a framework version in `.bay-version` and move with
`bin/bay update`. Read the entries between your pinned version and the latest
before upgrading — anything needing manual action is called out under
**Upgrade notes**.

## [0.2.0] — 2026-08-24

### Added

- **`.githooks/pre-push` — two gates on every push out of this repo.**
  - *History gate.* If the remote is recognised as the public one, a ref that
    descends from the private root commit is refused. The public repo is a
    separate orphan history; publishing such a ref would expose everything
    behind it.
  - *Content gate.* `scripts/leak-scan.sh` now runs against **every commit
    being pushed**, not just the tip. A leak introduced in one commit and
    fixed in the next leaves a clean worktree and a dirty history — and
    history is what a push publishes.
- **`scripts/leak-scan.sh` takes an optional REF argument.**
  `bash scripts/leak-scan.sh <commit>` scans that commit's tree instead of the
  working tree. With no argument the behaviour is unchanged.
- **Environment variables read by the hook:**
  - `BAY_PUSH_SKIP_GUARDS=1` — skip both gates. Prints a loud multi-line
    warning to stderr. For a human who has read the runbook, not for scripts.
  - `BAY_PUBLIC_REMOTE_PATTERN` — extra extended regex that marks a remote URL
    as public, on top of the built-in match.
  - `BAY_PRIVATE_ROOTS` — space-separated root commit SHAs, overriding the
    built-in list. Used by the tests.
- `tests/test_pre_push_hook.py` proves both gates go red, including the
  leak-in-a-middle-commit case that a tip-only scan would wave through.

### Fixed

- **`--check` no longer kills the deploy play.** `container_lifecycle`'s
  `Reconciler report` task debug-printed `_reconcile_result.stdout |
  from_json`. In check mode the reconciler command task is skipped, so
  `stdout` is empty and `from_json` raised — the play died with exit 2 and
  `bin/bay deploy <env> -- --check --diff` was unusable as a dry run. The
  report is now guarded by `when: _reconcile_result is not skipped`. It is
  deliberately not an `rc`-based guard: a command task skipped by check mode
  still registers `rc: 0`.

### Upgrade notes

- Run `make hooks` (or `make install`) in every existing clone, **including the
  public one**. `core.hooksPath` is per-clone git config, so a clone that has
  never run it has no hooks at all and neither gate applies.

## [0.1.1] — 2026-08-24

Post-launch hygiene pass over the public tree.

### Security
- `debug_agent` no longer grants root by accident. The `docker` group is
  gone from the default groups (it is root-equivalent); opt back in with
  `debug_agent_docker_access: true`, documented as "this is root". The bare
  `cat`/`tail`/`head`/`grep`/`journalctl`/`systemctl` sudo entries — which
  allowed `sudo cat /etc/shadow` and a `!sh` root shell from the pager —
  are replaced by four argument-validated wrappers: `bay-readlog`
  (paths limited to `debug_agent_readable_paths`), `bay-journal`,
  `bay-systemctl-ro` and `bay-docker-ro` (all `--no-pager`, read-only
  verbs only). Sudo target narrowed from `(ALL)` to `(root)` with
  `env_reset`, `secure_path` and `!use_pty`.
- `files/hooks/validate-ssh.sh` is now deny-by-default: it parses the
  command, refuses chaining, `-J`, `-F`, `-e`, `ProxyCommand`/`ProxyJump`/
  `User` options, and checks every `user@host` token. It was previously
  bypassed by any command containing the string `debugbot@`. The docs now
  describe it as a client-side guard, not a security boundary.

### Changed
- `scripts/leak-scan.sh` closes five blind spots found by planting test
  leaks: case-insensitive and apex-domain hostname matching, base64 in the
  entropy check, an IPv6 section, a long-hex section, and a tracked-junk
  check; the `vendor/` exclusion now applies only to the entropy sections.
- Internal tracker IDs (`M85`-style) removed from docs, comments and tests.
- Small identity scrub: LICENSE holder, example usernames, one ADR incident
  narrative.

### Upgrade notes
- `debug_agent` lives in `provision.yml`: run
  `bin/bay provision <env> --tags users,agent-debug`.
- If your agent relied on `sudo cat`/`sudo tail`/`sudo docker ps`, switch
  it to `sudo bay-readlog`, `sudo bay-journal`, `sudo bay-docker-ro`.
  A consumer that overrides `debug_agent_sudoers_commands` or
  `debug_agent_groups` keeps its own list — and its own exposure.

## [0.1.0] — 2026-08-24

First public release.

Bay had a long private life before this tag — roughly 340 releases of it. The
version line restarts at 0.1.0 because this is the first release anyone
outside its original operator could actually use, and it would be dishonest to
present a 1.x number as a stability promise to a public that has never run it.
The code is mature; the public contract is new.

### What Bay does

Provisions and operates hardened Docker hosts with Ansible and a Python CLI:

- **Declarative service surface.** One `services.yml` describes every app and
  accessory — domains, ports, env, secrets, health checks, log retention.
- **Traefik ingress** in host-network mode, so services see real client IPs,
  with automatic TLS.
- **Access gateways.** `none` for a plain public deploy, `wireguard` for
  hand-configured peers, or self-hosted `headscale` for a managed tailnet with
  split-DNS, ACLs and per-device identity. All three sit behind one adapter
  contract; see `docs/access-gateways.md`.
- **CrowdSec and nftables** for intrusion detection and firewalling.
- **A server-side reconciler** that diffs desired against running container
  state instead of re-applying everything each deploy.
- **Build pipeline** with local, remote and registry strategies, a self-hosted
  Zot registry, and a webhook receiver for push-to-deploy.
- **Alerting** with a single registry of alert IDs and severities, fanned out
  to Telegram or any webhook adapter.
- **Restic backups**, multi-region deploys, and cross-region service links.

### Getting started

`README.md` for the tour, `docs/onboarding.md` for a walkthrough, and
`example/` for a complete runnable consumer to copy.

### Licence

MIT.
