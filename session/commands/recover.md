---
description: Browse & resume Claude Code sessions that were closed or crashed (incl. ones missing from --resume)
argument-hint: "[crashed|here|recent|search|all|done] [--native] [--all] [--include-done] [--since 24h] [keywords]"
allowed-tools: Bash(python3:*), Bash(pwd), Bash(command -v:*), AskUserQuestion
---

You are helping the user re-open Claude Code sessions that aren't currently running — including
ones killed by a crash that never show up in `claude --resume`.

**Locating the script.** It is `${CLAUDE_PLUGIN_ROOT}/scripts/claude-session-recover.py`. If that
path still contains the literal text `${CLAUDE_PLUGIN_ROOT}` — meaning this file is running as a
standalone `~/.claude/commands/` command rather than from an installed plugin, so the placeholder
was never substituted — use `~/.claude/scripts/claude-session-recover.py` instead.

Call it as `python3 "<resolved path>" <subcmd> …`. Below it is written as `<script>` for brevity —
always substitute the resolved path.

It finds non-live sessions, **scores each one's resume-likelihood**, previews them, and opens them
as iTerm2 tabs. Your job is to drive it. **Never reopen a session that is already live** — the
script excludes those automatically, so always go through it rather than guessing session ids.

## Resume-likelihood (why noise is hidden)

Each session gets a bucket + reason (shown in every view): **▲ high** (uncommitted/unpushed work, or
crashed mid-work, or an open linked bead), **● medium** (recent/idle open work), **· low** (stale),
**✓ done** (dir deleted, PR merged, branch merged, bead closed, or you marked it done), **⏸ snoozed**.
**`done` and `snoozed` are hidden by default** — that's the whole point. Sorted best-first.

## Arguments

`$ARGUMENTS` may contain (all optional, in any order):
- a category word: `crashed`, `here`, `recent`, `search`, `all`, or `done` (show only done/snoozed)
- free-text keywords (anything not recognized as a flag) → treat as a search query
- `--native` → force the in-chat flow even if `fzf` is installed
- `--all` → skip selection and open every session in the chosen filter
- `--include-done` → don't hide done/snoozed
- `--since <window>` → e.g. `--since 12h` (default `24h`, only affects `recent`)

## Verdicts (manual override, persisted in the work-tracker beads DB)

- `mark-done <ids>` — hide these from `/session:recover` (e.g. you're finished with them)
- `snooze <ids> --until +3d` — hide until later
- `keep <ids>` — undo done/snooze
In the fzf picker, **^X** marks the highlighted row done in place. If the user asks to dismiss/snooze
sessions, run the matching subcommand.

Verdicts need the `bd` work-tracker CLI with a DB at `~/.claude/work-tracker/.beads`. If it isn't
installed the script degrades gracefully — everything else still works, verdicts just don't persist.

## Freshness

Resume-likelihood uses git + cached PR state. PR-merged detection needs a network check: if the user
wants up-to-date PR status, run `python3 <script> refresh` (warms the cache via `gh`; slower). Plain
`list`/`pick` stay fast and use cached/local-git signals.

## Step 1 — context

Run `pwd` to learn the invoking directory (needed for the **here** category). Then check for fzf:
`command -v fzf`.

## Step 2 — pick the path

**If `fzf` is available AND `--native` was NOT passed → use the fzf picker (preferred):**
Run:
```
python3 <script> pick --cwd "<pwd>"
```
This pops a picker in a new iTerm window where the user fuzzy-searches, multi-selects with TAB,
toggles categories with `^C` (crashed) / `^F` (this folder) / `^A` (all), and presses Enter to open
the chosen sessions as tabs. Tell them to drive it there. You're done — do not also run the native
flow.

(If they passed a category and/or `--all`, still just launch the picker — it's interactive and
covers every category. Only fall through to the native flow when they explicitly asked for it via a
category word together with `--native`, or when fzf is missing.)

**Otherwise (no fzf, or `--native`) → native in-chat flow:**

1. **Decide the filter.** If a category word was given in `$ARGUMENTS`, use it. If keywords were
   given, use `search` with those keywords as `--query`. If nothing was given, ask with
   `AskUserQuestion` (header "Find by"): **Recent · Crashed · This folder · Search by keyword**.
   - "Search by keyword" → ask for the term (or reuse keywords from `$ARGUMENTS`).
2. **List it.** Run the script's `list` for the chosen filter, e.g.:
   - `list --category recent --since 24h`
   - `list --category here --cwd "<pwd>"`
   - `list --category crashed`
   - `list --category search --query "<term>"`
   - `list --category all`
   Show the returned table to the user (it's already ranked/most-recent-first and capped; if they
   want more, re-run with `--limit 0` or a tighter `--query`/`--since`). To get stable ids for
   launching, re-run the same query with `--format json` and map the row numbers you displayed to
   their `id`s.
3. **Select.** If `--all` was passed, open them all. Otherwise ask which to open — accept `all` or a
   list like `1,3,5`. Map the chosen rows to their session ids.
4. **Open.** Run:
   `python3 <script> launch --ids <id1,id2,…>`
   (add `--terminal print` instead if the user only wants the commands, not opened tabs.)
   Report how many tabs opened and surface any `⚠ skipped` lines (e.g. a deleted worktree dir).

## Notes
- `crashed` is precise but can be empty if Claude already swept stale heartbeats after the restart —
  if so, the script says exactly that; suggest **recent** or **all** / a keyword search instead.
- Sessions resume by id and continue the same transcript; opening one is safe and non-destructive.
- Default terminal is iTerm2. The picker and `launch` both open a single new iTerm window with one
  tab per session.
