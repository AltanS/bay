BAY_DIR := .bay
BAY_REPO := git@github.com:AltanS/bay.git
ARGS ?=

# Load framework targets (available after bay:setup)
-include $(BAY_DIR)/bay.mk

# Default target: show help if framework loaded, otherwise prompt to setup
.DEFAULT_GOAL := _help
ifeq ($(wildcard $(BAY_DIR)/bay.mk),)
_help:
	@echo ""
	@echo "  Bay framework not initialized."
	@echo "  Run: make bay:setup"
	@echo ""
else
_help: bay\:help
endif

# ── Bootstrap ────────────────────────────────────────────────────────────
# Must live in consumer — the framework isn't available yet when this runs

bay\:setup:
	@if [ -d "$(BAY_DIR)" ]; then \
		echo "Already initialized. Use 'make bay:install' to sync or 'make bay:update' to bump version."; \
		exit 0; \
	fi; \
	command -v uv >/dev/null 2>&1 || { echo "uv not found — install from https://docs.astral.sh/uv/getting-started/installation/"; exit 1; }; \
	echo "Cloning bay framework into $(BAY_DIR)/..."; \
	git clone $(BAY_REPO) $(BAY_DIR); \
	if [ -f .bay-version ]; then \
		_ver=$$(cat .bay-version | tr -d '[:space:]'); \
	else \
		_ver=$$(git -C $(BAY_DIR) tag --sort=-v:refname | head -1); \
		if [ -n "$${_ver}" ]; then \
			echo "No .bay-version found, using latest release: $${_ver}"; \
			echo "$${_ver}" > .bay-version; \
			echo "Created .bay-version: $${_ver}"; \
		fi; \
	fi; \
	if [ -n "$${_ver}" ]; then \
		echo "Pinning framework to $${_ver}..."; \
		git -C $(BAY_DIR) -c advice.detachedHead=false checkout "$${_ver}"; \
	fi; \
	ln -sfn ../group_vars $(BAY_DIR)/group_vars; \
	echo "Installing Python + Ansible dependencies..."; \
	uv sync --project $(BAY_DIR); \
	echo "Installing Galaxy roles..."; \
	uv run --project $(BAY_DIR) ansible-galaxy install -r $(BAY_DIR)/requirements.yml -p $(BAY_DIR)/vendor/roles --force; \
	echo "Installing Galaxy collections..."; \
	uv run --project $(BAY_DIR) ansible-galaxy collection install -r $(BAY_DIR)/requirements.yml -p $(BAY_DIR)/vendor/collections --force; \
	echo ""; \
	echo "Setup complete. Run 'make' to see available commands."

.PHONY: _help bay\:setup
