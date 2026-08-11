---
description: Fork this conversation into a new terminal tab (current session keeps running)
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
session ID — this session keeps running untouched and both can be driven independently.

When it succeeds, relay the script's one-line confirmation verbatim. Nothing else.
