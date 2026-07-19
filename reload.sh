#!/bin/bash
# Run this from Terminal (not Claude Code) to reload the bridge LaunchAgent.
# Must be run from Terminal so launchd inherits Full Disk Access for chat.db.
PLIST="$HOME/Library/LaunchAgents/com.devin.imessagebridge.plist"
launchctl bootout gui/$(id -u) "$PLIST" 2>/dev/null
sleep 1
launchctl bootstrap gui/$(id -u) "$PLIST"
echo "Bridge reloaded. Check logs:"
echo "  tail -f $HOME/code/zemdawg/logs/bridge.log"
