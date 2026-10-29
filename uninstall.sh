#!/usr/bin/env bash
# Remove the runcat-ai-usage launchd agent.
set -euo pipefail

LABEL="io.github.ukkiee.runcat-ai-usage"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

# The homes the agent was polling, read before the plist is gone.
CLAUDE_HOMES="$(/usr/libexec/PlistBuddy -c "Print :EnvironmentVariables:RUNCAT_CLAUDE_HOMES" "$PLIST" 2>/dev/null || true)"
CLAUDE_HOMES="${CLAUDE_HOMES:-$HOME/.claude}"
CODEX_HOMES="$(/usr/libexec/PlistBuddy -c "Print :EnvironmentVariables:RUNCAT_CODEX_HOMES" "$PLIST" 2>/dev/null || true)"
CODEX_HOMES="${CODEX_HOMES:-$HOME/.codex}"

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
rm -f "$PLIST"

# The poller's own state, including runcat-reset-state.json — the obsolete file
# the persisted Usage Reading replaced. The cards themselves are left alone: they
# are what RunCat Neo is pointed at, so removing them is the user's call.
CARDS=""
IFS=',' read -ra HOMES <<< "$CLAUDE_HOMES"
for home in "${HOMES[@]}"; do
  home="$(echo "$home" | xargs)"
  [ -n "$home" ] || continue
  home="${home/#\~/$HOME}"
  home="${home%/}"
  rm -f "$home/runcat-reading.json" \
        "$home/runcat-reset-state.json"
  CARDS="${CARDS:+$CARDS, }$home/runcat-usage.json"
done
IFS=',' read -ra HOMES <<< "$CODEX_HOMES"
for home in "${HOMES[@]}"; do
  home="$(echo "$home" | xargs)"
  [ -n "$home" ] || continue
  home="${home/#\~/$HOME}"
  home="${home%/}"
  rm -f "$home/runcat-reading.json" \
        "$home/runcat-reset-state.json" \
        "$home/runcat-rotation-lost.json"
  CARDS="$CARDS, $home/runcat-usage.json"
done

echo "Removed launchd agent: $LABEL, and the poller's state files"
echo "Left in place: $CARDS"
echo "Remove those sources in RunCat Neo settings if you no longer want the cards."
