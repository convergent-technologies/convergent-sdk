#!/usr/bin/env bash
# Usage: release-preflight.sh <package-dir> <tag-prefix> <version>
#
# Every release workflow runs this before it publishes anything. It fails
# unless the pushed tag is <tag-prefix><version>, the tagged commit is on main,
# <package-dir>/CHANGELOG.md has a "## <version>" section, and the generated
# telemetry keys files under <package-dir> record exactly one REGISTRY_SHA256. It prints that
# section and a line naming the registry sha256, which become the GitHub Release
# notes. The checkout needs fetch-depth: 0 so origin/main is present.
set -euo pipefail

dir=$1
prefix=$2
version=$3

if [ "$GITHUB_REF_NAME" != "$prefix$version" ]; then
  echo "tag $GITHUB_REF_NAME does not name $dir version $version" >&2
  exit 1
fi
if ! git merge-base --is-ancestor "$GITHUB_SHA" origin/main; then
  echo "tag $GITHUB_REF_NAME is not on main" >&2
  exit 1
fi
notes=$(awk -v ver="$version" '
  $0 == "## " ver { printing = 1; next }
  printing && /^## / { exit }
  printing { print }
' "$dir/CHANGELOG.md")
if ! grep -q '[^[:space:]]' <<<"$notes"; then
  echo "$dir/CHANGELOG.md has no '## $version' section" >&2
  exit 1
fi
revisions=$(grep -rhoE --include=_telemetry_keys.py --include=telemetry-keys.ts \
  'REGISTRY_SHA256 = "[0-9a-f]{64}"' "$dir" | sort -u || true)
if [ "$(grep -c . <<<"$revisions")" != 1 ]; then
  echo "$dir has no generated telemetry keys recording exactly one REGISTRY_SHA256; found:" >&2
  printf '%s\n' "${revisions:-(none)}" >&2
  exit 1
fi
printf '%s\n\nTelemetry registry: sha256 `%s`\n' "$notes" "$(grep -oE '[0-9a-f]{64}' <<<"$revisions")"
