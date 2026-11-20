#!/usr/bin/env bash
# Build a sandbox image (TODO 1C1). Usage: scripts/build-sandbox.sh [python]
set -euo pipefail
lang="${1:-python}"
root="$(cd "$(dirname "$0")/.." && pwd)"
ctx="$root/sandboxes/$lang"
[ -f "$ctx/Dockerfile" ] || { echo "no sandbox for '$lang' in $ctx" >&2; exit 1; }
tag="grafter-sandbox-$lang:$(git -C "$root" rev-parse --short HEAD)"
docker build --pull -t "$tag" -t "grafter-sandbox-$lang:latest" "$ctx"
echo "built $tag"
cat <<NEXT
Smoke test against the fictional sandbox repo (no network, read-only source copy):
  docker run --rm --network none -v "$root/examples/sandbox-repo:/src:ro" grafter-sandbox-$lang:latest \
    sh -c 'cp -r /src/. /workspace && python -m pytest -q -p no:cacheprovider --cov'
NEXT
