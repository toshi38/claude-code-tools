#!/bin/bash
# fork-tab.sh <session-id> [--dry-run] [initial prompt...]
# Opens a new iTerm tab in the CURRENT window running:
#   cd <cwd-of-caller> && claude --resume <session-id> --fork-session ['prompt']
# The cd is derived from pwd here, so it can never be omitted:
# `claude --resume` only finds sessions belonging to the current folder.
set -euo pipefail

sid="${1:?usage: fork-tab.sh <session-id> [--dry-run] [prompt...]}"
shift
dry=0
if [ "${1:-}" = "--dry-run" ]; then dry=1; shift; fi
prompt="${*:-}"

if ! ls "$HOME"/.claude/projects/*/"$sid".jsonl >/dev/null 2>&1; then
  echo "error: no transcript found for session $sid" >&2
  exit 1
fi

cwd="$(pwd)"
cmd="cd $(printf %q "$cwd") && claude --resume $sid --fork-session"
if [ -n "$prompt" ]; then
  cmd="$cmd $(printf %q "$prompt")"
fi

if [ "$dry" = 1 ]; then
  echo "$cmd"
  exit 0
fi

# Escape for embedding inside an AppleScript double-quoted string.
esc="${cmd//\\/\\\\}"
esc="${esc//\"/\\\"}"

if ! osascript -e "tell application \"iTerm\"
  activate
  tell current window
    create tab with default profile
    tell current session of current tab
      write text \"$esc\"
    end tell
  end tell
end tell" 2>/dev/null; then
  # No current window — open a new one instead.
  osascript -e "tell application \"iTerm\"
  activate
  set w to (create window with default profile)
  tell current session of current tab of w
    write text \"$esc\"
  end tell
end tell"
fi

echo "Forked ${sid:0:8} into a new iTerm tab (cwd: $cwd)"
