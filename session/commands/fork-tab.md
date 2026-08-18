---
description: Fork this conversation into a new terminal tab (current session keeps running); both tabs are told about each other and coordinate via SendMessage
argument-hint: "[optional first prompt for the forked session]"
allowed-tools: Bash(${CLAUDE_PLUGIN_ROOT}/scripts/fork-tab.sh:*), Bash(~/.claude/scripts/fork-tab.sh:*), Bash(ls:*)
---

# Fork into a new terminal tab

**Locating the script.** It is `${CLAUDE_PLUGIN_ROOT}/scripts/fork-tab.sh`. If that path above
still contains the literal text `${CLAUDE_PLUGIN_ROOT}` — meaning this file is running as a
standalone `~/.claude/commands/` command rather than from an installed plugin, so the placeholder
was never substituted — use `~/.claude/scripts/fork-tab.sh` instead. Call the resolved path
`<script>` below.

Run exactly one command (from your current working directory — do NOT cd elsewhere first;
the script derives the fork's directory from pwd):

```
<script> <session-id> $ARGUMENTS
```

- `<session-id>` is the UUID in your scratchpad directory path from your system prompt
  (`…/<uuid>/scratchpad`). If unsure, verify with `ls ~/.claude/projects/*/<uuid>.jsonl`;
  fallback: UUID basename of `ls -t ~/.claude/projects/*/*.jsonl | head -1`.
- If `$ARGUMENTS` is non-empty, pass it after the session ID as the fork's first prompt
  (quote it as a single shell argument).

The script opens a new tab (iTerm2 or Ghostty, auto-detected) running
`cd <cwd> && claude --resume <id> --fork-session`, which resumes this conversation under a NEW
session ID — this session keeps running untouched and both can be driven independently. The
script prepends a handshake to the fork's first prompt telling it the parent (you) is still
running in another tab with the same working directory, and instructing it to find you via
ListAgents and introduce itself via SendMessage, mentioning your session id prefix.

When it succeeds, relay the script's one-line confirmation verbatim, plus one sentence
noting the fork will introduce itself via SendMessage.

**Standing instruction from this point on — you are the parent of a fork.** When the
fork's introduction arrives, confirm you are the session it forked from and agree on a
division of work. For the rest of this session, proactively SendMessage the fork when
you make significant findings or state changes, and check with it before edits that
could overlap — both tabs share the same working directory and files. If no
introduction has arrived by the next time you finish a task, run ListAgents and look
for the fork yourself.
