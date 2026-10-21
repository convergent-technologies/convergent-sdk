#!/usr/bin/env bash
# Usage: release-preflight.sh <package-dir> <tag-prefix> <version>
#
# Every release workflow runs this before it publishes anything. It fails
# unless the pushed tag is <tag-prefix><version>, the tagged commit is on main,
# and <package-dir>/CHANGELOG.md has a "## <version>" section. It prints that
# section, which becomes the GitHub Release notes. The checkout needs
# fetch-depth: 0 so origin/main is present.
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
printf '%s\n' "$notes"
