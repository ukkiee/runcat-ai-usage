#!/usr/bin/env bash
# Remove the runcat-ai-usage launchd agent.
set -euo pipefail

LABEL="io.github.ukkiee.runcat-ai-usage"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
rm -f "$PLIST"

# The poller's own state, including runcat-reset-state.json — the obsolete file
# the persisted Usage Reading replaced. The cards themselves are left alone: they
# are what RunCat Neo is pointed at, so removing them is the user's call.
rm -f "$HOME/.claude/runcat-reading.json" \
      "$HOME/.claude/runcat-reset-state.json" \
      "$HOME/.codex/runcat-reading.json" \
      "$HOME/.codex/runcat-reset-state.json" \
      "$HOME/.codex/runcat-rotation-lost.json"

echo "Removed launchd agent: $LABEL, and the poller's state files"
echo "Left in place: ~/.claude/runcat-usage.json, ~/.codex/runcat-usage.json"
echo "Remove those sources in RunCat Neo settings if you no longer want the cards."
