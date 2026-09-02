# Changelog

Notable changes to Bay, newest first.

Consumers pin a framework version in `.bay-version` and move with
`bin/bay update`. Read the entries between your pinned version and the latest
before upgrading — anything needing manual action is called out under
**Upgrade notes**.

## [Unreleased]

### Changed

- The setup wizard now defaults the access gateway to **none**. Headscale is
  still offered; add it later with `bin/bay setup --gateway headscale`.
  Headscale adds a DNS record, a Tailscale client install and four post-deploy
  steps to a first run, which is a lot to carry before anything works.
- `bin/bay setup --defaults` now requires `--server-ip` and `--domain`. It
  previously scaffolded `0.0.0.0` and `example.com`, which can never deploy.
- The wizard scaffolds `group_vars/all/alerts.yml` with an empty
  `alert_recipients` list. The legacy `docker_monitor_telegram_*` keys are no
  longer generated — they sit at env level, outrank `group_vars/all/alerts.yml`
  by Ansible precedence, and cause duplicate delivery once a real recipient is
  added. Configure alerts with `bin/bay alerts`. See `docs/alerting.md`.
- `bin/bay setup` takes `--email` (alias of `--letsencrypt-email`), honoured on
  every path including `--no-interactive`. Without it, `admin@<domain>` is
  derived and announced. The example copy previously left the shipped
  placeholder in place, so a `--no-interactive` scaffold failed validate.
- `bin/bay validate` now hard-fails on an empty `letsencrypt_email`. There is
  no ACME opt-out in the framework, so an empty value always means broken SSL.
  `example/group_vars/production/domains.yml` shipped it empty; it now carries
  a placeholder that validate rejects until you replace it.
- One documented entry path: clone over **HTTPS** into `.bay/`, run
  `.bay/bootstrap.sh`, then `bin/bay setup`. `README.md`, `SKILL.md`,
  `docs/onboarding.md` and `example/README.md` now say the same thing, and a
  test fails the build if they drift apart again.
- The generated `Makefile`'s `bay:setup` target clones the framework and then
  calls `.bay/bootstrap.sh`. It no longer carries its own copy of the pin /
  symlink / `uv sync` / Galaxy-install logic, which had already drifted from
  the script — it never created `bin/bay`.
- `BAY_REPO` now defaults to the HTTPS clone URL, in the generated `Makefile`
  and in `bin/bay dev-unlink`'s fallback. Override it for SSH:
  `make bay:setup BAY_REPO=<ssh url>`.
- `bin/bay setup` and `bootstrap.sh` now write an identical `bin/bay` wrapper,
  copied from `scripts/bin-bay-wrapper.sh`. Previously whichever ran first won,
  and only the bootstrap version unset `VIRTUAL_ENV`.

### Fixed

- `bin/bay doctor`'s SSH check connected as your local username. It now tries
  `root` (fresh server) and then `admin_user` (provisioned server), and names
  the one that worked.
- `bin/bay doctor`'s DNS check probed the bare apex domain, which a wildcard
  `*.example.com` record does not cover — so a correct DNS zone reported
  NXDOMAIN. It now resolves the first service domain, or
  `status.<domain_base>`.
- `bin/bay doctor`'s webhook check read `group_vars/<env>/services.yml`, which
  the wizard never writes, so it always skipped. It now reads
  `group_vars/all/services.yml`, falling back to the per-environment file.
- `bin/bay doctor` could print "All checks passed" after a probe raised. A
  failed probe is now counted as an issue.
- The framework-version guard's error messages named the pre-1.0 paths
  `.argo/version.yml` and `.argo-version`. They now name `.bay/version.yml`
  and `.bay-version`, matching the layout every consumer actually has.
- `make bay:setup` on its own left no `bin/bay` wrapper, so the documented next
  command could not run. The delegated target creates it.
- The post-setup next-steps panel omitted the DNS record, `bin/bay validate`
  and `bin/bay doctor`. All three are now listed, and DNS guidance is printed
  for every gateway choice rather than only for Headscale — with no gateway,
  the operator was told to deploy with no record in place and Traefik's first
  ACME challenge failed.
- `docs/features.md` documented an `admin` access mode that does not exist (the
  schema allows `public` and `vpn`), and described `bay validate` as performing
  `bay doctor`'s SSH/DNS/vault probes. Both corrected.
- `README.md` linked to a production-access document that is not part of this
  repo, and instructed a hand-written `git tag` instead of `make release`.
  `CONTRIBUTING.md` now documents the release process.
- "bay not found" no longer tells you to run `bin/bay setup`, which cannot run
  without `.bay/`. It names the clone and `.bay/bootstrap.sh` instead.
- The framework-version-drift message in `provision.yml` and `deploy.yml` names
  `bin/bay install` instead of the `make bay:install` alias.

### Upgrade notes

- No host-side action for the alert change. Existing consumers that set
  `docker_monitor_telegram_*` keep working; migrate to `alert_recipients` when
  convenient and delete the legacy keys in the same change, or alerts arrive
  twice.
- Existing consumers: your `Makefile` is a generated file. Re-run
  `bin/bay setup --force` to pick up the new `bay:setup` target (it backs the
  old one up to `Makefile.bak`), or leave it — the old target still works.

## [0.2.4] — 2026-08-25

### Fixed

- Restore notifications go through the alert channel. `restore.yml` POSTed
  directly to `api.telegram.org`, so a restore alert reached one hard-coded
  sink, ignored every recipient's `min_level`, never appeared in
  `bin/bay alerts list` and could not be muted. It now composes an alert and
  delegates delivery to `roles/alert_channel`, like every other alert.
- A failed restore now alerts. Previously only success notified, so the case
  that matters — the restore broke and the pre-restore backup is the way back
  — was silent. `restore.yml` runs inside a block with a rescue handler that
  emits `restore.failed` and re-raises the original failure.

### Added

- Two alert IDs: `restore.completed` (`info`, default **off**) and
  `restore.failed` (`critical`, default **on**).

### Changed

- The control-node alert fan-out moved from
  `roles/deploy_stack/tasks/send_deploy_alert.yml` to
  `roles/alert_channel/tasks/send_alert.yml`, and callers reach it with
  `include_role` + `tasks_from: send_alert`. Ansible has no cross-role task
  include path, so while it lived in `deploy_stack` no playbook could use it —
  which is exactly why `restore.yml` had its own sender. Behaviour for
  `deploy.complete` / `deploy.failed` is unchanged.
- `tests/test_alert_registry.py` scans the top-level playbooks as well as
  `roles/`. A new guard in `tests/test_alert_channel.py` fails the build on any
  Telegram request outside `roles/alert_channel`.

### Upgrade notes

- Restore alerts are now **routed**, not broadcast. `restore.failed` is
  `critical`, so it reaches every recipient. `restore.completed` is `info` and
  ships **default-off** like the other success notices — if you relied on the
  old unconditional "Restore complete" message, opt back in:

  ```yaml
  # group_vars/all/alerts.yml
  alerts_enabled:
    - restore.completed
  ```

  A recipient with `min_level: warn` or higher will not receive
  `restore.completed` unless it is named in `alerts_enabled`, which overrides
  `min_level`.
- No deploy is needed for this. Both alerts are sent from the control node by
  `restore.yml` itself, so `bin/bay update` is enough.

## [0.2.3] — 2026-08-25

### Changed

- Docs pass from a newcomer review: aligned `example/README.md`'s Setup
  section with the main README's Quick Start (wizard-first, manual `.vault_pass`
  path kept as a fallback); added `bin/bay validate` ahead of provision/deploy
  in the README and onboarding guide, with `doctor` (environment) vs
  `validate` (config, also runs automatically on every deploy) spelled out;
  fixed the "copied by bootstrap.sh" attribution to `bin/bay setup`; fixed a
  broken doc-server pointer in `docs/README.md` and added a Glossary; fixed
  the `docs/tailnet-ingress.md` ACL-policy anchor in `docs/access-gateways.md`;
  gave the public pre-push hook a runbook pointer that resolves for outside
  contributors; documented accessory `expose:` and a worked `database:`
  binding example in `docs/services.md`, plus a new "Rotating a secret"
  section covering the config-hash/env-digest recreate behavior; clarified
  how an operator alert mute reaches the hosts in `docs/alerting.md`; added
  CrowdSec lockout-recovery re-enable + verify steps; and documented the
  first-provision-as-root command form.

## [0.2.2] — 2026-08-25

### Changed

- Corrected the copyright holder named in LICENSE.

## [0.2.1] — 2026-08-25

### Fixed

- **`--check` no longer kills the play in three more places.** Ansible does not
  execute `command`/`shell` modules in check mode, but the registered result
  still carries `rc: 0` and an **empty** `stdout`. Anything that then parses
  that stdout dies with exit 2, taking the whole dry run with it. Same shape as
  the reconciler fix in 0.2.0.
  - `roles/tailscale_register` — "Set registration needed fact" piped
    `tailscale status --json` through `from_json`. Now guarded with
    `is not skipped`, and every consumer of the fact reads
    `_needs_registration | default(false)`, so the role cannot register a node
    in a dry run.
  - `roles/headscale` — "Install validated headscale ACL policy" copied
    `.policy.hujson.staged`, which does not exist in check mode, and aborted
    with "Source does not exist". Now skipped in check mode; the staging task
    still shows the rendered policy as a diff.
  - `restore.yml` — "Parse snapshot details" piped `restic snapshots --json`
    through `from_json`. Now guarded, and every task that dereferences `_snap`
    is gated on it being defined.

  None of these uses `rc is defined`: a command task skipped by check mode
  registers `rc: 0`, so that guard passes and the crash happens anyway.
- **A comment-only nftables change no longer restarts the Docker daemon.**
  Reloading nftables wipes the chains Docker installs at daemon start, so the
  reload handler has to restart Docker and every container on the host bounces.
  `Deploy nftables configuration` is a `template`, which reports "changed" for
  any byte difference — so an edit to a comment in `nftables.conf.j2` (or a
  re-rendered `ansible_managed` header) triggered that outage on every host.
  The live file is now fingerprinted with comments and trailing whitespace
  stripped, before and after the write, and the reload is notified only when
  the fingerprints differ. The file itself is still always written, so comments
  land on the host; a real ruleset change reloads exactly as before, including
  on first install.

### Upgrade notes

- The nftables fix is picked up by `bin/bay deploy --rig <env>` (the role runs
  under `_rig_mode`) or by `bin/bay provision <env> --tags nftables`. A plain
  non-rig `bin/bay deploy` does **not** run the role. Upgrading costs no
  container bounce: if your ruleset is semantically unchanged, the new
  fingerprint comparison matches and nothing is reloaded.

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
