#!/usr/bin/env bash
# Seed an extra account's CLAUDE_CONFIG_DIR so remote-control launches never
# hang on first-run onboarding (theme picker / trust dialog), WITHOUT copying
# credentials — the account logs in via the dashboard, which writes its own
# .credentials.json into this dir. Idempotent; no sudo.
#
# Usage: ./seed-account.sh /home/you/.claude-alt [workdir-to-trust]
set -euo pipefail

DST="${1:?usage: seed-account.sh <config_dir> [workdir]}"
WORKDIR="${2:-$HOME}"
SRC="$HOME/.claude"

mkdir -p "$DST"

# Symlink shared, account-agnostic assets so they never drift.
for item in plugins skills CLAUDE.md settings.json settings.local.json; do
  if [ -e "$SRC/$item" ]; then
    ln -sfn "$SRC/$item" "$DST/$item"
  fi
done

# Per-account state file: complete onboarding + trust the workdir up front.
# CLAUDE_CONFIG_DIR moves this file to $DST/.claude.json — if it's missing,
# every headless launch stalls on interactive first-run prompts.
# Do NOT overwrite an existing one (would clobber accumulated state).
if [ ! -f "$DST/.claude.json" ]; then
  cat > "$DST/.claude.json" <<JSON
{
  "theme": "dark",
  "hasCompletedOnboarding": true,
  "projects": {
    "$WORKDIR": { "hasTrustDialogAccepted": true, "hasCompletedProjectOnboarding": true }
  }
}
JSON
  chmod 600 "$DST/.claude.json"
fi

echo "Seeded $DST"
if [ -f "$DST/.credentials.json" ]; then
  echo "Account is logged in."
else
  echo "Account NOT logged in yet — use the dashboard '🔑 Log in' button."
fi
