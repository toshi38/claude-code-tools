#!/usr/bin/env python3
"""
claude-session-recover.py — find and resume Claude Code sessions that were
closed or crashed, including ones that don't show up in `claude --resume`.

Subcommands:
  list     emit non-live sessions (filtered by category/query) as a table / json / fzf lines
  preview  pretty-print the tail of one session (used by the fzf preview pane)
  launch   open the given session ids as terminal tabs (cd <cwd> && claude --resume <id>)
  pick     interactive fzf picker (multi-select + preview + category hotkeys) -> launch

Detection facts this relies on (see plan):
  - ~/.claude/sessions/<pid>.json  : per-process heartbeat {pid, sessionId, cwd, status, updatedAt}
      live   = pid alive AND its `ps` command contains "claude"
      crashed= heartbeat whose pid is dead (process vanished without cleanup)
  - ~/.claude/projects/<enc-cwd>/<uuid>.jsonl : the resumable transcripts
      sub-agent transcripts live under <uuid>/subagents/ and are ignored
"""
import os, sys, glob, json, time, subprocess, argparse, shlex, signal

# Don't dump a traceback when output is piped into `head` etc.
try:
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)
except (AttributeError, ValueError):
    pass

HOME = os.path.expanduser("~")
SESSIONS_DIR = os.path.join(HOME, ".claude", "sessions")
PROJECTS_DIR = os.path.join(HOME, ".claude", "projects")
SELF = os.path.abspath(__file__)
PY = sys.executable or "python3"
CLAUDE = os.path.expanduser("~/.local/bin/claude")
if not os.path.exists(CLAUDE):
    CLAUDE = "claude"  # fall back to PATH lookup

# Global work tracker DB (NEVER call bare `bd` — it would resolve a team repo's .beads).
BEADS_DB = os.path.join(HOME, ".claude", "work-tracker", ".beads")
PASSB_WINDOW = 21 * 86400      # only run git/gh signals for dirs touched within 21d
PASSB_MAX_DIRS = 60            # …and at most this many distinct cwds (recency order)
NONTERMINAL_TTL = 45 * 60      # re-check open PRs/branches after 45 min
BUCKET_RANK = {"high": 0, "medium": 1, "low": 2, "snoozed": 3, "done": 4}
ALLOW_NETWORK = False          # set True by `refresh` so interactive `list` stays fast

# ----------------------------------------------------------------------------- beads / cache

def _run(args, timeout=8, cwd=None):
    """Run a command, return stdout str or None on any failure/timeout."""
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout, cwd=cwd)
        return p.stdout if p.returncode == 0 else None
    except Exception:
        return None

def _ancestor(repo, rev, base, timeout=6):
    """True iff `rev` is an ancestor of `base` (i.e. rev is merged into base)."""
    try:
        p = subprocess.run(["git", "-C", repo, "merge-base", "--is-ancestor", rev, base],
                           capture_output=True, text=True, timeout=timeout)
        return p.returncode == 0
    except Exception:
        return False

def _bd(*args, timeout=8):
    """Invoke bd against the tracker DB ONLY. Returns stdout or None."""
    return _run(["bd", "--db", BEADS_DB, *args], timeout=timeout)

_TRACKER_OK = None
def tracker_available():
    global _TRACKER_OK
    if _TRACKER_OK is None:
        _TRACKER_OK = os.path.isdir(os.path.join(BEADS_DB, "embeddeddolt")) or \
                      os.path.isdir(os.path.join(BEADS_DB, "dolt"))
    return _TRACKER_OK

# In-memory mirror of the tracker kv store: loaded once, flushed once.
_KV = {}
_KV_LOADED = False
_KV_DIRTY = set()

def kv_load():
    global _KV_LOADED
    if _KV_LOADED:
        return _KV
    _KV_LOADED = True
    if not tracker_available():
        return _KV
    out = _bd("kv", "list", timeout=8)
    if not out:
        return _KV
    for line in out.splitlines():
        line = line.rstrip()
        for sep in (" = ", ": ", "\t"):
            if sep in line:
                key, val = line.split(sep, 1)
                break
        else:
            continue
        key, val = key.strip(), val.strip()
        if not key:
            continue
        try:
            _KV[key] = json.loads(val)
        except Exception:
            _KV[key] = val
    return _KV

def cache_get(key):
    return kv_load().get(key)

def cache_set(key, value):
    kv_load()[key] = value
    _KV_DIRTY.add(key)

def kv_flush():
    if not _KV_DIRTY or not tracker_available():
        return
    for key in _KV_DIRTY:
        v = _KV.get(key)
        _bd("kv", "set", key, v if isinstance(v, str) else json.dumps(v), timeout=6)
    _KV_DIRTY.clear()

def load_verdicts():
    """Manual mark-done / snooze flags from kv. {sid: {'dismissed':bool,'snooze_until':epoch|None}}."""
    verdicts = {}
    for key, val in kv_load().items():
        if not key.startswith("session:"):
            continue
        parts = key.split(":")  # session:<sid>:<field>
        if len(parts) != 3:
            continue
        _, sid, field = parts
        v = verdicts.setdefault(sid, {"dismissed": False, "snooze_until": None})
        if field == "dismissed":
            v["dismissed"] = str(val).lower() in ("1", "true", "yes")
        elif field == "snooze-until":
            try:
                v["snooze_until"] = float(val)
            except (ValueError, TypeError):
                pass
    return verdicts

def load_bead_links():
    """Map sessionId -> list of (beadId, status) from tracker beads' metadata.sessionIds.
    One bulk call. Tracker beads are the link records; team repo beads are referenced
    only via their ids in metadata (status fetched lazily elsewhere if needed)."""
    links = {}
    if not tracker_available():
        return links
    out = _bd("list", "--json", "--status", "all", timeout=10) or _bd("list", "--json", timeout=10)
    if not out:
        return links
    try:
        beads = json.loads(out)
    except Exception:
        return links
    for b in beads:
        meta = b.get("metadata") or {}
        sids = meta.get("sessionIds") or ([meta["sessionId"]] if meta.get("sessionId") else [])
        for sid in sids:
            links.setdefault(sid, []).append((b.get("id"), b.get("status")))
    return links

# ----------------------------------------------------------------------------- helpers

def _age(secs):
    secs = max(0, int(secs))
    if secs < 90:        return f"{secs}s"
    if secs < 5400:      return f"{secs // 60}m"
    if secs < 172800:    return f"{secs // 3600}h"
    return f"{secs // 86400}d"

def _short(path):
    if not path: return "?"
    return path.replace(HOME, "~", 1) if path.startswith(HOME) else path

def _parse_since(s):
    """'24h' '30m' '7d' '90s' -> seconds. Bare number = hours."""
    if not s: return None
    s = s.strip().lower()
    mult = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    if s[-1] in mult:
        try: return float(s[:-1]) * mult[s[-1]]
        except ValueError: return None
    try: return float(s) * 3600
    except ValueError: return None

def _pid_alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except (ProcessLookupError, ValueError):
        return False
    except PermissionError:
        return True  # exists, owned by someone else

def _pid_is_claude(pid):
    try:
        out = subprocess.run(["ps", "-p", str(pid), "-o", "command="],
                             capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return False
    return "claude" in out.lower()

# ----------------------------------------------------------------------------- heartbeats

def scan_heartbeats():
    """Return (live_ids:set, crashed:dict[sid]=hb). live wins over crashed."""
    live, crashed = set(), {}
    for hb in glob.glob(os.path.join(SESSIONS_DIR, "*.json")):
        try:
            d = json.load(open(hb))
        except Exception:
            continue
        pid, sid = d.get("pid"), d.get("sessionId")
        if not sid:
            continue
        if pid is not None and _pid_alive(pid) and _pid_is_claude(pid):
            live.add(sid)
        else:
            crashed.setdefault(sid, d)
    for sid in list(crashed):
        if sid in live:
            del crashed[sid]  # was resumed -> now live
    return live, crashed

# ----------------------------------------------------------------------------- transcripts

WRAPPER_PREFIXES = ("<local-command", "<command-name", "<command-message",
                    "<command-args", "caveat:", "<system-reminder", "<bash-input",
                    "<bash-stdout", "base directory for this skill", "<task-notification",
                    "<task-id", "<tool-use-id", "<output-file", "<user-prompt-submit-hook")

def _is_wrapper(text):
    t = text.lstrip().lower()
    return t.startswith(WRAPPER_PREFIXES)

def _extract_text(msg):
    if not isinstance(msg, dict):
        return ""
    c = msg.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        parts = []
        for x in c:
            if isinstance(x, dict) and isinstance(x.get("text"), str):
                parts.append(x["text"])
        return " ".join(parts)
    return ""

_CWD_RE = __import__("re").compile(r'"cwd"\s*:\s*"((?:[^"\\]|\\.)*)"')

def _tail_cwd(path, nbytes=131072):
    """Authoritative cwd = the LAST cwd written (resumed sessions can carry an
    older cwd at the head). Read only the file tail to stay O(1)."""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - nbytes))
            blob = f.read().decode("utf-8", "ignore")
    except OSError:
        return None
    matches = _CWD_RE.findall(blob)
    if not matches:
        return None
    try:
        return json.loads('"' + matches[-1] + '"')  # unescape
    except Exception:
        return matches[-1]

def parse_meta(path, headlines=600):
    """Fast: title + first real prompt + cwd. cwd prefers the file tail (last/current),
    falling back to the first cwd seen in the head."""
    title = first_prompt = cwd_head = None
    try:
        f = open(path, errors="ignore")
    except OSError:
        return {"cwd": None, "label": "(unreadable)"}
    with f:
        for i, line in enumerate(f):
            if i > headlines:
                break
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            if cwd_head is None and isinstance(d.get("cwd"), str):
                cwd_head = d["cwd"]
            if d.get("type") == "ai-title" and d.get("aiTitle"):
                title = d["aiTitle"]
            msg = d.get("message") if isinstance(d.get("message"), dict) else None
            role = (msg or {}).get("role") or d.get("role")
            if role == "user" and first_prompt is None:
                txt = _extract_text(msg or d)
                if txt and not _is_wrapper(txt):
                    first_prompt = " ".join(txt.split())[:200]
            if title and first_prompt:
                break
    cwd = _tail_cwd(path) or cwd_head
    return {"cwd": cwd, "label": title or first_prompt or "(no title)"}

def content_has(path, q):
    """Stream the whole transcript looking for substring q (case-insensitive)."""
    q = q.lower()
    try:
        with open(path, errors="ignore") as f:
            for line in f:
                if q in line.lower():
                    return True
    except OSError:
        pass
    return False

def transcript_files():
    """All real session transcripts: projects/<enc>/<uuid>.jsonl (no subagents)."""
    return glob.glob(os.path.join(PROJECTS_DIR, "*", "*.jsonl"))

def index_by_id():
    return {os.path.splitext(os.path.basename(p))[0]: p for p in transcript_files()}

# ----------------------------------------------------------------------------- git / PR signals

def _git(cwd, *args, timeout=6):
    return _run(["git", "-C", cwd, *args], timeout=timeout)

def git_context(cwd):
    """(repo_root, main_repo_key, branch, head_sha) or None if cwd isn't a git repo.
    main_repo_key folds worktrees onto the shared repo via --git-common-dir.
    Two git calls: one for toplevel+common+sha, one for the branch name."""
    out = _git(cwd, "rev-parse", "--show-toplevel", "--git-common-dir", "HEAD")
    if not out:
        return None
    parts = out.strip().splitlines()
    if len(parts) < 3:
        return None
    root, common_raw, head = parts[0].strip(), parts[1].strip(), parts[2].strip()
    common = os.path.abspath(os.path.join(cwd, common_raw)) if common_raw else root
    branch = (_git(cwd, "rev-parse", "--abbrev-ref", "HEAD") or "").strip() or None
    return root, common, branch, head

def is_dirty(cwd, head_sha):
    """dirty = staged/modified TRACKED files OR commits ahead of upstream.
    Untracked-only does NOT count. Cached by cwd@head_sha (self-invalidates on new commits)."""
    key = f"cache:dirty:{cwd}@{head_sha}"
    cached = cache_get(key)
    if isinstance(cached, dict) and "dirty" in cached:
        return cached["dirty"]
    porcelain = _git(cwd, "status", "--porcelain", "--untracked-files=no")
    dirty = bool(porcelain and porcelain.strip())
    if not dirty:
        # commits ahead of upstream?
        ahead = _git(cwd, "rev-list", "--count", "@{upstream}..HEAD")
        if ahead and ahead.strip().isdigit():
            dirty = int(ahead.strip()) > 0
    cache_set(key, {"dirty": dirty})
    return dirty

def branch_pr_state(main_key, repo_root, branch):
    """{'merged':bool,'branch_gone':bool,'pr_number':int|None,'pr_state':str}.
    pr_state in open/merged/closed/none/unknown. Deduped+cached per (repo,branch)."""
    if not branch or branch == "HEAD":
        return {"merged": False, "branch_gone": False, "pr_number": None, "pr_state": "none"}
    key = f"cache:branch:{main_key}@{branch}"
    cached = cache_get(key)
    if isinstance(cached, dict):
        terminal = cached.get("merged") or cached.get("pr_state") in ("merged", "closed") or cached.get("branch_gone")
        fresh = (time.time() - cached.get("checked_at", 0)) < NONTERMINAL_TTL
        if terminal or fresh:
            return cached
    res = {"merged": False, "branch_gone": False, "pr_number": None, "pr_state": "none",
           "checked_at": time.time()}
    # branch merged into the repo's default branch (origin/HEAD), via ancestor check (local, no net)
    if _ancestor(repo_root, branch, "origin/HEAD"):
        res["merged"] = True
    if not ALLOW_NETWORK:
        # cache-only mode: don't spend a network gh call during interactive list
        res["pr_state"] = "unknown"
        cache_set(key, res)
        return res
    # PR state via gh (cached; never let a gh failure mark a session done)
    out = _run(["gh", "pr", "list", "--head", branch, "--state", "all",
                "--json", "number,state", "--limit", "1"], timeout=12, cwd=repo_root)
    if out is not None:
        try:
            prs = json.loads(out)
            if prs:
                res["pr_number"] = prs[0].get("number")
                res["pr_state"] = (prs[0].get("state") or "").lower() or "none"
            else:
                res["pr_state"] = "none"
        except Exception:
            res["pr_state"] = "unknown"
    else:
        res["pr_state"] = "unknown"
    cache_set(key, res)
    return res

# ----------------------------------------------------------------------------- scoring

def score_row(row, now):
    """Ordered first-match rule list -> (bucket, reason). See plan.
    row carries: cwd_exists, mtime, crashed, dirty, beads (list of (id,status)),
    pr_state, pr_number, branch_merged, branch_gone, snooze_until, dismissed."""
    age = now - row["mtime"]
    d7, d2 = 7 * 86400, 2 * 86400
    # manual overrides first
    if row.get("dismissed"):
        return "done", "marked done"
    su = row.get("snooze_until")
    if su and su > now:
        return "snoozed", f"snoozed {_age(su - now)}"
    # rule list
    if not row["cwd_exists"]:
        return "done", "dir deleted — can't resume"
    if row.get("dirty"):
        return "high", "uncommitted/unpushed work"
    if row["crashed"] and age < d7:
        return "high", "crashed mid-work"
    beads = row.get("beads") or []
    open_bead = next((bid for bid, st in beads if st and st != "closed"), None)
    closed_bead = next((bid for bid, st in beads if st == "closed"), None)
    pr_state = row.get("pr_state")
    if open_bead and pr_state in (None, "none", "open", "unknown") and age < d7:
        return "high", f"open work item ({open_bead})"
    if pr_state == "merged":
        return "done", f"PR #{row.get('pr_number')} merged"
    if closed_bead:
        return "done", f"bead {closed_bead} closed"
    if row.get("branch_merged") or row.get("branch_gone"):
        return "done", "branch merged/gone"
    if (open_bead or pr_state == "open") and age >= d7:
        return "medium", "open work, idle"
    if row["crashed"]:
        return "medium", "old crash"
    if age < d2:
        return "medium", "recent, status unknown"
    return "low", "stale, no open work"

# ----------------------------------------------------------------------------- core query

def collect(category="all", cwd_filter=None, since=None, query=None, compute=True):
    live, crashed = scan_heartbeats()
    now = time.time()
    since_secs = _parse_since(since) if since else None
    verdicts = load_verdicts()
    links = load_bead_links()
    rows = []
    for path in transcript_files():
        sid = os.path.splitext(os.path.basename(path))[0]
        if sid in live:
            continue  # already open
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        is_crashed = sid in crashed

        # cheap pre-filters
        if category == "crashed" and not is_crashed:
            continue
        if category == "recent" and since_secs and (now - mtime) > since_secs:
            continue

        info = parse_meta(path)
        scwd = info["cwd"] or (crashed.get(sid, {}).get("cwd"))

        if category == "here":
            if not scwd:
                continue
            base = os.path.abspath(cwd_filter or os.getcwd())
            if not (scwd == base or scwd.startswith(base + os.sep)):
                continue

        q = (query or "").strip().lower()
        score = 0
        if q:
            label_l = (info["label"] or "").lower()
            cwd_l = (scwd or "").lower()
            if q in label_l:      score = 3   # title hit — most relevant
            elif q in cwd_l:      score = 2   # folder hit
            elif content_has(path, q): score = 1   # mentioned somewhere in transcript
            else:
                continue

        v = verdicts.get(sid, {})
        rows.append({
            "id": sid,
            "cwd": scwd,
            "cwd_exists": bool(scwd) and os.path.isdir(scwd),
            "mtime": mtime,
            "age": _age(now - mtime),
            "label": info["label"],
            "crashed": is_crashed,
            "score": score,
            "beads": links.get(sid, []),
            "dismissed": v.get("dismissed", False),
            "snooze_until": v.get("snooze_until"),
            "dirty": False, "branch": None, "branch_merged": False,
            "branch_gone": False, "pr_state": None, "pr_number": None,
        })

    # Pass B — git/PR signals for candidates only, deduped by cwd and (repo,branch).
    # Bounded: most-recent first, within PASSB_WINDOW, at most PASSB_MAX_DIRS distinct dirs.
    if compute:
        ctx = {}
        dirty_owned = set()   # only the most-recent session per cwd owns "uncommitted work"
        for r in sorted(rows, key=lambda r: r["mtime"], reverse=True):
            if not r["cwd_exists"] or (now - r["mtime"]) > PASSB_WINDOW:
                continue
            cwd = r["cwd"]
            if cwd not in ctx:
                if len(ctx) >= PASSB_MAX_DIRS:
                    continue  # budget spent; older/over-cap dirs get cheap scoring
                ctx[cwd] = git_context(cwd)
            gc = ctx.get(cwd)
            if not gc:
                continue
            root, common, branch, head = gc
            r["branch"] = branch
            # dirty is a directory-state signal — attribute it only to the latest
            # session in that dir (the one whose resume continues the in-flight work).
            if head and cwd not in dirty_owned:
                dirty_owned.add(cwd)
                r["dirty"] = is_dirty(cwd, head)
            bp = branch_pr_state(common, root, branch)
            r["branch_merged"] = bp.get("merged", False)
            r["branch_gone"] = bp.get("branch_gone", False)
            r["pr_state"] = bp.get("pr_state")
            r["pr_number"] = bp.get("pr_number")

    for r in rows:
        r["bucket"], r["reason"] = score_row(r, now)

    if query and query.strip():
        rows.sort(key=lambda r: (r["score"], r["mtime"]), reverse=True)
    else:
        rows.sort(key=lambda r: (BUCKET_RANK.get(r["bucket"], 9), -r["mtime"]))
    kv_flush()
    return rows, crashed

# ----------------------------------------------------------------------------- output

_BUCKET_BADGE = {"high": "▲ high", "medium": "● med", "low": "· low",
                 "snoozed": "⏸ snze", "done": "✓ done"}

def _visible(rows, a):
    """Apply the hide-done/snoozed default unless the user opted in."""
    if getattr(a, "include_done", False) or a.category == "done":
        return rows
    return [r for r in rows if r["bucket"] not in ("done", "snoozed")]

def cmd_list(a):
    fast = getattr(a, "fast", False)
    rows, _ = collect(a.category, a.cwd, a.since, a.query, compute=not fast)
    if a.category == "done":
        rows = [r for r in rows if r["bucket"] in ("done", "snoozed")]
    else:
        rows = _visible(rows, a)
    if a.format == "json":
        print(json.dumps(rows, indent=2))
        return 0
    if a.format == "fzf":
        for r in rows:
            badge = _BUCKET_BADGE.get(r["bucket"], r["bucket"])
            print(f"{r['id']}\t{badge:<7} {r['age']:>4}  {_short(r['cwd']):<46} "
                  f"{r['label']}  — {r['reason']}")
        return 0
    # human table
    if not rows:
        if a.category == "crashed":
            print("No crash-confirmed sessions right now "
                  "(stale heartbeats may have been cleared on restart). "
                  "Try: list --category recent  or  --category all")
        else:
            print("No sessions worth resuming (done/noise hidden). "
                  "Use --include-done or `--category done` to see them.")
        return 0
    total = len(rows)
    shown = rows if a.limit in (0, None) else rows[:a.limit]
    print(f"{'#':>3}  {'bucket':<7} {'when':>5}  {'directory':<42}  title — why")
    print("-" * 104)
    for i, r in enumerate(shown, 1):
        badge = _BUCKET_BADGE.get(r["bucket"], r["bucket"])
        print(f"{i:>3}  {badge:<7} {r['age']:>5}  {_short(r['cwd'])[:42]:<42}  "
              f"{r['label'][:46]} — {r['reason']}")
    if total > len(shown):
        print(f"\n… showing {len(shown)} of {total}. Narrow with --query <kw>, --since <12h>, "
              f"or raise --limit. (fzf picker shows all.)")
    else:
        print(f"\n{total} session(s).  open with `launch --ids <id,...>`")
    return 0

def cmd_preview(a):
    idx = index_by_id()
    path = idx.get(a.id)
    if not path:
        print(f"(no transcript for {a.id})")
        return 0
    info = parse_meta(path)
    cwd = info["cwd"]
    print(f"title : {info['label']}")
    print(f"cwd   : {_short(cwd)}{'' if cwd and os.path.isdir(cwd) else '  ⚠ missing'}")
    print(f"id    : {a.id}")
    # cheap per-session scoring for the preview pane
    try:
        now = time.time()
        _, crashed = scan_heartbeats()
        v = load_verdicts().get(a.id, {})
        row = {"id": a.id, "cwd": cwd, "cwd_exists": bool(cwd) and os.path.isdir(cwd),
               "mtime": os.path.getmtime(path), "crashed": a.id in crashed,
               "beads": load_bead_links().get(a.id, []),
               "dismissed": v.get("dismissed", False), "snooze_until": v.get("snooze_until"),
               "dirty": False, "branch": None, "branch_merged": False,
               "branch_gone": False, "pr_state": None, "pr_number": None}
        if row["cwd_exists"]:
            gc = git_context(cwd)
            if gc:
                root, common, branch, head = gc
                row["branch"] = branch
                if head:
                    row["dirty"] = is_dirty(cwd, head)
                bp = branch_pr_state(common, root, branch)
                row.update({"branch_merged": bp.get("merged", False),
                            "pr_state": bp.get("pr_state"), "pr_number": bp.get("pr_number")})
                kv_flush()
        bucket, reason = score_row(row, now)
        extra = []
        if row["branch"]:    extra.append(f"branch={row['branch']}")
        if row["pr_state"] and row["pr_state"] != "none": extra.append(f"PR={row['pr_state']}")
        if row["beads"]:     extra.append("bead=" + ",".join(b for b, _ in row["beads"]))
        print(f"signal: {bucket.upper()} — {reason}" + (f"   ({'; '.join(extra)})" if extra else ""))
    except Exception:
        pass
    print("-" * 60)
    # last N messages
    msgs = []
    try:
        for line in open(path, errors="ignore"):
            try:
                d = json.loads(line)
            except Exception:
                continue
            msg = d.get("message") if isinstance(d.get("message"), dict) else None
            role = (msg or {}).get("role") or d.get("role")
            if role in ("user", "assistant"):
                txt = _extract_text(msg or d).strip()
                if txt and not _is_wrapper(txt):
                    msgs.append((role, " ".join(txt.split())))
    except OSError:
        pass
    for role, txt in msgs[-a.n:]:
        marker = ">" if role == "user" else "⏺"
        print(f"{marker} {txt[:280]}")
    return 0

# ----------------------------------------------------------------------------- launch

TERMINALS = ("iterm", "ghostty")

def _detect_terminal():
    """Return the terminal to drive: one of TERMINALS.

    CLAUDE_SESSION_TERMINAL is honoured when it names a supported terminal,
    ignoring case and surrounding space, and is a hard error otherwise so a
    typo cannot quietly drive the wrong app. Unset, a Ghostty host shell is
    recognised by TERM_PROGRAM or GHOSTTY_RESOURCES_DIR; anything else is
    iTerm2. fork-tab.sh applies the same rule.
    """
    raw = os.environ.get("CLAUDE_SESSION_TERMINAL", "")
    override = raw.strip().lower()
    if override in TERMINALS:
        return override
    if override:
        print(f"error: CLAUDE_SESSION_TERMINAL={raw!r} is not a supported terminal "
              f"(use {' or '.join(TERMINALS)})", file=sys.stderr)
        raise SystemExit(2)
    if (os.environ.get("TERM_PROGRAM", "").lower() == "ghostty"
            or os.environ.get("GHOSTTY_RESOURCES_DIR")):
        return "ghostty"
    return "iterm"

def _app_name(terminal):
    return "Ghostty" if terminal == "ghostty" else "iTerm"

def _run_osascript(script, terminal):
    """Run `script`, reporting failure on stderr. Returns True when it succeeded.

    osascript echoes the value of the last statement — a window or tab
    specifier — so its stdout is dropped to keep our own one-line output clean.
    Errors go to stderr and are surfaced rather than swallowed.
    """
    r = subprocess.run(["osascript", "-e", script], check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    if r.returncode == 0:
        return True
    detail = (r.stderr or "").strip() or f"osascript exited {r.returncode}"
    print(f"error: could not drive {_app_name(terminal)}: {detail}", file=sys.stderr)
    if terminal == "ghostty":
        print("Ghostty needs 1.3+ for AppleScript support; export "
              "CLAUDE_SESSION_TERMINAL=iterm to use iTerm2 instead.", file=sys.stderr)
    return False

def _as_quote(text):
    """Escape for embedding inside an AppleScript double-quoted string."""
    return text.replace("\\", "\\\\").replace('"', '\\"')

# A terminal that is not already running is launched by `activate` from this
# process and inherits its environment, so the new tab would hand the resumed
# session the parent's Claude Code markers. CLAUDE_CODE_CHILD_SESSION alone
# turns off transcript saving and peer registration.
SCRUB_ENV = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_CHILD_SESSION",
             "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_MESSAGING_SOCKET",
             "CLAUDE_CODE_MESSAGING_TOKEN", "CLAUDE_PID", "CLAUDE_SESSION_TERMINAL")

def _resume_cmd(cwd, sid):
    scrub = " ".join(f"-u {v}" for v in SCRUB_ENV)
    return f"cd {shlex.quote(cwd)} && env {scrub} {shlex.quote(CLAUDE)} --resume {sid}"

def _applescript_ghostty(pairs):
    """pairs: list of (cwd, id) -> AppleScript opening one Ghostty window, a tab each.

    `initial input` is delivered to each new surface as it starts; the trailing
    linefeed is what submits the command rather than leaving it at the prompt.
    """
    lines = ['tell application "Ghostty"', '  activate']
    for i, (cwd, sid) in enumerate(pairs):
        lines += ['  set cfg to new surface configuration',
                  f'  set initial working directory of cfg to "{_as_quote(cwd)}"',
                  f'  set initial input of cfg to "{_as_quote(_resume_cmd(cwd, sid))}" & linefeed']
        lines += ['  set w to new window with configuration cfg'] if i == 0 else \
                 ['  new tab in w with configuration cfg']
    lines += ['end tell']
    return "\n".join(lines)

def _applescript_iterm(pairs):
    """pairs: list of (cwd, id) -> AppleScript opening one iTerm window, a tab each."""
    lines = ['tell application "iTerm"', '  activate',
             '  set w to (create window with default profile)', '  tell w']
    for i, (cwd, sid) in enumerate(pairs):
        cmd_as = _as_quote(_resume_cmd(cwd, sid))
        if i == 0:
            lines += ['    tell current session of current tab',
                      f'      write text "{cmd_as}"', '    end tell']
        else:
            lines += ['    set t to (create tab with default profile)',
                      '    tell current session of t',
                      f'      write text "{cmd_as}"', '    end tell']
    lines += ['  end tell', 'end tell']
    return "\n".join(lines)

def _applescript_for(pairs, terminal):
    return (_applescript_ghostty if terminal == "ghostty" else _applescript_iterm)(pairs)

def resolve(ids):
    """Return (pairs, skipped) where pairs=[(cwd,id)] with an existing dir."""
    idx = index_by_id()
    pairs, skipped = [], []
    for sid in ids:
        path = idx.get(sid)
        cwd = parse_meta(path)["cwd"] if path else None
        if cwd and os.path.isdir(cwd):
            pairs.append((cwd, sid))
        else:
            skipped.append((sid, cwd))
    return pairs, skipped

def launch(ids, terminal="auto"):
    pairs, skipped = resolve(ids)
    for sid, cwd in skipped:
        where = f" (dir gone: {_short(cwd)})" if cwd else " (no cwd found)"
        print(f"⚠ skipped {sid[:8]}{where}", file=sys.stderr)
    if not pairs:
        print("Nothing to open.", file=sys.stderr)
        return 1
    if terminal == "print":
        for cwd, sid in pairs:
            print(_resume_cmd(cwd, sid))
        return 0
    if terminal == "auto":
        terminal = _detect_terminal()
    if not _run_osascript(_applescript_for(pairs, terminal), terminal):
        print("Resume them by hand instead:", file=sys.stderr)
        for cwd, sid in pairs:
            print(_resume_cmd(cwd, sid))
        return 1
    print(f"Opened {len(pairs)} tab(s) in a new {_app_name(terminal)} window.")
    return 0

def cmd_launch(a):
    ids = [x for x in (a.ids or "").split(",") if x.strip()]
    if not ids:
        print("no --ids given", file=sys.stderr)
        return 1
    return launch([i.strip() for i in ids], a.terminal)

# ----------------------------------------------------------------------------- pick (fzf)

def _open_terminal_running(cmd, terminal="auto"):
    """Open `cmd` in a new terminal window. Returns True when it succeeded."""
    if terminal == "auto":
        terminal = _detect_terminal()
    cmd_as = _as_quote(cmd)
    if terminal == "ghostty":
        script = ('tell application "Ghostty"\n  activate\n'
                  '  set cfg to new surface configuration\n'
                  f'  set initial input of cfg to "{cmd_as}" & linefeed\n'
                  '  new window with configuration cfg\nend tell')
    else:
        script = ('tell application "iTerm"\n  activate\n'
                  '  set w to (create window with default profile)\n'
                  '  tell w\n    tell current session of current tab\n'
                  f'      write text "{cmd_as}"\n    end tell\n  end tell\nend tell')
    return _run_osascript(script, terminal)

def cmd_pick(a):
    if not _which("fzf"):
        print("fzf not found; use the native flow (`/recover --native`).", file=sys.stderr)
        return 2
    cwd = a.cwd or os.getcwd()
    # fzf needs a real TTY. If we're not on one (e.g. invoked by the slash command),
    # relaunch the picker inside its own terminal window.
    if not sys.stdout.isatty():
        relaunch = (f"{shlex.quote(PY)} {shlex.quote(SELF)} pick --cwd {shlex.quote(cwd)}"
                    f" --terminal {a.terminal}")
        if not _open_terminal_running(relaunch, a.terminal):
            print(f"Run the picker yourself instead:\n  {relaunch}", file=sys.stderr)
            print("Or use the in-chat flow (`/recover --native`).", file=sys.stderr)
            return 1
        print("Opened a session picker in a new terminal window — choose sessions there "
              "(type to search · TAB multi-select · ⏎ to open).")
        return 0
    base = f"{shlex.quote(PY)} {shlex.quote(SELF)} list --format fzf"
    reload_all     = f"{base} --category all"
    reload_crashed = f"{base} --category crashed"
    reload_here    = f"{base} --category here --cwd {shlex.quote(cwd)}"
    reload_done    = f"{base} --category done"
    preview_cmd    = f"{shlex.quote(PY)} {shlex.quote(SELF)} preview --id {{1}}"
    fzf = [
        "fzf", "--multi", "--ansi", "--delimiter", "\t", "--with-nth", "2..",
        "--height", "100%", "--border", "--reverse",
        "--header", ("TAB pick · ⏎ open · ^X mark-done · ^C crashed · ^F folder · "
                     "^A all · ^D done · ESC cancel"),
        "--preview", preview_cmd, "--preview-window", "right,55%,wrap",
        "--bind", f"ctrl-c:reload({reload_crashed})",
        "--bind", f"ctrl-f:reload({reload_here})",
        "--bind", f"ctrl-a:reload({reload_all})",
        "--bind", f"ctrl-d:reload({reload_done})",
        "--bind", f"ctrl-x:execute-silent({shlex.quote(PY)} {shlex.quote(SELF)} mark-done {{1}})+reload({reload_all})",
    ]
    src = subprocess.run([PY, SELF, "list", "--format", "fzf", "--category", "all"],
                         capture_output=True, text=True).stdout
    if not src.strip():
        print("No recoverable sessions found.")
        return 0
    sel = subprocess.run(fzf, input=src, capture_output=True, text=True)
    if sel.returncode not in (0,):  # 130 = ESC/cancel
        print("Cancelled.")
        return 0
    ids = [ln.split("\t", 1)[0] for ln in sel.stdout.splitlines() if ln.strip()]
    if not ids:
        print("Nothing selected.")
        return 0
    return launch(ids, a.terminal)

def _which(b):
    return subprocess.run(["bash", "-lc", f"command -v {b}"],
                          capture_output=True, text=True).returncode == 0

# ----------------------------------------------------------------------------- verdicts / refresh

def _need_tracker():
    if not tracker_available():
        print(f"tracker DB not found at {BEADS_DB}. Run: "
              f"(cd ~/.claude/work-tracker && bd init --prefix wt --non-interactive)", file=sys.stderr)
        return False
    return True

def cmd_mark_done(a):
    if not _need_tracker():
        return 1
    for sid in a.ids.split(","):
        sid = sid.strip()
        if sid:
            _bd("kv", "set", f"session:{sid}:dismissed", "true")
            print(f"✓ {sid[:8]} marked done (hidden from /recover)")
    return 0

def cmd_keep(a):
    if not _need_tracker():
        return 1
    for sid in a.ids.split(","):
        sid = sid.strip()
        if sid:
            _bd("kv", "clear", f"session:{sid}:dismissed")
            _bd("kv", "clear", f"session:{sid}:snooze-until")
            print(f"↺ {sid[:8]} restored (done/snooze cleared)")
    return 0

def cmd_snooze(a):
    if not _need_tracker():
        return 1
    secs = _parse_since(a.until.lstrip("+"))
    if not secs:
        print(f"bad --until '{a.until}' (use e.g. +3d, +12h, +1w)", file=sys.stderr)
        return 1
    until = time.time() + secs
    for sid in a.ids.split(","):
        sid = sid.strip()
        if sid:
            _bd("kv", "set", f"session:{sid}:snooze-until", str(until))
            print(f"⏸ {sid[:8]} snoozed for {_age(secs)}")
    return 0

def cmd_refresh(a):
    """Populate the git/PR cache with live network checks (gh)."""
    global ALLOW_NETWORK
    ALLOW_NETWORK = True
    rows, _ = collect(a.category, a.cwd, a.since, None, compute=True)
    n = sum(1 for r in rows if r["pr_state"] not in (None, "unknown"))
    print(f"Refreshed signals for {len(rows)} session(s); {n} with resolved PR state. Cache warmed.")
    return 0

# ----------------------------------------------------------------------------- cli

def main():
    p = argparse.ArgumentParser(prog="claude-session-recover")
    sub = p.add_subparsers(dest="cmd", required=True)

    pl = sub.add_parser("list")
    pl.add_argument("--category", default="all",
                    choices=["crashed", "here", "recent", "all", "search", "done"])
    pl.add_argument("--cwd", default=None)
    pl.add_argument("--since", default="24h")
    pl.add_argument("--query", default=None)
    pl.add_argument("--format", default="table", choices=["table", "json", "fzf"])
    pl.add_argument("--limit", type=int, default=40, help="cap table rows (0 = all)")
    pl.add_argument("--include-done", action="store_true", help="show done/snoozed too")
    pl.add_argument("--fast", action="store_true", help="skip git/PR signals (cheap only)")
    pl.set_defaults(func=cmd_list)

    pp = sub.add_parser("preview")
    pp.add_argument("--id", required=True)
    pp.add_argument("-n", type=int, default=6)
    pp.set_defaults(func=cmd_preview)

    pla = sub.add_parser("launch")
    pla.add_argument("--ids", required=True)
    pla.add_argument("--terminal", default="auto",
                     choices=["auto", *TERMINALS, "print"])
    pla.set_defaults(func=cmd_launch)

    pk = sub.add_parser("pick")
    pk.add_argument("--cwd", default=None)
    pk.add_argument("--terminal", default="auto", choices=["auto", *TERMINALS])
    pk.set_defaults(func=cmd_pick)

    pm = sub.add_parser("mark-done")
    pm.add_argument("ids", help="comma-separated session ids")
    pm.set_defaults(func=cmd_mark_done)

    pkp = sub.add_parser("keep")
    pkp.add_argument("ids", help="comma-separated session ids")
    pkp.set_defaults(func=cmd_keep)

    psn = sub.add_parser("snooze")
    psn.add_argument("ids", help="comma-separated session ids")
    psn.add_argument("--until", default="+7d", help="duration, e.g. +3d, +12h, +1w")
    psn.set_defaults(func=cmd_snooze)

    pr = sub.add_parser("refresh")
    pr.add_argument("--category", default="recent",
                    choices=["crashed", "here", "recent", "all", "search", "done"])
    pr.add_argument("--cwd", default=None)
    pr.add_argument("--since", default="14d")
    pr.set_defaults(func=cmd_refresh)

    a = p.parse_args()
    sys.exit(a.func(a))

if __name__ == "__main__":
    main()
