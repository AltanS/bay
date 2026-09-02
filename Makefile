REQUIRED_BINS := uv
$(foreach bin,$(REQUIRED_BINS),$(if $(shell command -v $(bin) 2>/dev/null),,$(error '$(bin)' not found — install from https://docs.astral.sh/uv/getting-started/installation/)))

install: hooks
	uv run ansible-galaxy install -r requirements.yml -p vendor/roles --force
	uv run ansible-galaxy collection install -r requirements.yml -p vendor/collections --force

lint: typecheck
	uv run ansible-lint

typecheck:
	uv run mypy
	uv run ruff check src/bay_reconcile tests/test_reconcile_*.py

test: test-framework test-bootstrap test-python

test-framework:
	bash tests/test_framework.sh

test-bootstrap:
	bash tests/test_bootstrap.sh

# Two passes. The first is parallel (`--dist loadfile` keeps each file on one
# worker, so module-scoped fixtures are built once). The second re-runs the
# `serial`-marked wall-clock tests, which skip themselves inside an xdist
# worker because N-way CPU contention makes a timing assertion meaningless.
test-python:
	uv run pytest tests/ -n auto --dist loadfile -q
	uv run pytest tests/ -q -m serial -p no:xdist

docs-alerts:
	uv run python scripts/gen_alert_docs.py

docs-skill:
	uv run python scripts/gen_skill.py

docs: docs-alerts docs-skill

# Point git at .githooks/ so the SKILL.md pre-commit hook runs. Per-clone
# config, so every checkout needs it once; `make install` does it for you.
hooks:
	git config core.hooksPath .githooks
	@echo "git hooks enabled (core.hooksPath=.githooks)"

release:
ifndef VERSION
	$(error VERSION is required — usage: make release VERSION=0.38.1)
endif
	bash scripts/release.sh $(VERSION)

.PHONY: install hooks lint typecheck test test-framework test-bootstrap test-python docs docs-alerts docs-skill release
