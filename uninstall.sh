#!/usr/bin/env bash
# Remove the runcat-ai-usage launchd agent.
set -euo pipefail

LABEL="io.github.ukkiee.runcat-ai-usage"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
rm -f "$PLIST"

echo "Removed launchd agent: $LABEL"
echo "Left in place: ~/.claude/runcat-usage.json, ~/.codex/runcat-usage.json"
echo "Remove those sources in RunCat Neo settings if you no longer want the cards."
