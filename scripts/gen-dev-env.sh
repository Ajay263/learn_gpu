#!/usr/bin/env bash
# Create .env from .env.example, replacing every "change-me*" value with a random secret.
# Refuses to overwrite an existing .env.
set -euo pipefail
cd "$(dirname "$0")/.."

[ -e .env ] && { echo ".env already exists — not overwriting." >&2; exit 1; }

while IFS= read -r line; do
  if [[ "$line" =~ ^([A-Z0-9_]+)=change-me ]]; then
    printf '%s=%s\n' "${BASH_REMATCH[1]}" "$(openssl rand -hex 20)"
  else
    printf '%s\n' "$line"
  fi
done < .env.example > .env
chmod 600 .env
echo "Wrote .env with random secrets (mode 600)."
