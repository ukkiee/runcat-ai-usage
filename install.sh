#!/usr/bin/env bash
# Install the runcat-ai-usage launchd agent. Runs the poller from this folder
# every RUNCAT_POLL_INTERVAL seconds (default 300).
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LABEL="io.github.ukkiee.runcat-ai-usage"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
INTERVAL="${RUNCAT_POLL_INTERVAL:-300}"
PY="$(command -v python3 || true)"

if [ -z "$PY" ]; then
  echo "error: python3 not found on PATH" >&2
  exit 1
fi

# launchd starts the poller without a login shell, so settings exported when
# installing are written into the agent itself.
ENV_XML=""
for key in RUNCAT_CLAUDE_HOMES RUNCAT_CODEX_HOMES RUNCAT_LANG; do
  value="${!key:-}"
  [ -n "$value" ] || continue
  value="${value//&/&amp;}"; value="${value//</&lt;}"; value="${value//>/&gt;}"
  ENV_XML="$ENV_XML    <key>$key</key><string>$value</string>"$'\n'
done
if [ -n "$ENV_XML" ]; then
  ENV_XML="  <key>EnvironmentVariables</key>"$'\n'"  <dict>"$'\n'"$ENV_XML  </dict>"
fi

mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PY</string>
    <string>$DIR/runcat-poll.py</string>
  </array>
  <key>StartInterval</key><integer>$INTERVAL</integer>
  <key>RunAtLoad</key><true/>
  <key>ProcessType</key><string>Background</string>
  <key>LowPriorityIO</key><true/>
  <key>StandardOutPath</key><string>$DIR/runcat-poll.log</string>
  <key>StandardErrorPath</key><string>$DIR/runcat-poll.log</string>
$ENV_XML
</dict>
</plist>
EOF

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
launchctl kickstart -k "gui/$(id -u)/$LABEL"

echo "Installed launchd agent: $LABEL (every ${INTERVAL}s)"
echo
echo "Next: RunCat Neo -> Settings -> Metrics -> Custom Metrics -> Add Custom Metrics Source"
if [ -n "${RUNCAT_CLAUDE_HOMES:-}" ]; then
  IFS=',' read -ra CLAUDE_HOMES <<< "$RUNCAT_CLAUDE_HOMES"
  for home in "${CLAUDE_HOMES[@]}"; do
    home="$(echo "$home" | xargs)"
    [ -n "$home" ] && echo "  ${home%/}/runcat-usage.json   (Claude Code)"
  done
else
  echo "  ~/.claude/runcat-usage.json   (Claude Code)"
fi
if [ -n "${RUNCAT_CODEX_HOMES:-}" ]; then
  IFS=',' read -ra CODEX_HOMES <<< "$RUNCAT_CODEX_HOMES"
  for home in "${CODEX_HOMES[@]}"; do
    home="$(echo "$home" | xargs)"
    [ -n "$home" ] && echo "  ${home%/}/runcat-usage.json   (Codex)"
  done
else
  echo "  ~/.codex/runcat-usage.json    (Codex)"
fi
