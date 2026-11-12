#!/opt/conda/bin/python3
"""RS's status bar for Claude Code (the `statusLine` command RS writes into an
interactive agent's user settings; staged root-owned at
/usr/local/lib/rs/rs-statusbar.py in every project container and agent box).

  Opus 5.5 · high │ ● cache: 56m (94% cached) · $307 (R: 67%, W: 21%, O: 12%) │ ctx 161k/1M (7 compactions)

- model, then its effort level (and "fast" when fast mode is on).
- cache: time left on the prompt cache (amber in the last fifth of its life), or
  "cold" once it has expired, then the session's cache hit rate: the share of
  all input tokens (main + subagents) that were read from the cache rather than
  written to it or sent uncached.
- $: session cost as Claude Code computes it, with the share of it spent on
  cache reads (R), cache writes (W) and output (O), from the token counts in the
  session's transcripts (main conversation + subagents).
- ctx: current context, grey / yellow from 50% / red from 80% of the window,
  followed by "(N compactions)" once the main conversation has been compacted.

A field Claude Code does not send drops only its own segment. Reads the status
line JSON on stdin; keeps a small per-session state file so each refresh parses
only the new part of the transcripts. Its twin for pi is rs-statusbar.ts.
"""
import glob
import json
import os
import re
import sys
import time

R, DIM, BOLD = "\033[0m", "\033[2m", "\033[1m"
CYAN, GREEN, YELLOW, RED, MAG = "\033[36m", "\033[32m", "\033[33m", "\033[31m", "\033[35m"
SEP = f" {DIM}│{R} "

# Anthropic's price multipliers relative to a base input token (the same for every current
# model): cache read 0.1x, 5-minute cache write 1.25x, 1-hour cache write 2x, output 5x.
# Only the RATIOS are used, to split the cost into shares; dollar amounts come from Claude Code.
W_READ, W_WRITE_5M, W_WRITE_1H, W_OUT, W_IN = 0.1, 1.25, 2.0, 5.0, 1.0

STATE_DIR = os.path.join(os.path.expanduser("~"), ".cache", "rs-statusbar")
KEYS = ("read", "w5m", "w1h", "out", "in")
STATE_VERSION = 2  # bump when the state gains a field, so old state files are re-read from scratch


def human(n):
    n = float(n)
    if n >= 1e6:
        s = f"{n / 1e6:.1f}".removesuffix(".0")
        return s + "M"
    if n >= 1e3:
        return f"{int(n // 1e3)}k"
    return str(int(n))


def money(x):
    return f"${x:,.0f}" if x >= 100 else f"${x:.2f}"


def usage_sums(transcript, state):
    """Add the usage of new transcript lines (main + subagents) to state['sums'], and count
    compactions of the main conversation in state['compactions']."""
    files = [transcript] + sorted(glob.glob(os.path.join(transcript[: -len(".jsonl")], "subagents", "*.jsonl")))
    offsets = state.setdefault("offsets", {})
    last_ids = state.setdefault("last_ids", {})
    sums = state.setdefault("sums", dict.fromkeys(KEYS, 0))
    for path in files:
        try:
            size = os.path.getsize(path)
        except OSError:
            continue
        off = offsets.get(path, 0)
        if size < off:  # file rewritten: cannot trust the running sums any more
            return False
        if size == off:
            continue
        with open(path, "rb") as fh:
            fh.seek(off)
            chunk = fh.read(size - off)
        end = chunk.rfind(b"\n")
        if end < 0:
            continue
        for line in chunk[: end + 1].splitlines():
            if path == transcript and b'"compact_boundary"' in line:
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if d.get("type") == "system" and d.get("subtype") == "compact_boundary":
                    state["compactions"] = state.get("compactions", 0) + 1
                continue
            if b'"usage"' not in line:
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            m = d.get("message")
            if d.get("type") != "assistant" or not isinstance(m, dict) or not m.get("usage"):
                continue
            # one API response is written as several lines (one per content block), same usage
            if m.get("id") == last_ids.get(path):
                continue
            last_ids[path] = m.get("id")
            u = m["usage"]
            cc = u.get("cache_creation") or {}
            w5, w1 = cc.get("ephemeral_5m_input_tokens"), cc.get("ephemeral_1h_input_tokens")
            if w5 is None and w1 is None:  # no TTL split reported: count as 5-minute writes
                w5, w1 = u.get("cache_creation_input_tokens") or 0, 0
            sums["read"] += u.get("cache_read_input_tokens") or 0
            sums["w5m"] += w5 or 0
            sums["w1h"] += w1 or 0
            sums["out"] += u.get("output_tokens") or 0
            sums["in"] += u.get("input_tokens") or 0
        offsets[path] = off + end + 1
    return True


def cached_pct(s):
    total = s["read"] + s["w5m"] + s["w1h"] + s["in"]
    return f"{DIM}({round(s['read'] * 100 / total)}% cached){R}" if total > 0 else ""


def shares(s):
    parts = {
        "R": s["read"] * W_READ,
        "W": s["w5m"] * W_WRITE_5M + s["w1h"] * W_WRITE_1H + s["in"] * W_IN,
        "O": s["out"] * W_OUT,
    }
    tot = sum(parts.values())
    if tot <= 0:
        return ""
    return f"{DIM}({', '.join(f'{k}: {round(v * 100 / tot)}%' for k, v in parts.items())}){R}"


def load_state(sid):
    try:
        with open(os.path.join(STATE_DIR, sid + ".json")) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def save_state(sid, state):
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        path = os.path.join(STATE_DIR, sid + ".json")
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w") as fh:
            json.dump(state, fh)
        os.replace(tmp, path)
    except OSError:
        pass


def main():
    try:
        d = json.load(sys.stdin)
    except ValueError:
        d = {}
    get = lambda *ks: _dig(d, ks)
    out = []

    # --- model ---------------------------------------------------------------
    model = get("model", "display_name") or get("model", "id") or "?"
    seg = f"{BOLD}{CYAN}{model}{R}"
    if get("effort", "level"):
        seg += f"{DIM} · {get('effort', 'level')}{R}"
    if get("fast_mode") is True:
        seg += f" {MAG}⚡fast{R}"
    out.append(seg)

    # --- session usage split (running sums kept in a per-session state file) ----
    sid = get("session_id") or ""
    cost = get("cost", "total_cost_usd")
    transcript = get("transcript_path")
    sums = None
    compactions = 0
    if re.fullmatch(r"[\w-]+", sid) and transcript and transcript.endswith(".jsonl"):
        state = load_state(sid)
        if state.get("version") != STATE_VERSION:
            state = {}
        state["version"] = STATE_VERSION
        if not usage_sums(transcript, state):  # transcript rewritten: start over
            state = {"version": STATE_VERSION}
            usage_sums(transcript, state)
        sums = state["sums"]
        compactions = state.get("compactions", 0)
        save_state(sid, state)

    # --- cache + session cost -------------------------------------------------
    pc = get("prompt_cache") or {}
    seg = []
    if pc.get("ttl"):
        left = (pc.get("expires_at") or 0) - int(time.time())
        if pc.get("warm") is True and left > 0:
            t = f"{left // 3600}h{left % 3600 // 60}m" if left >= 3600 else f"{left // 60}m" if left >= 60 else f"{left}s"
            win = {"1h": 3600, "5m": 300}.get(pc["ttl"], 0)
            c = YELLOW if win and left < win // 5 else GREEN  # amber in the last fifth of the TTL
            s = f"{c}● cache: {t}{R}"
        else:
            s = f"{RED}○ cache: cold{R}"
        hit = cached_pct(sums) if sums else ""
        seg.append(s + (" " + hit if hit else ""))
    if cost is not None:
        seg.append(f"{DIM}{money(cost)}{R}" + (" " + shares(sums) if sums else ""))
    if seg:
        out.append(f" {DIM}·{R} ".join(seg))

    # --- context --------------------------------------------------------------
    cw = get("context_window") or {}
    size, pct = cw.get("context_window_size"), cw.get("used_percentage")
    cu = cw.get("current_usage") or {}
    used = sum(cu.get(k) or 0 for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
    if pct is None and size:
        pct = used * 100 // size
    if pct is not None:
        c = RED if pct >= 80 else YELLOW if pct >= 50 else DIM
        seg = f"{c}ctx {human(used)}/{human(size)}{R}" if size else f"{c}ctx {pct}%{R}"
        if compactions:
            seg += f" {DIM}({compactions} compaction{'s' if compactions != 1 else ''}){R}"
        out.append(seg)

    sys.stdout.write(SEP.join(out))


def _dig(d, ks):
    for k in ks:
        if not isinstance(d, dict):
            return None
        d = d.get(k)
    return d


if __name__ == "__main__":
    main()
