# Install Bay

Bay is a command you install once per machine. It is not a clone inside your project.

## What you need

- `git`
- [`uv`](https://docs.astral.sh/uv/getting-started/installation/)

## Install

```bash
git clone https://github.com/AltanS/bay ~/.local/share/bay/framework
uv tool install --editable ~/.local/share/bay/framework
```

The checkout at `~/.local/share/bay/framework` is the framework. The `bay` command runs
from it. The deploy commands also read the Ansible files in it.

The checkout also needs its Python and Ansible dependencies. The script
`bootstrap.sh` in the checkout does all of it in one go, and it is safe to run again:

```bash
git clone https://github.com/AltanS/bay ~/.local/share/bay/framework
~/.local/share/bay/framework/bootstrap.sh
```

If your shell cannot find `bay`, run `uv tool update-shell` and open a new shell.

## Update

```bash
bay self update              # newest release
bay self update --to v2.0.0  # one given tag
bay self version             # what is installed, and where
```

`bay self update` fetches the tags, checks out the tag, syncs the dependencies,
installs the command again and prints the old and the new version. It stops when
the checkout has uncommitted changes.

## Development mode: the editable install

`uv tool install --editable <checkout>` makes `bay` run the checkout live. This is the
install above. It is an editable install: the command is the code on disk, not a copy.

- `bay self version` prints the checkout path, so you see which code runs.
- A framework edit in the checkout takes effect at once. There is no tag to cut and no
  link to make.
- There is one checkout. Never edit it while a `bay up` runs: the run reads the Ansible
  roles and the Python code from disk, so a half-saved edit changes a live deploy.
- `bay self update` refuses when the checkout has local edits. Commit or stash them first.

To try a change without touching the checkout that deploys, work in a separate clone. Run
its code from inside it with `uv run bay --fleet <path> <verb>`.

## Make a fleet

A fleet is the repo that holds your boxes and the apps on them. Bay keeps each fleet at
`~/.config/bay/fleets/<name>`.

```bash
bay fleet init prod                                       # a new fleet with a minimal bay.fleet.toml
bay fleet init prod --from git@example.com:me/fleet.git   # or clone one you already have
bay fleet ls                                              # list the fleets on this machine
```

## Pick a fleet

A command that works on a fleet finds it in this order:

1. `--fleet <path>`, before the command: `bay --fleet ./my-fleet deploy production`
2. `BAY_FLEET=<path>` in the environment.
3. The fleet that the `bay.toml` of the app repo you are in names (`fleet = "prod"`).
4. `BAY_FLEET_NAME=<name>`, a fleet in `~/.config/bay/fleets`.

With none of these, the command stops and lists the ways to pick one.

`--fleet <path>` is also how you try a fleet that is not under `~/.config/bay/fleets`.
It works from any directory.

## Where Bay reads an app repo

A lock (`projects/<name>/bay.lock`) names the app repo by its clone URL (`repo`),
never by a path on a machine. When Bay needs the repo, it looks in this order:

1. **The checkout you stand in.** When the git checkout of the current directory has
   an `origin` that is the lock's `repo`, Bay reads it. URLs are compared in one form,
   so `git@github.com:acme/app.git` and `https://github.com/acme/app` match.
2. **The repo cache of the fleet.** `<fleet>/.bay-cache/repos/<slug>` is a mirror
   clone of the repo. The slug comes from the URL, so the same URL gives the same
   directory on every machine, and two projects in one repo share one cache. Bay clones
   it the first time and fetches it (`--prune`) before each plan. You need read access
   to the repo from this machine.

Bay only reads both: it never writes to your checkout. A pinned commit that is in
neither place is an error that names the project.

**Push first.** `bay up` refuses a commit that is on no branch of the remote. The box
builds from the remote, so it cannot run a commit that only your machine has. `bay plan`
says so in a note. `bay up` deploys the whole box environment, not one project. Other projects deploy at their own pin.

## Caches

Bay keeps what it learns about a fleet in `<fleet>/.bay-cache/`. This holds the
rig-state cache (`.rig-state-cache`), the probe cache of `bay validate`
(`.validate-probe-cache`) and the repo cache (`repos/`). The two cache files expire
after one hour. Two fleets on one machine never share a cache. `bay fleet init` adds
`.bay-cache/` to the `.gitignore` of a new fleet, and the directory also holds its own
`.gitignore` of `*`, so git never shows it. Delete the directory at any time. Bay
builds it again; the repo cache is cloned again on the next plan.

## Check a machine and a fleet: bay doctor

`bay doctor [env]` answers the questions to ask before a deploy. It prints one
line per check, each `ok`, `warn` or `fail`, and exits 1 when a check fails:

| Check | What it says |
|---|---|
| Fleet | The fleet Bay picked, its real path (a fleet may be a symlink), and why: `--fleet`, `BAY_FLEET`, the `fleet` line of a `bay.toml`, `BAY_FLEET_NAME`, or the fleet directory you stand in. |
| Fleet format | The `format` of `bay.fleet.toml`. Format 1 is a warning; a format newer than the CLI knows fails. |
| CLI | The installed version and the checkout it runs from, as `bay self version`. |
| Vault | Whether the secrets of the environment open with `.vault_pass`. No name and no value is printed. |
| Box | Whether each box answers. Bay reads its receipt the way `bay status` does, with a short timeout. |
| Repo | Whether each app repo's pinned commit can be read: in the checkout you stand in, in the repo cache, else after one fetch that never asks for a password. |
| Fleet clone | Whether the fleet clone is behind its remote. Pull first: a stale clone reverts config on the next deploy. |
| v1 leftovers | The `bin/` folder, the old copy of Bay and the old pin file of a Bay 1 fleet. Bay 2 does not use them; remove them. |
| Plans | Plan files in `plans/` that are not committed. `bay plan` saves every plan, `bay up` commits only the one it applies. |

Then come the older checks: the inventory, SSH, DNS, the gateway and the
webhook. `--no-remote` asks no box and fetches no app repo. `--json` prints the
lines as one document: `{"doctor_version": 1, "ok", "env", "fleet", "lines":
[{"check", "status", "detail"}]}`.

## Uninstall

```bash
uv tool uninstall bay
rm -rf ~/.local/share/bay
```

Fleets under `~/.config/bay/fleets` stay. They are your data.
