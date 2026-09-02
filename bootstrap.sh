#!/usr/bin/env bash
set -euo pipefail

# Bay — bootstrap a new consumer project
#
# Run this from your project directory after cloning the framework:
#
#   mkdir my-infra && cd my-infra
#   git clone https://github.com/AltanS/bay.git .bay
#   .bay/bootstrap.sh
#
# Pins the framework version, installs dependencies, and creates the
# bin/bay wrapper. Run 'bin/bay setup' afterward to scaffold the project.

# ── Helpers ──────────────────────────────────────────────────────────────

die()  { echo "Error: $*" >&2; exit 1; }
info() { echo "==> $*"; }

# ── Locate framework ────────────────────────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BAY_DIR="$(cd "$SCRIPT_DIR" && pwd)"

# Verify we're inside .bay/
if [ ! -f "$BAY_DIR/pyproject.toml" ]; then
    die "Cannot find bay framework at $BAY_DIR"
fi

PROJECT_DIR="$(dirname "$BAY_DIR")"
PROJECT_NAME="$(basename "$PROJECT_DIR")"

cd "$PROJECT_DIR"

# ── Preflight ────────────────────────────────────────────────────────────

command -v git >/dev/null 2>&1 || die "git not found"
command -v uv  >/dev/null 2>&1 || die "uv not found — install from https://docs.astral.sh/uv/getting-started/installation/"

info "Bootstrapping project '$PROJECT_NAME' in $PROJECT_DIR"

# ── Snapshot the wrapper source ─────────────────────────────────────────
# Read it BEFORE the pin below checks out an older tag. This script runs from
# the clone's HEAD, but the pin rewinds the working tree underneath it — and a
# tag that predates scripts/bin-bay-wrapper.sh would leave nothing to copy.

WRAPPER_SRC="$BAY_DIR/scripts/bin-bay-wrapper.sh"
WRAPPER_SNAPSHOT=""
if [ -f "$WRAPPER_SRC" ]; then
    WRAPPER_SNAPSHOT="$(mktemp)"
    cp "$WRAPPER_SRC" "$WRAPPER_SNAPSHOT"
    trap 'rm -f "$WRAPPER_SNAPSHOT"' EXIT
fi

# ── Pin framework version ───────────────────────────────────────────────

if [ -f .bay-version ]; then
    _ver=$(cat .bay-version | tr -d '[:space:]')
else
    _ver=$(git -C .bay tag --sort=-v:refname | head -1)
fi

if [ -n "$_ver" ]; then
    info "Pinning framework to ${_ver}..."
    git -C .bay -c advice.detachedHead=false checkout --quiet "$_ver"
    echo "$_ver" > .bay-version
fi

# ── Symlink group_vars ──────────────────────────────────────────────────

ln -sfn ../group_vars .bay/group_vars

# ── Create bin/bay wrapper ─────────────────────────────────────────────

if [ ! -f bin/bay ]; then
    mkdir -p bin
    # scripts/bin-bay-wrapper.sh is the single source of truth for the wrapper.
    # bay_cli.commands.framework._ensure_bin_wrapper copies the same file, so
    # the two writers cannot drift. Prefer the pre-pin snapshot; fall back to
    # the checked-out tag's copy for a bootstrap.sh old enough to lack one.
    if [ -n "$WRAPPER_SNAPSHOT" ] && [ -f "$WRAPPER_SNAPSHOT" ]; then
        cp "$WRAPPER_SNAPSHOT" bin/bay
    elif [ -f "$WRAPPER_SRC" ]; then
        cp "$WRAPPER_SRC" bin/bay
    else
        die "wrapper source not found at $WRAPPER_SRC — the framework checkout is incomplete"
    fi
    chmod +x bin/bay
    info "Created bin/bay wrapper"
fi

# ── Install dependencies ────────────────────────────────────────────────

info "Installing Python + Ansible dependencies..."
uv sync --project .bay

# --force on both, matching ansible.sync_deps(): without it ansible-galaxy
# skips anything already present in the *configured* paths (~/.ansible/
# collections) and never writes it into -p, leaving vendor/ silently partial.
info "Installing Galaxy roles..."
uv run --project .bay ansible-galaxy install -r .bay/requirements.yml -p .bay/vendor/roles --force

info "Installing Galaxy collections..."
uv run --project .bay ansible-galaxy collection install -r .bay/requirements.yml -p .bay/vendor/collections --force

# ── Done ────────────────────────────────────────────────────────────────

echo ""
echo "  Project '$PROJECT_NAME' is ready."
echo ""
echo "  Next step:"
echo "    bin/bay setup   — interactive setup wizard"
echo ""
