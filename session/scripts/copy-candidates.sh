#!/usr/bin/env bash
# Extract copyable candidates from a Claude Code session transcript.
# Usage: copy-candidates.sh [transcript.jsonl]
# Writes each candidate to /tmp/claude-copy/N.txt (1 = most recent) and
# prints an index line per candidate: <path>\t[kind]\t<preview>
# Kinds: cmd = bash command, out = tool/command output, code = fenced code
# block from an assistant message, txt = assistant message text.
set -euo pipefail

OUT=/tmp/claude-copy
rm -rf "$OUT"
staging="$OUT/.staging"
mkdir -p "$staging"

TRANSCRIPT="${1:-}"
if [[ -z "$TRANSCRIPT" && -n "${CLAUDE_CODE_SESSION_ID:-}" ]]; then
  TRANSCRIPT=$(ls "$HOME/.claude/projects"/*/"$CLAUDE_CODE_SESSION_ID.jsonl" 2>/dev/null | head -1 || true)
fi
if [[ -z "$TRANSCRIPT" ]]; then
  # Fallback: newest transcript for this project dir — can pick the wrong
  # session when several run concurrently in the same directory.
  proj="$HOME/.claude/projects/$(pwd | LC_ALL=C sed -e 's;[/.];-;g')"
  TRANSCRIPT=$(ls -t "$proj"/*.jsonl 2>/dev/null | head -1 || true)
  [[ -n "$TRANSCRIPT" ]] || TRANSCRIPT=$(ls -t "$HOME"/.claude/projects/*/*.jsonl 2>/dev/null | head -1 || true)
fi
[[ -f "${TRANSCRIPT:-}" ]] || { echo "No session transcript found." >&2; exit 1; }

n=0
emit() { # $1=kind $2=file
  n=$((n + 1))
  mv "$2" "$staging/$n.txt"
  echo "$1" >"$staging/$n.kind"
}

# Pull candidates from the last 500 transcript messages, oldest first.
while IFS=$'\t' read -r kind b64; do
  cur="$staging/cur"
  printf '%s' "$b64" | base64 -d >"$cur" 2>/dev/null || continue
  [[ -s "$cur" ]] || continue
  if [[ "$kind" == "txt" ]] && grep -q '^[[:space:]]*```' "$cur"; then
    nblocks=$(awk -v pre="$staging/blk" '
      /^[[:space:]]*```/ { if (inb) { inb = 0; close(out) } else { inb = 1; b++; out = pre b ".txt" }; next }
      inb { print >out }
      END { print b + 0 }' "$cur")
    for ((b = 1; b <= nblocks; b++)); do
      [[ -s "$staging/blk$b.txt" ]] && emit code "$staging/blk$b.txt"
    done
  fi
  emit "$kind" "$cur"
done < <(tail -n 500 "$TRANSCRIPT" | jq -r '
  if .type == "assistant" then
    .message.content[]?
    | if .type == "tool_use" and .name == "Bash" and ((.input.command // "") | length > 0) then
        "cmd\t" + (.input.command | @base64)
      elif .type == "text" and ((.text // "") | length > 0) then
        "txt\t" + (.text | @base64)
      else empty end
  elif .type == "user" then
    (.toolUseResult
     | if type == "string" then .
       elif type == "object" then
         (.stdout // ((.content // []) | if type == "array" then map(.text // empty) | join("\n") else "" end))
       else "" end) as $o
    | if ($o | type) == "string" and ($o | length) > 0 then "out\t" + ($o | @base64) else empty end
  else empty end' 2>/dev/null)

# Print newest first, deduped, capped at 30.
i=0
declare -A seen
for ((k = n; k >= 1; k--)); do
  f="$staging/$k.txt"
  h=$(shasum <"$f" | cut -d' ' -f1)
  [[ -n "${seen[$h]:-}" ]] && continue
  seen[$h]=1
  i=$((i + 1))
  mv "$f" "$OUT/$i.txt"
  preview=$(head -c 400 "$OUT/$i.txt" | LC_ALL=C tr '\n\t' '  ' | LC_ALL=C cut -c1-80)
  printf '%s\t[%s]\t%s\n' "$OUT/$i.txt" "$(cat "$staging/$k.kind")" "$preview"
  ((i >= 30)) && break
done
rm -rf "$staging"

((i > 0)) || echo "No copyable candidates found in $TRANSCRIPT"
