#!/usr/bin/env bash
set -euo pipefail

# Print the CHANGELOG.md section for one version, without its heading.
# Usage: scripts/release-notes.sh 2.6.0 [CHANGELOG.md]
#
# release.sh feeds this to `gh release create` so the GitHub Release page
# carries the same text as the changelog. The section runs from its
# `## [X.Y.Z]` heading to the next `## ` heading.

VERSION="${1:-}"
CHANGELOG="${2:-CHANGELOG.md}"
VERSION="${VERSION#v}"

if ! [[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "usage: scripts/release-notes.sh X.Y.Z [CHANGELOG.md]" >&2
  exit 1
fi

NOTES=$(awk -v v="[${VERSION}]" '
  /^## / { if (found) exit; if (index($0, "## " v) == 1) { found = 1; next } }
  found { print }
' "$CHANGELOG")

# Drop leading and trailing blank lines.
NOTES=$(printf '%s\n' "$NOTES" | sed -e '/./,$!d' | sed -e ':a' -e '/^\n*$/{$d;N;ba' -e '}')

if [[ -z "$NOTES" ]]; then
  echo "error: CHANGELOG.md has no entry for ${VERSION}" >&2
  exit 1
fi

printf '%s\n' "$NOTES"
