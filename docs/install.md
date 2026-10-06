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
the checkout has uncommitted changes. If you develop Bay itself, work in a separate
clone and point a fleet at it with `bay --fleet <path>`.

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

## Uninstall

```bash
uv tool uninstall bay
rm -rf ~/.local/share/bay
```

Fleets under `~/.config/bay/fleets` stay. They are your data.
