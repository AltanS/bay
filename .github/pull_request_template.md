## What changed

## Why

What problem does this solve? Link the issue if there is one.

## Checklist

- [ ] `make test` passes
- [ ] `make lint` passes
- [ ] `CHANGELOG.md` has an entry for this change
- [ ] Docs updated (`README.md`, `docs/`, role `defaults/main.yml` header comments)
- [ ] No real domains, IP addresses or credentials in the diff (`bash scripts/leak-scan.sh`)

## Release note

Framework releases go through `make release VERSION=X.Y.Z`. It bumps
`version.yml`, commits, tags and pushes in one step. Never tag or push a
release by hand — consumers pin to tags, and a hand-written tag leaves
`version.yml` behind.
