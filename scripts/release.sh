#!/usr/bin/env bash
# Cut a release: bump version, require a CHANGELOG entry, commit, tag.
# Usage: scripts/release.sh X.Y.Z
set -euo pipefail
cd "$(dirname "$0")/.."

v="${1:?usage: $0 X.Y.Z}"
[[ $v =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "version must be X.Y.Z" >&2; exit 1; }
[[ $(git branch --show-current) == main ]] || { echo "release from main" >&2; exit 1; }
if git status --porcelain | grep -v ' CHANGELOG.md$' | grep -q .; then
  echo "commit other changes first (only CHANGELOG.md may be dirty)" >&2; exit 1
fi
grep -q "^## \[$v\]" CHANGELOG.md || { echo "add a '## [$v] - $(date +%F)' section to CHANGELOG.md" >&2; exit 1; }

sed -i.bak -E "s/^version = \".*\"/version = \"$v\"/" pyproject.toml && rm pyproject.toml.bak
scripts/dev.sh lock
scripts/dev.sh test -q

git add pyproject.toml uv.lock CHANGELOG.md
git commit -m "Release v$v"
git tag -a "v$v" -m "v$v"

cat <<EOF
Tagged v$v. Build and deploy:
  docker buildx build --platform linux/amd64,linux/arm64 -t homebase:$v .   # add --push with a registry tag
  HOMEBASE_VERSION=$v docker compose up -d
EOF
