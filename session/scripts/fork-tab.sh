#!/bin/bash
# fork-tab.sh <session-id> [--dry-run] [initial prompt...]
# Opens a new iTerm tab in the CURRENT window running:
#   cd <cwd-of-caller> && claude --resume <session-id> --fork-session "$(cat <promptfile>)"
# The cd is derived from pwd here, so it can never be omitted:
# `claude --resume` only finds sessions belonging to the current folder.
# The handshake prepended to the fork's first prompt tells the fork that its
# parent session is still running in another tab with the same cwd, and
# instructs it to find the parent via ListAgents, introduce itself via
# SendMessage, and coordinate to avoid stepping on each other's work.
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

handshake="[fork-tab handshake] You are a fork of session ${sid:0:8}. The parent session is still running in another iTerm tab with the same working directory ($cwd), so you can step on each other's work: same files, same repo, possibly the same task. Standing instruction for this entire session: run ListAgents, find the parent among the local peers, and introduce yourself via SendMessage, mentioning parent id ${sid:0:8} so the right session can confirm; agree on a division of work, proactively message the parent when you make significant findings or state changes, and check with it before edits that could overlap."
if [ -n "$prompt" ]; then
  prompt="$handshake First task: $prompt"
else
  prompt="$handshake No task was given at fork time: introduce yourself to the parent now, then await instructions."
fi

# The prompt travels via a temp file, never inline on the typed command: iTerm
# types the command into the new tab before the shell is ready, and the tty's
# canonical-mode input buffer silently drops lines longer than 1024 bytes
# (MAX_CANON) — the handshake alone would blow that and leave the command
# truncated and unexecuted at the prompt.
promptfile="$(mktemp -t fork-tab-prompt)"
printf '%s' "$prompt" > "$promptfile"

cmd="cd $(printf %q "$cwd") && claude --resume $sid --fork-session \"\$(cat $(printf %q "$promptfile"))\""

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
