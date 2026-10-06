#!/usr/bin/env bash
# Tests bootstrap.sh, the machine-level installer, end to end:
#   1. Installs from a local clone of this repo into throwaway directories.
#   2. Checks that `bay` runs and that `bay fleet init` and `bay fleet ls` work.
#   3. Runs the installer a second time (it must be safe to repeat).
#
# Usage: bash tests/test_bootstrap.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BAY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
TMPDIR=""

PASS=0
FAIL=0
ERRORS=()

pass() { PASS=$((PASS + 1)); printf "  \033[32m✓\033[0m %s\n" "$1"; }
fail() { FAIL=$((FAIL + 1)); ERRORS+=("$1"); printf "  \033[31m✗\033[0m %s\n" "$1"; }

section() { printf "\n\033[1m%s\033[0m\n" "$1"; }

cleanup() {
  if [[ -n "$TMPDIR" && -d "$TMPDIR" ]]; then
    rm -rf "$TMPDIR"
  fi
}
trap cleanup EXIT

TMPDIR=$(mktemp -d)
FRAMEWORK="$TMPDIR/share/bay/framework"
export BAY_REPO="$BAY_DIR"
export BAY_FRAMEWORK_DIR="$FRAMEWORK"
# Keep uv's tool install away from the real machine.
export UV_TOOL_DIR="$TMPDIR/uv-tools"
export UV_TOOL_BIN_DIR="$TMPDIR/bin"
export HOME="$TMPDIR/home"
mkdir -p "$HOME"
unset BAY_FLEET BAY_FLEET_NAME

section "Install"

printf "  Using bay repo:  %s\n" "$BAY_DIR"
printf "  Install into:    %s\n" "$FRAMEWORK"

# Clone first, then overlay the working tree, so uncommitted changes are what
# gets installed (the installer then finds the clone and only fetches).
mkdir -p "$(dirname "$FRAMEWORK")"
git clone --quiet --no-tags "$BAY_DIR" "$FRAMEWORK"
tar -C "$BAY_DIR" -cf - \
  --exclude=./.git \
  --exclude=./.venv \
  --exclude=./vendor \
  --exclude=./group_vars \
  --exclude=./.tracker \
  --exclude='*/__pycache__' \
  --exclude=./.pytest_cache \
  --exclude=./.mypy_cache \
  --exclude=./.ruff_cache \
  --exclude=./.ansible \
  . | tar -C "$FRAMEWORK" -xf -

if bash "$FRAMEWORK/bootstrap.sh" 2>&1; then
  pass "bootstrap.sh completed"
else
  fail "bootstrap.sh exited with an error"
  exit 1
fi

BAY="$UV_TOOL_BIN_DIR/bay"
if [[ -x "$BAY" ]]; then
  pass "the bay command is installed"
else
  fail "the bay command is missing at $BAY"
  exit 1
fi

section "The installed command"

if "$BAY" --version >/dev/null 2>&1; then
  pass "bay --version"
else
  fail "bay --version failed"
fi

if "$BAY" self version 2>&1 | grep -q "checkout: $FRAMEWORK"; then
  pass "bay self version names the checkout"
else
  fail "bay self version does not name $FRAMEWORK"
fi

if "$BAY" fleet init demo >/dev/null 2>&1 && [[ -f "$HOME/.config/bay/fleets/demo/bay.fleet.toml" ]]; then
  pass "bay fleet init demo wrote bay.fleet.toml"
else
  fail "bay fleet init demo did not create the fleet"
fi

if "$BAY" fleet ls 2>&1 | grep -q "demo"; then
  pass "bay fleet ls lists demo"
else
  fail "bay fleet ls does not list demo"
fi

if BAY_FLEET_NAME=demo "$BAY" compile >/dev/null 2>&1; then
  pass "bay compile runs against the new fleet"
else
  fail "bay compile failed against the new fleet"
fi

section "Second run"

if bash "$FRAMEWORK/bootstrap.sh" >/dev/null 2>&1; then
  pass "bootstrap.sh is safe to run again"
else
  fail "bootstrap.sh failed on the second run"
fi

# ── Summary ──────────────────────────────────────────────────────────────
printf "\n\033[1mResults: %d passed, %d failed\033[0m\n" "$PASS" "$FAIL"
if [[ $FAIL -gt 0 ]]; then
  printf "\nFailures:\n"
  for e in "${ERRORS[@]}"; do
    printf "  - %s\n" "$e"
  done
  exit 1
fi
