#!/usr/bin/env bash
# Requires GitHub CLI (gh). Repository is private by default.
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
command -v gh >/dev/null || { echo 'Install GitHub CLI: https://cli.github.com/'; exit 1; }
gh auth status >/dev/null 2>&1 || { echo 'Run gh auth login first (do not paste tokens into chat).'; exit 1; }
ACCOUNT="$(gh api user --jq .login)"
REPOSITORY="${1:-photo-protection-platform}"
if git remote get-url origin >/dev/null 2>&1; then
  echo 'origin already exists; inspect it and push manually.'
  exit 1
fi
gh repo create "$ACCOUNT/$REPOSITORY" --private --source=. --remote=origin --push
