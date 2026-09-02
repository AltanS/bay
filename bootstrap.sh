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

# KEEP IN SYNC with _is_bay_boilerplate_wrapper() in
# src/bay_cli/commands/framework.py. Both writers must classify an existing
# bin/bay identically, or a consumer gets a different answer from 'bin/bay
# setup' than from bootstrap.sh.
#
# The marker line matched below must NEVER be removed from
# scripts/bin-bay-wrapper.sh: it is the only thing that identifies a wrapper as
# Bay's own, and without it every shipped wrapper becomes un-updatable.
# Both patterns also match the pre-1.0 wrapper, so a consumer that still has
# one gets the same answer.
_WRAPPER_MARKER_RE='^#[[:space:]]*(Bay|Argo) CLI wrapper([^[:alnum:]_]|$)'  # legacy-argo
_WRAPPER_ALLOWED_RE='^(#!/usr/bin/env bash|set -euo pipefail|SCRIPT_DIR="\$\(cd "\$\(dirname "\$\{BASH_SOURCE\[0\]\}"\)/\.\." && pwd\)"|if \[ ! -d "\$\{SCRIPT_DIR\}/\.(bay|argo)" \]; then|echo .*>&2|exit 1|fi|unset VIRTUAL_ENV( 2>/dev/null)?|exec uv run --project "\$\{SCRIPT_DIR\}/\.(bay|argo)" (bay|argo) "\$@")$'  # legacy-argo

wrapper_is_boilerplate() {
    _f="$1"
    # (a) Marker: one of the first 3 lines is the shipped header.
    head -3 "$_f" | grep -qE "$_WRAPPER_MARKER_RE" || return 1
    # (b) No stray code: drop blank lines and comments (but keep the shebang,
    # which is code), then anything left that Bay never shipped is hand-added.
    _stray="$(sed -e 's/^[[:space:]]*//' "$_f" \
        | grep -vE '^$' \
        | grep -vE '^#([^!]|$)' \
        | grep -vE "$_WRAPPER_ALLOWED_RE" \
        || true)"
    [ -z "$_stray" ]
}

# scripts/bin-bay-wrapper.sh is the single source of truth for the wrapper.
# bay_cli.commands.framework._ensure_bin_wrapper copies the same file, so the
# two writers cannot drift. Prefer the pre-pin snapshot; fall back to the
# checked-out tag's copy for a bootstrap.sh old enough to lack one.
write_wrapper() {
    mkdir -p bin
    if [ -n "$WRAPPER_SNAPSHOT" ] && [ -f "$WRAPPER_SNAPSHOT" ]; then
        cp "$WRAPPER_SNAPSHOT" bin/bay
    elif [ -f "$WRAPPER_SRC" ]; then
        cp "$WRAPPER_SRC" bin/bay
    else
        die "wrapper source not found at $WRAPPER_SRC — the framework checkout is incomplete"
    fi
    chmod +x bin/bay
}

# Same source resolution as write_wrapper, for the byte-comparison below.
WRAPPER_FROM=""
if [ -n "$WRAPPER_SNAPSHOT" ] && [ -f "$WRAPPER_SNAPSHOT" ]; then
    WRAPPER_FROM="$WRAPPER_SNAPSHOT"
elif [ -f "$WRAPPER_SRC" ]; then
    WRAPPER_FROM="$WRAPPER_SRC"
fi

if [ ! -f bin/bay ]; then
    write_wrapper
    info "Created bin/bay wrapper"
elif [ -n "$WRAPPER_FROM" ] && cmp -s "$WRAPPER_FROM" bin/bay; then
    :
elif wrapper_is_boilerplate bin/bay; then
    write_wrapper
    info "Updated bin/bay wrapper"
else
    info "bin/bay looks hand-edited, leaving it alone (shipped wrapper: .bay/scripts/bin-bay-wrapper.sh)"
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
