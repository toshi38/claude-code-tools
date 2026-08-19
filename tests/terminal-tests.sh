#!/bin/bash
# terminal-tests.sh [--live]
# Exercises the terminal-driving surface of the session plugin: which terminal
# gets chosen, what AppleScript is generated for it, and how failures are
# reported. Everything runs against a stub `osascript` and opens no windows.
#
# --live additionally drives the real iTerm2 and Ghostty: each opens a window,
# runs a command that touches a sentinel file, and closes itself. That is the
# only way to prove `initial input` and `write text` actually reach the shell.
set -uo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FORK="$root/session/scripts/fork-tab.sh"
REC="$root/session/scripts/claude-session-recover.py"
live=0
[ "${1:-}" = "--live" ] && live=1

tmp="$(mktemp -d)"
# fork-tab.sh leaves its prompt temp file behind for the new tab to read, and
# macOS mktemp -t ignores TMPDIR, so sweep whatever this run created.
sysmp="$(dirname "$(mktemp -u -t sweep)")"
marker="$tmp/.started"; : > "$marker"
trap 'find "$sysmp" -maxdepth 1 -name "fork-tab-prompt.*" -newer "$marker" -delete 2>/dev/null; rm -rf "$tmp"' EXIT
pass=0; fail=0; skip=0

ok()   { printf '  \033[32mPASS\033[0m %s\n' "$1"; pass=$((pass+1)); }
bad()  { printf '  \033[31mFAIL\033[0m %s\n' "$1"; [ -n "${2:-}" ] && printf '        %s\n' "$2"; fail=$((fail+1)); }
skipt(){ printf '  \033[33mSKIP\033[0m %s\n' "$1"; skip=$((skip+1)); }
group(){ printf '\n\033[1m%s\033[0m\n' "$1"; }
is()   { [ "$2" = "$3" ] && ok "$1" || bad "$1" "expected '$3', got '$2'"; }

# A stub osascript that records each script it is handed and then succeeds or
# fails per STUB_MODE, so failure handling is testable without breaking a real
# terminal.
mkdir -p "$tmp/bin"
cat > "$tmp/bin/osascript" <<'EOF'
#!/bin/bash
shift
n=$(( $(cat "$STUB_DIR/count" 2>/dev/null || echo 0) + 1 ))
echo "$n" > "$STUB_DIR/count"
printf '%s' "$1" > "$STUB_DIR/script.$n"
case "${STUB_MODE:-ok}" in
  fail)  echo "stub: simulated osascript failure" >&2; exit 1 ;;
  first) if [ "$n" = 1 ]; then echo "stub: simulated first-attempt failure" >&2; exit 1; fi ;;
esac
exit 0
EOF
chmod +x "$tmp/bin/osascript"

stub_reset() { rm -rf "$tmp/stub"; mkdir -p "$tmp/stub"; }
stub_count() { cat "$tmp/stub/count" 2>/dev/null || echo 0; }
# Run fork-tab.sh with the stub in front of the real osascript.
forkrun() { STUB_DIR="$tmp/stub" PATH="$tmp/bin:$PATH" bash "$FORK" "$@"; }
# Evaluate a helper from the recover script without running its CLI.
pyeval() {
  python3 -c "
import importlib.util, sys
spec = importlib.util.spec_from_file_location('rec', '$REC')
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
$1"
}

# A session id the scripts will accept: fork-tab.sh requires a real transcript.
sid="$(ls -t "$HOME"/.claude/projects/*/*.jsonl 2>/dev/null | head -1 | xargs -I{} basename {} .jsonl)"
[ -z "$sid" ] && { echo "no Claude transcripts found; cannot run"; exit 1; }

# ---------------------------------------------------------------- detection
group "Terminal detection is one contract, applied by both scripts"
while IFS='|' read -r override termprog expect; do
  [ -z "$override$termprog$expect" ] && continue
  stub_reset
  sh_out="$(STUB_DIR="$tmp/stub" PATH="$tmp/bin:$PATH" CLAUDE_SESSION_TERMINAL="$override" \
            TERM_PROGRAM="$termprog" bash "$FORK" "$sid" 2>/dev/null)"
  case "$sh_out" in *Ghostty*) sh_term=ghostty ;; *iTerm*) sh_term=iterm ;; *) sh_term=error ;; esac
  py_term="$(CLAUDE_SESSION_TERMINAL="$override" TERM_PROGRAM="$termprog" \
             pyeval "print(m._detect_terminal())" 2>/dev/null || echo error)"
  label="CLAUDE_SESSION_TERMINAL='$override' TERM_PROGRAM='$termprog'"
  if [ "$sh_term" != "$py_term" ]; then
    bad "$label" "fork-tab says '$sh_term', recover says '$py_term'"
  else
    is "$label -> $expect" "$sh_term" "$expect"
  fi
done <<'CASES'
|ghostty|ghostty
|Ghostty|ghostty
||iterm
|iTerm.app|iterm
ghostty||ghostty
GHOSTTY||ghostty
 ghostty ||ghostty
iterm|ghostty|iterm
kitty|ghostty|error
iterm2||error
CASES

group "An unsupported override is refused, not guessed at"
stub_reset
out="$(CLAUDE_SESSION_TERMINAL=kitty forkrun "$sid" 2>&1)"; rc=$?
is "fork-tab exits 2" "$rc" "2"
case "$out" in *"not a supported terminal"*) ok "fork-tab names the accepted values" ;;
               *) bad "fork-tab names the accepted values" "$out" ;; esac
out="$(CLAUDE_SESSION_TERMINAL=kitty pyeval "m._detect_terminal()" 2>&1)"; rc=$?
is "recover exits 2" "$rc" "2"
case "$out" in *"not a supported terminal"*) ok "recover names the accepted values" ;;
               *) bad "recover names the accepted values" "$out" ;; esac

# ------------------------------------------------------------- no-op modes
group "Preview modes open nothing"
stub_reset
out="$(forkrun "$sid" --dry-run "a prompt")"
is "fork-tab --dry-run drives no terminal" "$(stub_count)" "0"
case "$out" in "cd "*"claude --resume $sid --fork-session"*) ok "fork-tab --dry-run prints the command" ;;
               *) bad "fork-tab --dry-run prints the command" "$out" ;; esac
stub_reset
out="$(STUB_DIR="$tmp/stub" PATH="$tmp/bin:$PATH" python3 "$REC" launch --terminal print --ids "$sid" 2>/dev/null)"
is "launch --terminal print drives no terminal" "$(stub_count)" "0"
expected="$(pyeval "
pairs, _ = m.resolve(['$sid'])
print(m._resume_cmd(*pairs[0]) if pairs else '')")"
if [ -n "$expected" ]; then
  is "launch --terminal print matches _resume_cmd" "$out" "$expected"
else
  skipt "launch --terminal print matches _resume_cmd (session cwd is gone)"
fi

# -------------------------------------------------------------- escaping
group "Hostile working directories survive AppleScript quoting"
hostile='/tmp/dir with spaces/and"quote/and\back'
gs="$(pyeval "print(m._applescript_ghostty([(r'''$hostile''', 'abc123')]))")"
it="$(pyeval "print(m._applescript_iterm([(r'''$hostile''', 'abc123')]))")"
for pair in "Ghostty:$gs" "iTerm:$it"; do
  name="${pair%%:*}"; script="${pair#*:}"
  # Every double quote in the body must be escaped; the only bare ones are the
  # delimiters, so an even count of backslash-escaped quotes is not enough --
  # check no line has an unescaped quote inside the string body.
  if printf '%s\n' "$script" | grep -q '[^\\]"[^ &]*[^\\]"[^ &]*[^\\]"'; then
    bad "$name escapes embedded quotes" "$(printf '%s' "$script" | grep '"' | head -2)"
  else
    ok "$name escapes embedded quotes"
  fi
  case "$script" in *'\\back'*) ok "$name escapes backslashes" ;;
                    *) bad "$name escapes backslashes" ;; esac
done
want="$(HOSTILE="$hostile" pyeval "import os, shlex; print(m._as_quote(shlex.quote(os.environ['HOSTILE'])))")"
if printf '%s' "$gs" | grep -qF -- "cd $want &&"; then
  ok "Ghostty shell-quotes the cd target"
else
  bad "Ghostty shell-quotes the cd target" "wanted: cd $want"
fi

group "Multi-session launch builds one window with a tab each"
two="$(pyeval "print(m._applescript_ghostty([('/tmp','a1'),('/tmp','b2')]))")"
is "Ghostty creates exactly one window" "$(printf '%s\n' "$two" | grep -c 'new window')" "1"
is "Ghostty adds the rest as tabs"     "$(printf '%s\n' "$two" | grep -c 'new tab in w')" "1"
two="$(pyeval "print(m._applescript_iterm([('/tmp','a1'),('/tmp','b2')]))")"
is "iTerm creates exactly one window"  "$(printf '%s\n' "$two" | grep -c 'create window')" "1"
is "iTerm adds the rest as tabs"       "$(printf '%s\n' "$two" | grep -c 'create tab')" "1"

# --------------------------------------------------------- failure reporting
group "A terminal that cannot be driven is reported, never counted as success"
for term in ghostty iterm; do
  stub_reset
  out="$(STUB_MODE=fail STUB_DIR="$tmp/stub" PATH="$tmp/bin:$PATH" \
         CLAUDE_SESSION_TERMINAL=$term bash "$FORK" "$sid" 2>/dev/null)"; rc=$?
  [ "$rc" -ne 0 ] && ok "fork-tab ($term) exits non-zero" \
                  || bad "fork-tab ($term) exits non-zero" "exit $rc"
  case "$out" in *Forked*) bad "fork-tab ($term) prints no false confirmation" "$out" ;;
                 *) ok "fork-tab ($term) prints no false confirmation" ;; esac
  stub_reset
  out="$(STUB_MODE=fail STUB_DIR="$tmp/stub" PATH="$tmp/bin:$PATH" \
         CLAUDE_SESSION_TERMINAL=$term python3 "$REC" launch --ids "$sid" 2>/dev/null)"; rc=$?
  err="$(STUB_MODE=fail STUB_DIR="$tmp/stub" PATH="$tmp/bin:$PATH" \
         CLAUDE_SESSION_TERMINAL=$term python3 "$REC" launch --ids "$sid" 2>&1 >/dev/null)"
  [ "$rc" -ne 0 ] && ok "launch ($term) exits non-zero" \
                  || bad "launch ($term) exits non-zero" "exit $rc"
  case "$out" in *"Opened"*) bad "launch ($term) prints no false confirmation" "$out" ;;
                 *) ok "launch ($term) prints no false confirmation" ;; esac
  case "$err" in *"could not drive"*) ok "launch ($term) reports the real error" ;;
                 *) bad "launch ($term) reports the real error" "$err" ;; esac
  case "$out" in *"claude --resume"*) ok "launch ($term) offers the commands to run by hand" ;;
                 *) bad "launch ($term) offers the commands to run by hand" "$out" ;; esac
done
stub_reset
err="$(STUB_MODE=fail STUB_DIR="$tmp/stub" PATH="$tmp/bin:$PATH" \
       CLAUDE_SESSION_TERMINAL=ghostty python3 "$REC" launch --ids "$sid" 2>&1 >/dev/null)"
case "$err" in *"1.3+"*) ok "launch names the Ghostty version requirement" ;;
               *) bad "launch names the Ghostty version requirement" "$err" ;; esac

group "Ghostty falls back to a window when there is no tab to attach to"
stub_reset
out="$(STUB_MODE=first STUB_DIR="$tmp/stub" PATH="$tmp/bin:$PATH" \
       CLAUDE_SESSION_TERMINAL=ghostty bash "$FORK" "$sid" 2>/dev/null)"; rc=$?
is "both attempts are made" "$(stub_count)" "2"
case "$(cat "$tmp/stub/script.1")" in *"new tab in front window"*) ok "first attempt targets a tab" ;;
  *) bad "first attempt targets a tab" ;; esac
case "$(cat "$tmp/stub/script.2")" in *"new window with configuration"*) ok "fallback opens a window" ;;
  *) bad "fallback opens a window" ;; esac
is "the fork is still reported" "$rc" "0"
case "$out" in *Forked*) ok "confirmation survives the fallback" ;;
               *) bad "confirmation survives the fallback" "$out" ;; esac

group "Bad input is refused"
stub_reset
out="$(forkrun 00000000-0000-0000-0000-000000000000 2>&1)"; rc=$?
is "unknown session id exits 1" "$rc" "1"
is "unknown session id drives no terminal" "$(stub_count)" "0"

# ------------------------------------------------- real AppleScript dictionary
group "Generated AppleScript compiles against the installed dictionaries"
compile_check() { # name, script -- resolves terminology without launching the app
  if osacompile -o /dev/null -e "$2" >/dev/null 2>"$tmp/err"; then ok "$1"
  else bad "$1" "$(head -1 "$tmp/err")"; fi
}
if [ -d /Applications/Ghostty.app ]; then
  compile_check "Ghostty: recover multi-tab script" \
    "$(pyeval "print(m._applescript_ghostty([('/tmp','a1'),('/tmp','b2')]))")"
  stub_reset
  CLAUDE_SESSION_TERMINAL=ghostty forkrun "$sid" >/dev/null 2>&1
  compile_check "Ghostty: fork-tab new-tab script"  "$(cat "$tmp/stub/script.1")"
  stub_reset
  STUB_MODE=first STUB_DIR="$tmp/stub" PATH="$tmp/bin:$PATH" \
    CLAUDE_SESSION_TERMINAL=ghostty bash "$FORK" "$sid" >/dev/null 2>&1
  compile_check "Ghostty: fork-tab fallback script" "$(cat "$tmp/stub/script.2")"
else
  skipt "Ghostty AppleScript (Ghostty not installed)"
fi
if [ -d /Applications/iTerm.app ]; then
  compile_check "iTerm: recover multi-tab script" \
    "$(pyeval "print(m._applescript_iterm([('/tmp','a1'),('/tmp','b2')]))")"
  stub_reset
  CLAUDE_SESSION_TERMINAL=iterm forkrun "$sid" >/dev/null 2>&1
  compile_check "iTerm: fork-tab new-tab script" "$(cat "$tmp/stub/script.1")"
else
  skipt "iTerm AppleScript (iTerm2 not installed)"
fi

group "The typed command stays under the tty canonical-mode limit (MAX_CANON)"
stub_reset
n=$(forkrun "$sid" --dry-run "$(head -c 400 /dev/zero | tr '\0' 'x')" | wc -c | tr -d ' ')
[ "$n" -lt 1024 ] && ok "a 400-byte prompt yields a ${n}-byte command line" \
                  || bad "typed command is ${n} bytes, over the 1024 MAX_CANON limit"

# ------------------------------------------------------------------- live
# `initial input` and `write text` are both documented as text handed to the
# terminal, not as a command the terminal runs. Whether it reaches the shell,
# and whether the trailing newline submits it, is only answerable by doing it.
wait_for() { # path, seconds
  local i=0
  while [ "$i" -lt "$(( $2 * 4 ))" ]; do
    [ -s "$1" ] && return 0
    sleep 0.25; i=$((i+1))
  done
  return 1
}

if [ "$live" = 1 ]; then
  group "Live: the command actually reaches the shell and runs"
  if [ -d /Applications/Ghostty.app ]; then
    s="$tmp/ghostty.sentinel"
    osascript -e "tell application \"Ghostty\"
  activate
  set cfg to new surface configuration
  set initial working directory of cfg to \"$tmp\"
  set initial input of cfg to \"printf ok > '$s'; exit\" & linefeed
  new window with configuration cfg
end tell" >/dev/null 2>&1
    wait_for "$s" 15 && ok "Ghostty runs its initial input" \
                     || bad "Ghostty runs its initial input" "sentinel never written"

    # The tty canonical-mode buffer (MAX_CANON, 1024 bytes) applies to Ghostty's
    # `initial input` exactly as it does to iTerm's `write text`: the sentinel is
    # written only if the whole line survived, so truncation shows up as a
    # missing file. This is what makes the temp-file indirection load-bearing.
    probe_input() { # padding bytes -> 0 if the command ran
      local n=$1 d s2 i=0
      d="$(mktemp -d)"; s2="$d/s"
      local pad; pad="$(head -c "$n" /dev/zero | tr '\0' 'x')"
      osascript -e "tell application \"Ghostty\"
  set cfg to new surface configuration
  set initial working directory of cfg to \"$d\"
  set initial input of cfg to \": $pad; printf ok > '$s2'; exit\" & linefeed
  new window with configuration cfg
end tell" >/dev/null 2>&1
      while [ $i -lt 60 ]; do
        [ -s "$s2" ] && { rm -rf "$d"; return 0; }
        sleep 0.25; i=$((i+1))
      done
      rm -rf "$d"; return 1
    }
    probe_input 900 && ok "Ghostty runs a ~960-byte initial input intact" \
                    || bad "Ghostty runs a ~960-byte initial input intact" "lost under the limit"
    if probe_input 1200; then
      bad "Ghostty drops an initial input over MAX_CANON" \
          "it ran -- Ghostty no longer truncates; the temp-file rationale needs revisiting"
    else
      ok "Ghostty drops an initial input over MAX_CANON (why the prompt uses a temp file)"
    fi
  else
    skipt "Live Ghostty (not installed)"
  fi

  if [ -d /Applications/iTerm.app ]; then
    s="$tmp/iterm.sentinel"
    osascript -e "tell application \"iTerm\"
  activate
  set w to (create window with default profile)
  tell current session of current tab of w
    write text \"printf ok > '$s'; exit\"
  end tell
end tell" >/dev/null 2>&1
    wait_for "$s" 15 && ok "iTerm runs its written text" \
                     || bad "iTerm runs its written text" "sentinel never written"
  else
    skipt "Live iTerm (not installed)"
  fi
else
  group "Live checks"
  skipt "real terminal windows (pass --live to run them)"
fi

printf '\n\033[1m%d passed, %d failed, %d skipped\033[0m\n' "$pass" "$fail" "$skip"
[ "$fail" -eq 0 ]
