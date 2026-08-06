# claude-code-tools

Personal [Claude Code](https://code.claude.com) tooling, distributed as a plugin marketplace.

**Requires macOS + [iTerm2](https://iterm2.com).** Every command here drives iTerm via AppleScript.

## Install

```
/plugin marketplace add toshi38/claude-code-tools
/plugin install session@toshi38
```

That's it on every machine you use — updates come with `/plugin update`.

## Plugins

### `session` — conversation & session tooling

| Command | What it does |
| :--- | :--- |
| `/session:fork-tab [prompt]` | Forks the current conversation into a new iTerm tab under a **new** session id. The original keeps running untouched, so you can drive both independently. Optionally seed the fork with a first prompt. |
| `/session:recover [filter]` | Browses sessions that aren't currently running — **including ones killed by a crash that never appear in `claude --resume`** — ranks them by how likely you are to want them back (uncommitted work, open PRs, crashed mid-task), and reopens the ones you pick as tabs. |
| `/session:copy <what>` | Copies content out of the current transcript to the clipboard **without an LLM reading or retyping it**. Resolves a candidate deterministically by number or grep pattern, then `pbcopy`s the exact bytes. |

#### Why `/session:copy` exists

Asking Claude to "copy that command" makes it re-emit the text from memory, which can silently
mangle it. This walks the transcript JSONL, extracts candidates (bash commands, tool output, fenced
code blocks, assistant text), writes them to `/tmp/claude-copy/N.txt`, and pipes the chosen file
straight to `pbcopy`. What lands on your clipboard is byte-identical to what was in the session.

## Dependencies

Everything degrades gracefully except where noted.

| Tool | Needed by | Required? |
| :--- | :--- | :--- |
| iTerm2 | all three | **yes** |
| `jq` | `/session:copy` | **yes** — `brew install jq` |
| `python3` | `/session:recover` | **yes** (stdlib only, no pip installs) |
| `fzf` | `/session:recover` | optional — enables the fuzzy picker; without it you get an in-chat flow |
| `gh` | `/session:recover` | optional — enables PR-merged detection when ranking |
| `git` | `/session:recover` | optional — enables uncommitted/unpushed-work ranking |
| `bd` ([beads](https://github.com/steveyegge/beads)) | `/session:recover` verdicts | optional — without it, `mark-done`/`snooze` won't persist |

## Optional: `ccopy` on your PATH

`/session:copy` is the slash command. The same resolver also works standalone inside a session with
the `!` prefix (`! ccopy`, `! ccopy 3`, `! ccopy curl`), which skips the model round-trip entirely.
To get that, symlink it onto your PATH:

```sh
ln -s ~/.claude/plugins/cache/<installed-plugin-path>/scripts/ccopy ~/.local/bin/ccopy
```

Find the installed path with `/plugin`. `ccopy` resolves its helper script relative to its own real
path, following symlinks, so it works from anywhere on your PATH.

## Want short command names?

Plugin commands are always namespaced — `/session:fork-tab`, never a bare `/fork-tab` — and there's
no user-side setting to alias or rename them. If you want the short form, install the commands as
standalone config instead of (or as well as) the plugin. Clone this repo and symlink:

```sh
git clone https://github.com/toshi38/claude-code-tools ~/code/claude-code-tools
R=~/code/claude-code-tools/session

for f in fork-tab.md copy.md recover.md; do
  ln -sfn "$R/commands/$f" ~/.claude/commands/$f
done
for f in fork-tab.sh copy-candidates.sh claude-session-recover.py ccopy; do
  ln -sfn "$R/scripts/$f" ~/.claude/scripts/$f
done
```

That gives you `/fork-tab`, `/copy`, `/recover`, running straight from the checkout — so edits take
effect immediately, with no `/plugin update` step. The command files detect which mode they're in and
resolve their scripts accordingly, so the same files serve both.

Standalone and plugin installs coexist without conflict: you'd get both `/fork-tab` and
`/session:fork-tab`. Installing the plugin is the better path if you just want the tools; the symlink
route is for hacking on them.

## License

MIT — see [LICENSE](LICENSE).
