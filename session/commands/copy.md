---
description: Copy content from the conversation to the macOS clipboard
argument-hint: <what to copy — a candidate number, grep pattern, or description>
allowed-tools: Bash(${CLAUDE_PLUGIN_ROOT}/scripts/ccopy:*), Bash(~/.claude/scripts/ccopy:*), Bash(for d in:*), Bash(pbcopy:*), Bash(cat:*)
---

# Copy to clipboard

Deterministic resolver result for **$ARGUMENTS** (it already ran `pbcopy` if the
first line starts with `Copied`; otherwise it printed the candidate index —
`path  [kind]  preview`, newest first, kinds: cmd = bash command, out = output,
code = fenced code block, txt = assistant text):

!`for d in "${CLAUDE_PLUGIN_ROOT}" "$HOME/.claude"; do [ -x "$d/scripts/ccopy" ] && exec "$d/scripts/ccopy" $ARGUMENTS; done; true`

## Your task

- If the output above starts with `Copied` and it plausibly matches what the user
  asked for, the copy is already done — relay that one `Copied: …` line verbatim
  and stop. No tool calls.
- If it matched the wrong candidate or nothing matched, pick the right file from
  the index using your memory of the conversation (prefer the most recent match;
  `cat` a file if unsure), then run exactly `pbcopy < <path>` — never inline
  content through a heredoc. Reply with one line: `Copied: <first ~80 chars>`.
- If the target isn't in any candidate but exists in your context, write it
  verbatim to /tmp/claude-copy/manual.txt and pbcopy that file.
