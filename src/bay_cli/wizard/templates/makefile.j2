BAY_DIR := .bay
BAY_REPO := https://github.com/AltanS/bay.git
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
# Only the clone lives here — the framework isn't on disk yet when this runs.
# Everything after it is $(BAY_DIR)/bootstrap.sh, which is the one entry path.
# Override the source with: make bay:setup BAY_REPO=git@github.com:AltanS/bay.git

bay\:setup:
	@if [ -d "$(BAY_DIR)" ]; then \
		echo "Already initialized. Use 'bin/bay install' to sync or 'bin/bay update' to bump version."; \
		exit 0; \
	fi; \
	command -v git >/dev/null 2>&1 || { echo "git not found"; exit 1; }; \
	echo "Cloning bay framework into $(BAY_DIR)/..."; \
	git clone $(BAY_REPO) $(BAY_DIR) || exit 1; \
	exec $(BAY_DIR)/bootstrap.sh

.PHONY: _help bay\:setup
