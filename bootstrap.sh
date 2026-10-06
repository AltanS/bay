#!/usr/bin/env bash
set -euo pipefail

# Bay: install the `bay` command on this machine.
#
#   git clone https://github.com/AltanS/bay ~/.local/share/bay/framework
#   ~/.local/share/bay/framework/bootstrap.sh
#
# Or, from nothing, run this file with curl:
#
#   curl -fsSL https://raw.githubusercontent.com/AltanS/bay/main/bootstrap.sh | bash
#
# What it does (it is safe to run again):
#   1. Clones Bay into $BAY_FRAMEWORK_DIR if it is not there yet. A checkout that
#      exists is fetched, never reset.
#   2. Syncs the Python and Ansible dependencies inside that checkout.
#   3. Runs `uv tool install --editable` on the checkout, which puts `bay` on
#      your PATH.
#
# Later, `bay self update` moves to a newer release. See docs/install.md.
#
# Variables:
#   BAY_REPO           Where to clone from. Default: https://github.com/AltanS/bay.git
#   BAY_FRAMEWORK_DIR  Where the checkout lives. Default: ~/.local/share/bay/framework

die()  { echo "Error: $*" >&2; exit 1; }
info() { echo "==> $*"; }

BAY_REPO="${BAY_REPO:-https://github.com/AltanS/bay.git}"
BAY_FRAMEWORK_DIR="${BAY_FRAMEWORK_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/bay/framework}"

command -v git >/dev/null 2>&1 || die "git not found"
command -v uv  >/dev/null 2>&1 || die "uv not found. Install it from https://docs.astral.sh/uv/getting-started/installation/"

# ── Checkout ─────────────────────────────────────────────────────────────

if [ -d "$BAY_FRAMEWORK_DIR/.git" ]; then
    info "Bay is already cloned at $BAY_FRAMEWORK_DIR, fetching tags"
    git -C "$BAY_FRAMEWORK_DIR" fetch --tags --prune --quiet
elif [ -e "$BAY_FRAMEWORK_DIR" ]; then
    die "$BAY_FRAMEWORK_DIR exists and is not a Bay checkout"
else
    info "Cloning Bay into $BAY_FRAMEWORK_DIR"
    mkdir -p "$(dirname "$BAY_FRAMEWORK_DIR")"
    git clone --quiet "$BAY_REPO" "$BAY_FRAMEWORK_DIR"
fi

# ── Dependencies ─────────────────────────────────────────────────────────

unset VIRTUAL_ENV 2>/dev/null || true

info "Installing Python and Ansible dependencies"
uv sync --project "$BAY_FRAMEWORK_DIR"

# --force on both, so a stale copy in ~/.ansible never hides a missing file in vendor/.
info "Installing Galaxy roles"
uv run --project "$BAY_FRAMEWORK_DIR" ansible-galaxy install \
    -r "$BAY_FRAMEWORK_DIR/requirements.yml" -p "$BAY_FRAMEWORK_DIR/vendor/roles" --force

info "Installing Galaxy collections"
uv run --project "$BAY_FRAMEWORK_DIR" ansible-galaxy collection install \
    -r "$BAY_FRAMEWORK_DIR/requirements.yml" -p "$BAY_FRAMEWORK_DIR/vendor/collections" --force

# ── The command ──────────────────────────────────────────────────────────

info "Installing the bay command"
uv tool install --editable --force "$BAY_FRAMEWORK_DIR"

echo ""
echo "  Bay is installed."
echo ""
echo "  If 'bay' is not found, run:  uv tool update-shell"
echo "  Next:                        bay fleet init <name>"
echo ""
