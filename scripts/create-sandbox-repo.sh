#!/usr/bin/env bash
# Create the private GitHub repo <owner>/grafter-sandbox from examples/sandbox-repo (TODO 0D5b).
# Usage: scripts/create-sandbox-repo.sh <owner>
set -euo pipefail

owner="${1:?usage: $0 <github-owner>}"
repo="$owner/grafter-sandbox"
root="$(cd "$(dirname "$0")/.." && pwd)"
src="$root/examples/sandbox-repo"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

command -v gh >/dev/null || { echo "gh CLI is required" >&2; exit 1; }
if gh repo view "$repo" >/dev/null 2>&1; then
  echo "$repo already exists; nothing to do" >&2
  exit 1
fi

cp -R "$src/." "$work/"
sed -i.bak "s/@OWNER/@$owner/" "$work/.github/CODEOWNERS" && rm -f "$work/.github/CODEOWNERS.bak"
cd "$work"
git init -q -b main
git add -A
git commit -q -m "Initial fictional sandbox for Grafter end-to-end tests"
gh repo create "$repo" --private --source . --push

cat <<NEXT
Created https://github.com/$repo (private).
Next (0D5b):
  1. Install the Grafter GitHub App on $repo (0D2).
  2. Add a ruleset on main: PR + 1 approval + status check "test", no force push, App not in bypass.
  3. Put deploy/grafter.sandbox.yaml on the VM as ~/.grafter/grafter.yaml with OWNER=$owner.
NEXT
