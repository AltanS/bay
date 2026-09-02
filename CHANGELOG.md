# Changelog

Notable changes to Bay, newest first.

Consumers pin a framework version in `.bay-version` and move with
`bin/bay update`. Read the entries between your pinned version and the latest
before upgrading — anything needing manual action is called out under
**Upgrade notes**.

## [0.4.0] — 2026-09-02

### Changed

- **Zot tag-retention policy is now bounded.** The prior policy kept every
  tag pushed OR pulled within `zot_retention_keep_within` (720h) **in
  addition to** the N-most-recent rules, so a busy repo could accumulate
  dozens of tags — one repo hit 91. The policy is now: 10 most recently
  pushed tags (`zot_retention_keep_count`), 3 most recently pulled tags
  (new `zot_retention_keep_pulled_count`, so the image a host is
  currently running survives even if older than the last 10 pushes), and
  any tag matching `zot_retention_always_keep` (new, default `["^latest$"]`).
  `zot_retention_keep_within` is now empty/optional — set it only if you
  understand that a time window is unbounded in count.

### Upgrade notes

- Consumers that already override `zot_retention_keep_within` keep their
  window (it now layers on top of the bounded count rules instead of
  replacing them). Everyone else gets the new bounded policy after
  `bin/bay deploy <env> --tags zot` on the registry host. Zot applies the
  new policy on its **next GC pass**, not immediately — GC runs on
  `zot_gc_interval` (default `24h`).

## [0.3.4] — 2026-09-02

### Fixed

- **A git credential prompt could hang a deploy indefinitely on the
  SSH/no-token fetch path.** Every git invocation now disables terminal
  prompts and fails fast instead.

## [0.3.3] — 2026-09-02

### Fixed

- **Two more tasks reported "changed" on every deploy of an unchanged
  consumer.** The git-deploy-config timestamp sentinel was written with
  mode `0644` and no group, so `webhook.yml`'s state/ re-group task found
  it group-write-less and flipped it back on every following run. Fixed by
  writing the sentinel with group `git_deploy_build_group` and mode `0664`,
  same as the directory it lives in. Applied the same fix to the other
  writers under `state/`: `cb_state_migration.yml`'s migration script,
  `rebuild.sh`'s `_write_state`, and the stall watchdog's audit log and
  rate-limit file. The first deploy after this change flips the sentinel's
  mode once; every deploy after that is a no-op.
- **The git_deploy-side image pull reported "changed" on every deploy,
  even with nothing new to pull.** `roles/git_deploy/tasks/main.yml`'s
  "Pull freshly-pushed images on deployment server" task (the
  remote-build-strategy counterpart to `build_image`'s batched pull) had
  `changed_when: true` unconditionally. Fixed by keying `changed_when` on
  `docker pull`'s own "Downloaded newer image" marker, matching the
  batched task. No behaviour change beyond the reported status.

## [0.3.2] — 2026-09-02

### Fixed

- **`systemd.yml` reset the build state directory to 0755 on every deploy,
  blocking the webhook container from writing its own log.** Two tasks in
  `roles/git_deploy` disagreed about `{{ stack_dir }}/state`. `webhook.yml`
  sets it to owner `app_user`, group `git_deploy_build_group`, mode `2770`,
  so the webhook container (UID 10001, GID 2000) and `rebuild.sh` can both
  write into it. `systemd.yml` runs right after and re-created the same
  directory as mode `0755` with no group at all, undoing that on every
  single run. The webhook container could not write
  `telegram-failures.log` into `/state` on any 0.3.0 or 0.3.1 host, which
  is exactly the failure the `/state` mount was meant to fix. The mode
  flip also made three unrelated tasks report "changed" on every deploy
  for no functional reason. Fixed by making `systemd.yml` declare the same
  owner, group, mode and `become: true` / `become_user: root` as
  `webhook.yml`. The first 0.3.2 deploy changes the directory mode once,
  from `0755` back to `2770`; every deploy after that is a no-op.

## [0.3.1] — 2026-09-02

### Fixed

- **Webhook receiver rate-limit labels rendered as integers, breaking every
  deploy that enables it.** `build_specs.yml` set the
  `bay-webhook-ratelimit` Traefik labels (`ratelimit.average`,
  `ratelimit.burst`) from `"{{ webhook_rate_limit_average | default(10) }}"`.
  Ansible's native Jinja renders a pure `{{ ... }}` template as its native
  Python type, so with no override set the label came out as the int `10`,
  not the string `"10"`. The reconciler feeds specs straight to the Docker
  API, and Docker requires label values to be strings, so `docker create`
  for the webhook receiver failed on every 0.3.0 deploy with the webhook
  receiver enabled, and the container was removed. Fixed by adding
  `| string` to both labels, matching the existing pattern next to them
  (`zot_port | default(5000) | string`, `WEBHOOK_PEER_TIMEOUT`). The
  container is recreated on the next deploy with the fix in place.

## [0.3.0] — 2026-09-02

Three milestones land together: security hardening, onboarding repair and
performance. Read **Upgrade notes** before you bump an existing consumer, the
release adds validate failures that stop a deploy.

### Security

- **Headscale OIDC needs an allowlist.** `headscale_oidc_allowed_domains`,
  `headscale_oidc_allowed_users` and `headscale_oidc_allowed_groups` render
  into the OIDC block. `bin/bay validate` fails when OIDC is on with no
  allowlist, and warns when OIDC is on with no `headscale_acl_policy`.
- **`expose: host` needs `expose_host_ack: true`.** The flag records that you
  accept the port bypassing nftables and CrowdSec. Nothing about the rendered
  port changes.
- **A split entrypoint fails closed.** The deploy now stops when
  `traefik_split_entrypoints` is true and `traefik_public_bind_ip` is blank,
  instead of rendering a wildcard bind that collides with
  `websecure_tailnet`.
- **Traefik TLS floor and metrics bind.** `minVersion` is TLS 1.2 by default,
  and the metrics entrypoint binds `traefik_metrics_bind_ip` (`127.0.0.1`)
  instead of every interface. `sniStrict` stays off unless you set
  `traefik_tls_sni_strict: true`. `tls.options` is dynamic-only configuration,
  so the floor is rendered to `<stack_dir>/dynamic/tls-options.yml` and served
  by Traefik's file provider, which is now always enabled. The same block in
  the static `traefik.yml` is parsed and then ignored, so it enforced nothing.
- **The webhook receiver mounts `<stack_dir>/state`.** It writes its Telegram
  delivery-failure log there. The compose path already mounted it, the
  reconciler spec did not, so on the reconciler path those writes went to the
  container's writable layer and were lost on every recreate.
- **New nftables knobs, both default-compatible.**
  `nftables_forward_permissive` defaults to `true` and
  `nftables_container_host_ports` defaults to empty, so today's behaviour is
  unchanged until you tighten them.
- **Alert credentials leave world-readable files.** The Telegram token and
  alert webhook URL now live in `/etc/bay/alert.env` (0600 root), sourced by
  every notify snippet and read through `EnvironmentFile=` in nine systemd
  units. Emitter scripts drop to 0750, and recipient literals render as
  `BAY_RC_<n>_*` names.
- **PATs leave `argv` and `.git/config`.** `git_deploy` uses a `GIT_ASKPASS`
  helper in `clone_repos`, remote builds and `rebuild.sh`, build directories
  are 0700, and build args pass through the task environment rather than the
  command line.
- **Other credential handling.** `rebuild.sh` reads its HMAC key from a 0600
  file, `zot` takes the htpasswd password on stdin, webhook receiver and
  Watchtower secrets ship via `env_file`, and `no_log` covers the `cscli
  console enroll` and `alert_channel` URI tasks.
- **Basic-auth hashes move from APR1 to bcrypt**, with a deterministic
  secret-derived salt so the render stays stable for the reconciler. Passwords
  are unchanged. This adds a `bcrypt` Python dependency.
- **`bay-docker-ro inspect --format` is restricted** to an allowlist that
  cannot reach `Config.Env`, and the Headscale config file is 0640.
- **Every consumer value that reaches a shell or SQL is quoted.** The services
  schema gained name and env-key regexes plus route patterns, and
  `bin/bay validate` restates them in words. A bare `/` in `public_routes` and
  a backtick in a Traefik rule literal are now errors.
- **`bin/bay vault set` reads the value from stdin** when the positional
  argument is omitted, so a secret stops landing in shell history. The
  positional form still works for one transition release.
- **Build trigger and state directories are 2770**, owned by a fixed
  `bay-build` group (GID 2000) shared by `app_user` and the webhook container
  (UID 10001). They were 0777, so any local user could force a rebuild or
  rewrite the circuit breaker state.
- **Webhook receiver hardening.** A 1 MiB body cap is checked before any read,
  a 256-entry `X-GitHub-Delivery` cache drops repeat deliveries after the HMAC
  check, `/health` returns a service count instead of names, and the webhook
  router carries a Traefik rate-limit middleware
  (`webhook_rate_limit_average|burst|period`).
- **Supply chain pins.** restic is verified by sha256, Watchtower is pinned by
  index digest, the CrowdSec apt repository uses a `signed-by` keyring instead
  of the deprecated global key, and `github.com` host keys ship with the role
  so every `GIT_SSH_COMMAND` uses `StrictHostKeyChecking=yes`.
- **Push gates.** Gate A matches the public remote by root-commit identity,
  bypass environment variables print a loud warning, and `leak-scan` gained a
  lowercase-plus-digit entropy tier proven red by re-injection, plus an
  allowlist for RFC 2606 reserved TLDs.

### Onboarding

- **A scaffolded project now validates and deploys.** The generated Gatus
  service used `healthcheck.path` and the MariaDB accessory used
  `backup.method: mysqldump`, neither of which the schema accepts. They are now
  `healthcheck_path` and `mysql`.
- **Services that declare `config_files` now ship them.** `catalog/gatus/`
  gained `files/gatus/config.yaml`, and `bin/bay setup` copies catalog files
  into the consumer's `files/` through the same helper `bin/bay service add`
  uses.
- **Scaffolded secrets are generated**, by the same generator `bin/bay secret`
  uses, instead of written as empty strings.
- **The SSH-key step has no Skip.** At least one key is required for the admin
  account, because provisioning disables root and password login and a keyless
  admin locks you out. `--defaults` and `--no-interactive` take `--ssh-key` or
  `--ssh-key-file`, or fall back to `~/.ssh/*.pub`.
- **Four new validate hard failures**: a missing `config_files` entry, an admin
  user with no SSH keys, an empty referenced secret, and an empty or
  placeholder `letsencrypt_email`.
- **The wizard defaults the access gateway to none.** Headscale is still
  offered, and you can add it later with `bin/bay setup --gateway headscale`.
  It adds a DNS record, a Tailscale client install and four post-deploy steps
  to a first run, which is a lot to carry before anything works.
- **`bin/bay setup --defaults` requires `--server-ip` and `--domain`.** It
  previously scaffolded `0.0.0.0` and `example.com`, which can never deploy.
  `--defaults` also works without a TTY now.
- **`bin/bay setup` takes `--email`** (alias of `--letsencrypt-email`), honoured
  on every path. Without it, `admin@<domain>` is derived and announced.
- **The wizard scaffolds `group_vars/all/alerts.yml`** with an empty
  `alert_recipients` list. The legacy `docker_monitor_telegram_*` keys are no
  longer generated, they sit at env level and outrank
  `group_vars/all/alerts.yml` by Ansible precedence, which causes duplicate
  delivery once a real recipient is added.
- **One documented entry path**: clone over HTTPS into `.bay/`, run
  `.bay/bootstrap.sh`, then `bin/bay setup`. `README.md`, `SKILL.md`,
  `docs/onboarding.md` and `example/README.md` now agree, and a test fails the
  build if they drift.
- **`make bay:setup` delegates to `.bay/bootstrap.sh`.** It no longer carries
  its own copy of the pin, symlink, `uv sync` and Galaxy-install logic, which
  had drifted and never created `bin/bay`. `BAY_REPO` defaults to the HTTPS
  clone URL, override it for SSH.
- **`bin/bay setup` and `bootstrap.sh` write an identical `bin/bay` wrapper**,
  from the single source `scripts/bin-bay-wrapper.sh`. `bootstrap.sh` also
  snapshots the wrapper before checking out an older pinned tag, which used to
  leave a newer checkout with no wrapper to copy.
- **`bin/bay doctor` is trustworthy now.** The SSH check tries `root` then
  `admin_user` instead of your local username, the DNS check resolves a service
  domain instead of the apex a wildcard record does not cover, the webhook
  check reads `group_vars/all/services.yml` which the wizard actually writes,
  and a crashed probe counts as an issue instead of printing "All checks
  passed".
- **The next-steps panel lists DNS, secrets, validate, doctor, provision and
  deploy, in order.** DNS guidance prints for every gateway choice, not only
  for Headscale.
- **Error hints point at commands that exist.** "bay not found" names the clone
  and `.bay/bootstrap.sh` rather than `bin/bay setup`, and the version-drift
  guard names `.bay/version.yml` and `.bay-version` instead of the pre-1.0
  `.argo` paths.
- **Docs corrections.** `docs/features.md` dropped a non-existent `admin`
  access mode and the `validate` versus `doctor` mix-up, and `README.md`
  dropped a dangling link and the hand-written `git tag` advice.
  `CONTRIBUTING.md` documents the release process.
- **Repository metadata**: GitHub issue and pull request templates, a CI badge,
  and a README note to run `make install` before `make test`.
- **`docker_registry_org`, `docker_registry_username` and
  `docker_registry_token` are no longer scaffolded.** They stay readable and
  deprecated for existing consumers, move to the `docker_registries` list in
  `registry.yml`.

### Performance

- **The connection strategy is visible.** `run_playbook` prints one
  `strategy: mitogen_linear` line, or names the linear fallback. `--profile` on
  `deploy` and `provision` turns on `profile_tasks` and the timer. See
  `docs/performance.md`.
- **Pipelining and lighter facts.** The wizard `ansible.cfg` template and the
  example gain `pipelining = True`, and `provision.yml` and `restore.yml` now
  gather a reduced fact subset like `deploy.yml` already did.
- **Image pulls batch into one task** with `xargs -P 4`, reported changed only
  on a real download, retries kept.
- **Per-container task fans collapse.** `log_archive` renders one setup script
  instead of 16 looped tasks, `database_provision` runs one idempotent SQL
  script per accessory instead of six `docker exec` calls, and the log
  retention boundary uses one batched `docker inspect` per side.
  `deploy --list-tasks` drops from 269 to 255.
- **The reconciler ships as a tar**, gated on `<stack_dir>/.reconcile/.version`,
  and reads env digests in one batched call instead of one per container. The
  canary poll interval is 1 s.
- **Remote builds push straight from buildx.** One `--push` call carries both
  tags, so the `--load` export and import and the two separate `docker push`
  steps are gone, on the Ansible path and the webhook path alike.
- **Opt-in registry layer cache.** `git_deploy_registry_cache` (default
  `false`) adds `--cache-to`/`--cache-from type=registry` against a
  `:buildcache` tag, so a builder prune no longer costs a from-scratch rebuild.
- **`bin/bay validate` caches successful probes for an hour** in
  `<bay_dir>/.validate-probe-cache`, a gitignored JSON dotfile. Only successes
  are cached, credentials in a repo URL are stripped before writing, and
  `--no-probe-cache` forces a full re-probe. A success can go stale for up to
  an hour.
- **The CLI starts faster.** `requests` and `ruamel.yaml` load inside the
  functions that use them, taking `import bay_cli.cli` from about 200 ms to
  about 80 ms, pinned by a test.
- **`make test-python` runs under `pytest-xdist`** (`-n auto --dist loadfile`),
  about 125 s down to about 29 s.

### Upgrade notes

Work through these in order on an existing consumer.

- **Run `bin/bay validate` first, before you deploy.** This release adds hard
  failures that stop the pre-deploy gate, and each one is real breakage that
  used to be silent.
- **OIDC allowlist.** If `headscale_oidc_issuer` is set, add at least one of
  `headscale_oidc_allowed_domains`, `headscale_oidc_allowed_users` or
  `headscale_oidc_allowed_groups`. Until now the tailnet accepted any account
  your issuer authenticated, so audit `bin/bay gateway nodes` for unexpected
  entries and treat it as an incident.
- **`expose: host`.** Add `expose_host_ack: true` next to every `expose: host`
  on a service port or accessory, or validate fails.
- **Admin SSH keys.** Every user in the `ssh-access` group needs a non-empty
  `keys` list. An empty list was skipped silently and left the server with no
  way in.
- **Empty secrets.** A referenced vault key with an empty value now fails.
  Generate one with `bin/bay secret` and re-encrypt with
  `bin/bay vault encrypt production`.
- **`letsencrypt_email`.** It must be set and not a placeholder. There is no
  ACME opt-out, so an empty value always meant broken SSL.
- **`config_files`.** Every entry must have a real file behind it under the
  consumer's `files/`. A service that mounts a config it never received starts
  and dies.
- **Identifier regexes.** Service, accessory, database and database-user names
  must match `[a-z0-9_-]`, and env keys must be POSIX names. Renaming a service
  is not free, it renames the container and the database, so check before you
  rename.
- **`public_routes: ["/"]` is an error.** It made a `vpn` service entirely
  public. Set `access: public` deliberately instead.
- **Uppercase database names.** SQL identifiers are quoted now, and a quoted
  identifier is case-sensitive where an unquoted one was folded to lower case.
  Compare `psql -c '\l'` and `\du` against `services.yml` before upgrading.
- **Rotate the Telegram bot token and the alert webhook URL.** Both were
  readable by any local user on every Bay host, through 0755 scripts and
  `systemctl show`. Changing the file mode does not un-leak the old value.
- **Rotate the GitHub PAT.** The token is removed from `.git/config` only on
  the next clone, so existing build checkouts still hold the old one.
- **Delete `{{ git_deploy_build_dir }}` to force a fresh clone.** The role
  re-creates it at 0700 with the `GIT_ASKPASS` helper in place.
- **`/etc/bay/alert.env` needs a run to exist.** `roles/alert_channel` is in
  both `provision.yml` and `deploy.yml`, and alerts stop working on a host that
  has not been re-run since the upgrade. The roles that own the affected
  systemd units re-render them and reload systemd themselves, so no manual
  `daemon-reload` is needed, but a deploy alone does not cover the
  provision-only units.
- **Provision-only roles need a provision run.** Use
  `bin/bay provision <env> --tags crowdsec`, `--tags nftables`,
  `--tags outbound_monitor` and `--tags docker_monitor`, or
  `bin/bay deploy --rig <env>` for the rig roles. A plain deploy does not reach
  them.
- **Purge the old CrowdSec apt key by hand.** A host provisioned before this
  release still carries it in the global `/etc/apt/trusted.gpg`, where it can
  sign any repository. Delete it from `trusted.gpg` or `trusted.gpg.d`, the new
  `signed-by` keyring does not remove it.
- **The webhook container is recreated once.** The image gains a fixed UID
  (10001) and the build directories move to group `bay-build`, GID 2000. Change
  `git_deploy_build_gid` only if 2000 collides on your host. Local tooling that
  drops a `.trigger` file as another user stops working, use `bin/bay build`.
- **One extra container recreate from the basic-auth change.** The label value
  moves from APR1 to bcrypt, so each basic-auth-protected container is
  recreated on the first deploy. Passwords are unchanged and no consumer edit
  is needed.
- **`traefik.yml` and `nftables.conf` change once**, so Traefik is recreated
  and the ruleset reloads on the first run after the upgrade. The nftables
  defaults are compatible, so nothing is blocked that was allowed before.
- **TLS 1.2 floor and loopback metrics.** Clients below TLS 1.2 are refused.
  Any external Prometheus scraping Traefik's metrics port directly breaks, set
  `traefik_metrics_bind_ip` back or scrape over the tailnet.
- **Traefik gains a dynamic-config mount and is recreated once.**
  `<stack_dir>/dynamic` is now mounted on every host, not only hosts with
  `traefik_dns_challenge_enabled`, because the TLS floor lives there. The file
  provider is enabled unconditionally for the same reason. Nothing else in the
  directory changes, so no routing changes with it.
- **The webhook receiver is recreated once** to pick up the
  `<stack_dir>/state` mount.
- **Remote builds no longer leave a local image on the build server.** The
  build pushes directly, so anything expecting to `docker run` the image there
  will not find it, and registry credentials now fail inside `buildx build`
  rather than at a later push step.
- **`git_deploy_registry_cache` is opt-in.** Setting it writes an extra
  `:buildcache` tag to the image repository, expect that tag to grow to about
  one full layer set.
- **Add `pipelining = True`** under `[ssh_connection]` in your consumer
  `ansible.cfg`. The wizard template only renders at scaffold time, so an
  existing consumer keeps the old block. If a host was hardened outside Bay
  with `requiretty`, you will see `sudo: sorry, you must have a tty`, see
  `docs/performance.md`.
- **First deploy rewrites each `.retention` file and reships the reconciler.**
  Both are expected one-time changes. A host with a hand-edited `.reconcile/`
  tree is only repaired when the marker moves, delete
  `<stack_dir>/.reconcile/.version` to force a reship.
- **Watchtower and restic.** Watchtower is pinned by digest and recreated once.
  Overriding `backup_restic_version` now also requires
  `backup_restic_checksum`, the two move together.
- **Webhook behaviour changes.** Payloads above 1 MiB get 413, repeated
  deliveries are no-ops within the cache window, `/health` returns a `services`
  integer instead of an array, and bursty fan-out may see 429.
- **The wizard gateway default flipped to none.** This affects new projects
  only. An existing consumer with Headscale configured is untouched.
- **Legacy alert keys.** Consumers setting `docker_monitor_telegram_*` keep
  working. Migrate to `alert_recipients` and delete the legacy keys in the same
  change, or alerts arrive twice.
- **Your `Makefile` is a generated file.** Re-run `bin/bay setup --force` to
  pick up the new `bay:setup` target, it backs the old one up to
  `Makefile.bak`. The old target still works.
- **Framework developers: run `uv sync`.** This bump adds `bcrypt` and
  `pytest-xdist`. Without it, `make test` fails with
  `unrecognized arguments: -n`. Consumers get both through `bin/bay install`.

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
