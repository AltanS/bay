# Contributing to Bay

Thanks for your interest in contributing. This document covers how to set up a
development environment, the checks your change needs to pass, and the
conventions the codebase follows.

## Code of Conduct

This project follows the [Contributor Covenant](CODE_OF_CONDUCT.md). By
participating, you are expected to uphold it.

## License

Bay is licensed under the [MIT License](LICENSE). By submitting a
contribution, you agree that it is accepted and distributed under the same
license.

## Setting up a development environment

Bay is a Python + Ansible project managed with [uv](https://docs.astral.sh/uv/).

1. Install uv if you don't already have it:

   ```bash
   curl -LsSf https://astral.sh/uv/install.sh | sh
   ```

2. Clone the repo and install dependencies (Python deps via `uv sync`, plus
   Galaxy roles/collections):

   ```bash
   git clone git@github.com:AltanS/bay.git
   cd bay
   uv sync
   make install
   ```

   `make install` also wires up git hooks (`core.hooksPath = .githooks`), so
   do this once per clone even if you already ran `uv sync` manually.

## Running tests and lint

```bash
make test         # Full test suite: framework, bootstrap, and Python tests
make test-python   # Python unit tests only (pytest)
make test-framework # Playbook syntax, role structure, ansible-lint, YAML validity
make test-bootstrap # End-to-end bootstrap test against a temp consumer project
make lint          # mypy + ruff (typecheck) and ansible-lint
make typecheck      # mypy + ruff only
```

Run `make test` and `make lint` before opening a pull request. CI runs the
same targets and will not merge a red build.

## Commit convention

Commits follow [Conventional Commits](https://www.conventionalcommits.org/):

- `feat:` — a new feature
- `fix:` — a bug fix
- `chore:` — maintenance work with no user-facing behavior change
- `refactor:` — code change that neither fixes a bug nor adds a feature
- `docs:` — documentation only

Keep the summary line short and imperative (e.g. `fix: correct traefik label
for vpn-only services`).

## Ansible conventions

The Ansible roles and playbooks in this repo follow a consistent style:

- Use fully qualified collection names (FQCN) for modules, e.g.
  `ansible.builtin.apt`, never bare `apt`.
- Role names and variable names are `lowercase_underscore`
  (e.g. `deploy_stack`, `docker_users`).
- Every task has a `name` field.
- YAML files use the `.yml` extension (not `.yaml`) and start with `---`.
- Indentation is 2 spaces.
- Templates use `.j2` and are rendered with Jinja2.

`make lint` enforces most of this via `ansible-lint`; please run it locally
before pushing.

## Opening an issue

Bug reports and feature requests are welcome via GitHub issues. Please
include:

- What you expected to happen and what happened instead
- Steps to reproduce (Bay version, consumer config shape if relevant)
- Relevant logs or error output

Do not include real credentials, IP addresses, hostnames, or other
identifying infrastructure details in an issue — use placeholder values
(e.g. RFC 5737 addresses like `203.0.113.0/24`, or `example.com`).

If you have found a security vulnerability, do not open a public issue — see
[SECURITY.md](SECURITY.md) instead.

## Opening a pull request

1. Fork the repo and create a branch off `main`.
2. Make your change, following the conventions above.
3. Add or update tests as appropriate.
4. Run `make test` and `make lint` locally.
5. Open a pull request describing the change and why it's needed.

## CI must be green before merge

Every pull request runs through CI (`.github/workflows/ci.yml`), which must
pass before merge:

- **Tests** — `make test` (Python tests, framework tests, bootstrap
  end-to-end test)
- **Lint & typecheck** — `make typecheck` (mypy + ruff) and `ansible-lint`
- **Identity leak scan** — a guard that fails the build if real operator
  IPs, consumer names, or private hostnames are reintroduced into the
  framework repo. Use RFC 5737 addresses and `example.com` in any docs or
  test fixtures you add.

A failing job in any of these blocks merge — please fix the underlying issue
rather than skip or silence the check.

## Cutting a release (maintainers only)

Consumers pin to git tags via `.bay-version`, so an untagged commit is
invisible to them. Releases go out through one command:

1. Land your change on `main`.
2. Add a `## [X.Y.Z] — <date>` section to `CHANGELOG.md` and commit it.
   `make release` refuses to tag a version that has no entry — the changelog
   is how a consumer learns what a version bump brings. Add an **Upgrade
   notes** subsection for anything manual.
3. Run:

   ```bash
   make release VERSION=X.Y.Z
   ```

   Patch for fixes and docs, minor for features, major for breaking changes.

`make release` bumps `version.yml`, commits, tags and pushes in one step.
Never run `git tag` or `git push` by hand for a release: `version.yml` would
drift from the tags, which breaks the framework's minimum-version checks.
