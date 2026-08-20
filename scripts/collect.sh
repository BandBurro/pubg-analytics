#!/bin/bash
# Scheduled collection run, invoked by launchd.
#
# launchd gives us a near-empty environment, so PATH is set explicitly.
# A lock directory prevents a slow run from overlapping the next trigger.
set -euo pipefail

PROJECT_DIR="$HOME/personal-projects/pubg-analytics"
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

LIMIT="${1:-1000}"
LOCK_DIR="$PROJECT_DIR/data/.collect.lock"
STALE_MINUTES=180

cd "$PROJECT_DIR"
mkdir -p data/logs

ts() { date -u +%Y-%m-%dT%H:%M:%SZ; }

# Clear a lock left behind by a hard kill, so one crash can't halt collection forever.
if [ -d "$LOCK_DIR" ] && [ -z "$(find "$LOCK_DIR" -maxdepth 0 -mmin "-$STALE_MINUTES")" ]; then
    echo "$(ts) clearing stale lock"
    rmdir "$LOCK_DIR" 2>/dev/null || true
fi

if ! mkdir "$LOCK_DIR" 2>/dev/null; then
    echo "$(ts) another run in progress — skipping"
    exit 0
fi
trap 'rmdir "$LOCK_DIR" 2>/dev/null || true' EXIT

echo "$(ts) collect start (limit=$LIMIT)"
uv run pubg run --limit "$LIMIT"
echo "$(ts) collect done — $(uv run pubg status | grep -E '^\s+done' | tr -s ' ')"
