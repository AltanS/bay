# Deploy performance

A deploy is ~265 Ansible tasks against one host. Almost all of the wall clock is
connection overhead and image pulls, not work on the server. This page covers the
three levers Bay ships — Mitogen, SSH pipelining, and `--profile` — and how to read
a slow deploy.

## The strategy line

Every `bin/bay deploy` and `bin/bay provision` prints one line before the playbook
starts:

```
  i strategy: mitogen_linear
```

or

```
  i strategy: linear (mitogen unavailable)
```

That line is the only signal you get. Mitogen prints nothing when it is active, so
without it a degraded run looks exactly like a healthy one — just slower.

## Mitogen

[Mitogen](https://mitogen.networkgenomics.com/ansible_detailed.html) replaces
Ansible's default connection strategy. Stock Ansible copies a Python module to the
target, runs it, and tears the interpreter down — for every task. Mitogen keeps one
persistent remote interpreter and calls into it, which removes most of the per-task
cost on a run with hundreds of small tasks.

In Bay it is a **hard dependency** (`mitogen` in `pyproject.toml`), not an optional
extra. `bin/bay` enables it automatically: the CLI looks for the strategy plugin in
the framework venv and, when it is there, sets `ANSIBLE_STRATEGY=mitogen_linear`
and `ANSIBLE_STRATEGY_PLUGINS` for the playbook run. You do not configure it in
`ansible.cfg`.

If the line says `linear (mitogen unavailable)`, one of two things is true:

1. `BAY_NO_MITOGEN=1` is set (see below), or
2. the framework venv is stale or was purged. Fix it with `bin/bay install`
   (or `make install` in the framework repo) and re-run.

### `BAY_NO_MITOGEN=1`

Set this to fall back to Ansible's stock `linear` strategy for one run:

```bash
BAY_NO_MITOGEN=1 bin/bay deploy production
```

Use it when you suspect Mitogen itself, not your playbook: unexplained
`become` / `become_user` failures, a task that hangs only under Bay, tracebacks
naming `ansible_mitogen`, or a module that misbehaves with a long-lived
interpreter. If the run is clean without Mitogen and broken with it, that is the
bug report. It is a debugging switch, not a setting — leaving it on costs you the
speedup on every deploy.

## SSH pipelining

```ini
[ssh_connection]
pipelining = True
```

Pipelining removes one SSH round trip per task: the module is fed to the remote
interpreter over the existing connection instead of being written to a temporary
file first. Across ~265 tasks that is hundreds of round trips.

The framework's own `ansible.cfg` sets it, and the setup wizard now writes it into
the consumer `ansible.cfg` it generates. **Consumers created before this was added
must add the line by hand** — the wizard template is only rendered at scaffold time,
so an existing consumer keeps its old `[ssh_connection]` block forever.

One requirement: `requiretty` must be off in the target's sudoers. Bay's own
provisioning never sets it, but a host hardened outside Bay might. The symptom is
unmistakable:

```
sudo: sorry, you must have a tty to run sudo
```

Fix it on the target with `visudo` — remove or negate the line:

```
Defaults    !requiretty
```

## Measuring: `--profile`

```bash
bin/bay deploy --profile production
bin/bay provision --profile production
```

`--profile` turns on two vendored `ansible.posix` callbacks for that run:
`profile_tasks` (per-task timings plus a slowest-tasks table at the end) and
`timer` (total playbook runtime). It is additive and off by default — nothing about
the deploy changes, you just get numbers.

Each task prints its own elapsed and cumulative time as it runs:

```
Saturday 01 September 2026  14:02:11 +0200 (0:00:03.412)  0:01:22.918 ****
```

and the run ends with the summary that actually matters:

```
===============================================================================
build_image : Pull images ---------------------------------------------- 12.44s
deploy_stack : Reconcile containers ------------------------------------- 9.10s
git_deploy : Clone repositories ----------------------------------------- 4.02s
...
Playbook run took 0 days, 0 hours, 2 minutes, 4 seconds
```

Read it top-down. The first entry is where to spend effort; anything under a second
is noise. Compare two runs of the same deploy rather than trusting one number — the
first deploy after a base-image change pulls layers and is not representative.

Note that `--profile` must be placed before the environment argument, like every
other flag (`bin/bay deploy --profile production`). Placed after it, it is rescued
with a warning rather than forwarded to `ansible-playbook`.

## What Bay already does

- **One parallel image pull.** All service and accessory images are pulled by a
  single task with `xargs -P 4`, and the task reports `changed` only when something
  was actually downloaded.
- **Narrow fact gathering.** Every play uses `gather_subset: ["!all"]`, which still
  collects the `min` set (hostname, distribution, `date_time`) but skips hardware,
  network interface and virtualisation discovery. No role reads those.
- **A server-side reconciler.** Container convergence is one Python pass on the
  host, not an Ansible loop per container. See [reconciler.md](reconciler.md).

## Validate's probe cache

Every deploy runs `bin/bay validate` first (unless you pass `--skip-validate`), and
validate reaches the network twice: `git ls-remote` for each build-from-source
service, and `skopeo inspect` for each image service. On a consumer with a handful
of services that is 2-5 seconds of the same answers, every single run.

Successful probes are cached in `<bay_dir>/.validate-probe-cache`, a JSON dotfile
next to `.rig-state-cache`, with a **1 hour TTL** — the same TTL as the rig-state
cache. A hit skips the subprocess entirely; a miss probes and records. The file is
written `0600` and is gitignored.

Three properties are worth knowing:

- **Only successes are cached.** An unreachable repo, a missing branch or an
  unverified image is re-probed on every run, so a fix shows up immediately. A
  green validate is the thing operators act on, so it is never served from a
  cached failure.
- **A success can go stale for up to an hour.** A repo or image that was reachable
  and has since disappeared still validates green until the entry expires. This is
  the one trade the cache makes.
- **Credentials are stripped before anything is written.** Build repo URLs
  routinely carry a token (`https://x-access-token:<pat>@github.com/...`); the
  userinfo is removed before the URL is used as a cache key, so a rotated token is
  also not a cache miss.

Force a full re-probe with:

```bash
bin/bay validate --no-probe-cache
```

which re-runs every probe and refreshes the cache entries. Deleting the file has
the same effect. The cache records only that a reference resolved — it never
records or hands off an image digest.

## CLI start-up

`bay_cli.cli` imports every command module eagerly, so a module-level import
anywhere under `src/bay_cli/` is paid for by every invocation, `bay --help`
included. The two heavyweights, `requests` (~60 ms) and `ruamel.yaml` (~12 ms),
are imported inside their call sites instead; that took `import bay_cli.cli` from
~200 ms to ~80 ms on the dev box.

`tests/test_cli_import_time.py` guards it from both ends: `requests` and
`ruamel.yaml` must be absent from `sys.modules` after importing the CLI (exact,
never skipped), and cumulative import time must stay under 100 ms (machine
dependent — skipped when `BAY_SKIP_PERF_ASSERTS` is set, and inside an xdist
worker). If you add a heavy import to a module on the CLI path, move it into the
function that needs it.
