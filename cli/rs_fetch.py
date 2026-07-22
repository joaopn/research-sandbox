#!/usr/bin/env python3
"""rs-fetch — stage agent work from the shared rs-gitea into a local clone.

Staged by the host into rs-fetch-ENABLED surfaces only (opt-in at box /
project creation — the box window's rs-fetch toggle, or the sandbox
workflow's create option; never baked, never universal). Runs as the
container user against the repo's ACTIVE consumer fork (per-consumer forks —
the owner comes from the staged wiring rows, steered by Management's
active-fork selector) using the READ-ONLY operator token staged at
``~/.dev-tokens/operator.token``
— it can fetch agent work; nothing it holds can write to gitea (the one-way
valve). The human's push credential is theirs alone and never involved here.

Usage:
    rs-fetch <repo> [<repo_path>]                      # list the fork's open PRs
    rs-fetch <repo> [<repo_path>] --pr N               # stage the whole PR
    rs-fetch <repo> [<repo_path>] --branch NAME        # stage a whole branch
    rs-fetch <repo> [<repo_path>] --pr N --commit SHA  # walk: ONE commit of the PR
    rs-fetch <repo> [<repo_path>] --branch NAME --commit SHA   # walk a branch

``repo_path`` defaults to the current directory and must be a git work tree
(the human's local clone of the real repo). The fetched work is applied as
STAGED changes — a 3-way patch application (``git apply --3way``) of exactly
the NEW commits' diff — with the agent's commit message(s) prefilled
(SCRUBBED, see below) into git's message file, so ``git commit`` opens ready
to edit. Staged work does NOT show in bare ``git diff``: review with ``git
diff --staged``. rs-fetch NEVER commits, and authorship is always the human's
— ``git apply`` carries no commit metadata at all, so the agent is
structurally neither author nor committer of anything this tool stages.

WHICH commits land is patch-id-based (``git cherry``): commits whose diff is
already in HEAD are skipped even though the local copies have different shas.
So fetching a PR's commits oldest-first (the WALK) stages exactly one
commit's delta per step, each prefilled with only its own message — while
jumping straight to a later commit (the SKIP) stages everything up to it as
one squash with the messages concatenated for editing. ``--commit`` always
requires the ``--pr``/``--branch`` locator: gitea refuses bare-SHA fetches by
default (uploadpack.allowReachableSHA1InWant), so the locator's branch is
fetched and the SHA resolved locally.

Ported from agentic-dev-sandbox's fetch-sandbox.py. The in-tool LLM review is
deliberately ABSENT: reviews run host-side in an ephemeral sandboxed container
and their verdicts live in the HOST ledger, surfaced on the webui Development
page ONLY — the SYSTEM never writes a verdict into any container (they must
not be agent-visible). The VERDICT_DIR seam below is a MANUAL human escape
hatch: rs-fetch surfaces a verdict file if the human deliberately staged one
themselves, and never blocks when none exists.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

# Lockstep with cli/gitea.py GITEA_CONTAINER / GITEA_INNER_PORT (this file is
# staged into containers and cannot import host modules). Main containers
# resolve the name via the project bridge's embedded DNS; boxes via the
# host-injected `--add-host rs-gitea:<ip>`.
GITEA_HOST = "rs-gitea"
GITEA_PORT = "3000"
GITEA_BASE = f"http://{GITEA_HOST}:{GITEA_PORT}"

# Lockstep with cli/gitea.py OPERATOR_USER / AGENT_USER_PREFIX.
OPERATOR_USER = "operator"
AGENT_USER_PREFIX = "agent-"

TOKEN_PATH = Path.home() / ".dev-tokens" / "operator.token"

# Verdict seam — a MANUAL human escape hatch only. The system NEVER writes
# here (verdicts stay host-side; the Development page is their surface): if
# the human deliberately copies a verdict to VERDICT_DIR/<repo>/<n>.json (a PR
# verdict) or VERDICT_DIR/<repo>/<full-sha>.json (a per-commit verdict — none
# are produced yet; the per-commit reviewer is future work, and the FULL-sha
# key is what keeps this seam forward-compatible with it), rs-fetch surfaces
# it beside the fetched diff; otherwise the absent-note prints and nothing
# blocks.
VERDICT_DIR = Path("/workspace/.rs-reviews")

# In-network API bound. Gitea is one bridge hop away: a healthy instance
# answers metadata GETs in ms and a down one refuses instantly — the bound only
# matters for a half-up gitea. 15s mirrors cli/gitea.py's API_TIMEOUT_S (the
# quick-call bound); at 5s a gitea mid-GC could false-fail an interactive call
# whose caller is a human who can just wait, at 60s a wedged gitea holds the
# terminal a full minute.
API_TIMEOUT_S = 15

# One page of open PRs. A single agent working a fork keeps nowhere near this
# many open at once; if it ever truncates the fix is pagination. Printed loud
# when hit (the no-silent-caps rule).
PR_LIST_LIMIT = 50

# Paths whose modification means the diff can execute code on the HUMAN's
# machine at build/open time (direnv, editor tasks, git hooks managers, build
# entrypoints). Ported verbatim from agentic-dev-sandbox fetch-sandbox.py.
# NOTE the hooks managers (.husky/, .pre-commit-config.yaml) execute at `git
# commit` time — and this tool's whole point is leaving work STAGED for a
# commit — so a hit here deserves the read-first warning more than ever.
AUTO_EXEC_PATHS = [
    ".envrc", ".vscode/", ".husky/", ".pre-commit-config.yaml", ".gitmodules",
    "package.json", "setup.py", "setup.cfg", "Makefile", "CMakeLists.txt",
    ".cargo/config.toml", ".eslintrc.js", ".prettierrc.js",
]

HEX_DIGITS = set("0123456789abcdefABCDEF")


# --- message scrub -----------------------------------------------------------
#
# The prefilled commit message is AGENT-AUTHORED text the human will push to
# the REAL GitHub repo, where GitHub re-interprets parts of it as COMMANDS: an
# issue reference the agent wrote against its own gitea fork RE-TARGETS to the
# real repo's issue of the same number ("Fixes #3" closes real issue #3 on
# push), and authorship trailers assert the very authorship this fetch exists
# to erase. Scope is the PI-decided STANDARD scrub:
#   * Co-authored-by / Signed-off-by trailer LINES are DROPPED — they ARE
#     authorship. Case-INSENSITIVE: GitHub's own UI emits "Co-authored-by".
#   * All three GitHub auto-close forms are NEUTRALIZED by breaking the
#     keyword-reference adjacency GitHub requires, keeping the reference
#     readable. Rewrite outputs:
#         "Fixes #3"                        -> "Fixes gitea issue 3"
#         "Closes octocat/repo#2"           -> "Closes octocat/repo issue 2"
#         "Resolves https://github.com/o/r/issues/3" -> "Resolves o/r issue 3"
#   * C0 control characters (except \n and \t) are stripped — terminal-escape
#     injection via a later `git log`.
#   * @mentions are deliberately NOT touched (PI decision: a bare @name only
#     pings if it happens to match a real GitHub account, and stripping
#     mangles emails and decorators).
_TRAILER_RE = re.compile(r"^\s*(?:co-authored-by|signed-off-by)\s*:.*$",
                         re.IGNORECASE | re.MULTILINE)
# GitHub's close-keyword vocabulary (close/fix/resolve + tenses), an optional
# colon, then one of the three reference forms. Alternation order matters:
# URL, then owner/repo#N, then bare #N (the specific before the general).
_AUTOCLOSE_RE = re.compile(
    r"\b(close[sd]?|fix(?:e[sd])?|resolve[sd]?):?\s+"
    r"(?:"
    r"https?://github\.com/([\w.-]+/[\w.-]+)/issues/(\d+)"  # 2,3: URL form
    r"|([\w.-]+/[\w.-]+)#(\d+)"                             # 4,5: owner/repo#N
    r"|#(\d+)"                                              # 6:   bare #N
    r")",
    re.IGNORECASE)
_C0_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def _autoclose_sub(m: re.Match) -> str:
    kw = m.group(1)
    if m.group(3):                                    # URL form
        return f"{kw} {m.group(2)} issue {m.group(3)}"
    if m.group(5):                                    # owner/repo#N
        return f"{kw} {m.group(4)} issue {m.group(5)}"
    return f"{kw} gitea issue {m.group(6)}"           # bare #N


def scrub_message(text: str) -> str:
    """The STANDARD scrub (see the block comment above) over a prefill
    message. Pure text -> text; used on the concatenated message exactly once,
    whichever apply path consumes it."""
    text = _TRAILER_RE.sub("", text)
    text = _AUTOCLOSE_RE.sub(_autoclose_sub, text)
    text = _C0_RE.sub("", text)
    # Trailer-stripping leaves blank runs; collapse them so the prefill reads
    # like a message, not a crime scene.
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip() + "\n"


def die(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(1)


# The staged non-secret wiring files (host-written; rows carry the repo's
# ACTIVE fork owner under `user` — per-consumer forks: the owner is no longer
# derivable from the repo name). TWO homes, cascaded at the ROW level:
#   * DEV_GITEA_JSON — the PROJECT wiring in the container workspace
#     (supervisors + the docker substrate; rows cover THIS project's own dev
#     consumers and are re-staged live on Management's active-fork change).
#   * BOX_WIRING_JSON — the GLOBAL fetch wiring staged into an rs-fetch-enabled
#     surface's home dir (every mirrored dev repo -> its active fork owner).
#     Boxes have no project wiring in their /workspace, and the docker
#     substrate's project file carries no rows — this file is what makes
#     cross-project fetch resolvable. Refreshes on box/project restart.
# The cascade is row-level, NOT file-level: the docker substrate HAS a
# workspace file (gitea_ip, `repos: []`), so stopping at the first readable
# file would never reach the global rows.
DEV_GITEA_JSON = Path("/workspace/.orchestrator/dev-gitea.json")
BOX_WIRING_JSON = Path.home() / ".dev-tokens" / "fetch-wiring.json"


def _wiring_rows(path: Path) -> list | None:
    """One staged wiring file's `repos` rows, or None when the file is absent/
    unreadable (the cascade treats unreadable as carrying no rows)."""
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    rows = data.get("repos") if isinstance(data, dict) else None
    return rows if isinstance(rows, list) else []


def fork_owner(repo: str) -> str:
    """The repo's ACTIVE consumer fork owner, from the staged wiring rows —
    the workspace file first (project containers; re-staged live on
    Management's active-fork change), then the home-dir global fetch wiring
    (rs-fetch-enabled boxes / the docker substrate). Dies with a remedy when
    neither carries a row — the per-repo `agent-<repo>` derivation is GONE."""
    ws_rows = _wiring_rows(DEV_GITEA_JSON)
    box_rows = _wiring_rows(BOX_WIRING_JSON)
    if ws_rows is None and box_rows is None:
        die(f"no fetch wiring staged (neither {DEV_GITEA_JSON} nor "
            f"{BOX_WIRING_JSON} is readable); this surface is not wired for "
            f"rs-fetch — create the box with the rs-fetch option (box "
            f"window), or recreate the project with rs-fetch enabled")
    for rows in ((ws_rows or []), (box_rows or [])):
        for row in rows:
            if isinstance(row, dict) and row.get("repo") == repo and row.get("user"):
                return row["user"]
    staged = sorted({r.get("repo") for rows in ((ws_rows or []), (box_rows or []))
                     for r in rows if isinstance(r, dict) and r.get("repo")})
    die(f"repo {repo!r} has no staged fork owner (staged repos: "
        f"{staged or 'none'}); add a dev project/box on it (or set the "
        f"active fork on the Development page), then restart this "
        f"box/project to refresh the wiring")


def valid_commit_sha(sha: str) -> bool:
    """Hex, 4-64 chars (the ADS validation)."""
    return 4 <= len(sha) <= 64 and all(c in HEX_DIGITS for c in sha)


def pick_mode(pr: int | None, branch: str, commit: str) -> str:
    """The selector grammar: at most one LOCATOR (--pr / --branch); --commit
    is a MODIFIER that requires one (gitea refuses bare-SHA fetches by
    default, so the locator's branch is what actually gets fetched and the
    sha is resolved locally). No locator at all -> 'list' (and a bare
    --commit is rejected with the remedy)."""
    if pr is not None and branch:
        raise ValueError("specify at most one of --pr <N> and --branch <name>")
    if pr is not None:
        return "pr"
    if branch:
        return "branch"
    if commit:
        raise ValueError(
            "--commit needs a locator: rs-fetch <repo> --pr <N> --commit <sha>"
            " or rs-fetch <repo> --branch <name> --commit <sha>")
    return "list"


def verdict_path(repo: str, pr: int) -> Path:
    return VERDICT_DIR / repo / f"{pr}.json"


def read_token() -> str:
    try:
        tok = TOKEN_PATH.read_text().strip()
    except OSError:
        tok = ""
    if not tok:
        die(f"operator token missing at {TOKEN_PATH}; it is staged only into "
            f"rs-fetch-enabled surfaces — create the box with the rs-fetch "
            f"option (box window) or recreate the project with rs-fetch "
            f"enabled; a stop/start of the project re-stages it")
    return tok


def api_get(path: str, token: str):
    """Bounded GET against the gitea API. Error messages carry only the path
    and status — never the token, never a response body."""
    req = Request(f"{GITEA_BASE}/api/v1{path}", headers={
        "Authorization": f"token {token}",
        "Accept": "application/json",
    })
    try:
        with urlopen(req, timeout=API_TIMEOUT_S) as resp:
            raw = resp.read()
    except HTTPError as e:
        die(f"gitea GET {path} -> HTTP {e.code}")
    except (URLError, OSError) as e:
        die(f"cannot reach gitea at {GITEA_BASE}: "
            f"{getattr(e, 'reason', e)} (is this surface wired for rs-fetch, "
            f"and is Gitea enabled on the Management page? restarting this "
            f"box/project refreshes the wiring)")
    try:
        return json.loads(raw) if raw else None
    except json.JSONDecodeError:
        return None


def git(repo_path: str, *args: str, with_auth: bool = False,
        check: bool = False) -> subprocess.CompletedProcess:
    """Run git in repo_path. with_auth injects a ONE-SHOT inline credential
    helper that echoes the operator identity and cats the token file at call
    time — the token never enters argv, and ~/.git-credentials is never
    touched (the clone may carry the human's own GitHub helper config)."""
    cmd = ["git", "-C", repo_path]
    if with_auth:
        helper = ("!f() { echo username=" + OPERATOR_USER + "; "
                  "echo \"password=$(cat "
                  + shlex.quote(str(TOKEN_PATH)) + ")\"; }; f")
        cmd += ["-c", "credential.helper=" + helper]
    cmd += list(args)
    return subprocess.run(cmd, capture_output=True, text=True, check=check)


def git_dir(repo_path: str) -> Path:
    """The clone's git dir via rev-parse — `.git` is a FILE, not a directory,
    in worktrees and submodules, so a hardcoded repo_path/.git would miss."""
    r = git(repo_path, "rev-parse", "--git-dir")
    p = Path(r.stdout.strip())
    return p if p.is_absolute() else Path(repo_path) / p


def squash_msg_path(repo_path: str) -> Path:
    """git's squash-message file: `git commit` prefills from it whenever it
    EXISTS (spike-verified — including after a plain `git apply` and after a
    resolved conflict), so writing the scrubbed message here reuses git's own
    prefill channel rather than inventing one."""
    return git_dir(repo_path) / "SQUASH_MSG"


# --- modes -------------------------------------------------------------------

def cmd_list(repo: str, token: str) -> None:
    """No selector: print the fork's open PRs so the human can pick one."""
    owner = fork_owner(repo)
    prs = api_get(f"/repos/{owner}/{repo}/pulls?state=open&limit={PR_LIST_LIMIT}",
                  token)
    if not isinstance(prs, list) or not prs:
        print(f"no open PRs on {owner}/{repo}")
        return
    print(f"open PRs on {owner}/{repo}:")
    for p in prs:
        if not isinstance(p, dict):
            continue
        head = (p.get("head") or {}).get("ref") or "?"
        print(f"  #{p.get('number'):<5} {p.get('title') or ''}  "
              f"[{head}]  updated {p.get('updated_at') or '?'}")
    if len(prs) >= PR_LIST_LIMIT:
        print(f"  (list truncated at {PR_LIST_LIMIT} — more PRs exist)")
    print(f"\nstage one:  rs-fetch {repo} --pr <N>   "
          f"(add --commit <sha> to walk it commit-by-commit)")


def resolve_pr(repo: str, pr: int, token: str) -> str:
    """PR number → head branch name (and print context)."""
    owner = fork_owner(repo)
    data = api_get(f"/repos/{owner}/{repo}/pulls/{pr}", token)
    if not isinstance(data, dict):
        die(f"PR #{pr} not found on {owner}/{repo}")
    head = (data.get("head") or {}).get("ref")
    base = (data.get("base") or {}).get("ref")
    if not head or not base:
        die(f"PR #{pr} response missing head/base refs")
    status = "merged" if data.get("merged") else data.get("state", "?")
    print(f"  PR #{pr} [{status}]: {head} -> {base}")
    if data.get("title"):
        print(f"  Title: {data['title']}")
    return head


# --- effective commits + message ----------------------------------------------

def effective_commits(repo_path: str, target: str) -> list[str]:
    """The commits `target` would ADD to HEAD, oldest-first, by PATCH-ID
    equivalence (`git cherry HEAD <target> <base>`), NOT sha ancestry: a
    walked commit lands locally under a NEW sha (new author, new timestamp),
    so ancestry would re-count — and re-message — every prior step forever;
    cherry recognizes the copied diff and skips it. The explicit <base> limit
    bounds the walk to base..target; ordering comes from rev-list --reverse
    (cherry's own output order is not documented)."""
    r = git(repo_path, "merge-base", "HEAD", target)
    base = r.stdout.strip()
    if r.returncode != 0 or not base:
        die("no common history between HEAD and the fetched work "
            "(is this clone of the same repo?)")
    r = git(repo_path, "cherry", "HEAD", target, base)
    plus = {ln[2:].strip() for ln in r.stdout.splitlines()
            if ln.startswith("+ ")}
    order = git(repo_path, "rev-list", "--reverse",
                f"{base}..{target}").stdout.split()
    return [sha for sha in order if sha in plus]


def build_message(repo_path: str, shas: list[str]) -> str:
    """Concatenate the messages of `shas` oldest-first, then scrub ONCE. One
    sha — the walk — is just that commit's message; N shas — the skip — hands
    the human every message to edit down. KNOWN LIMIT (stated, not hidden):
    patch-id equivalence is exact-diff, so after a SKIP landing (one squashed
    local commit for N agent commits) or a human touch-up, a later walk step's
    message may re-include already-landed commits — the CONTENT stays correct
    (the 3-way merge stages only the real delta; an all-landed re-fetch hits
    the nothing-new guard), only the prefill over-quotes."""
    parts = []
    for sha in shas:
        body = git(repo_path, "log", "-1", "--format=%B", sha).stdout.strip()
        if body:
            parts.append(body)
    return scrub_message("\n\n".join(parts))


# --- safety checks (ported from ADS) ------------------------------------------

def run_safety_checks(repo_path: str, ref: str) -> None:
    """Symlinks in the fetched tree + auto-execute-file modifications in the
    HEAD...ref diff. Informational — the human is the gate."""
    print("\n-- Pre-merge safety checks --")
    r = git(repo_path, "ls-tree", "-r", "--full-tree", ref)
    symlinks = [ln for ln in r.stdout.splitlines() if ln.startswith("120000")]
    if symlinks:
        print("  !! SYMLINKS in the fetched tree:")
        for ln in symlinks:
            parts = ln.split(None, 3)
            if len(parts) == 4:
                target = git(repo_path, "cat-file", "-p", parts[2])
                print(f"    {parts[3]} -> {target.stdout.strip()}")
    else:
        print("  symlinks: none")
    r = git(repo_path, "diff", "--quiet", f"HEAD...{ref}", "--",
            *AUTO_EXEC_PATHS)
    if r.returncode != 0:
        print("  !! auto-execute files MODIFIED (build/hook/editor entry "
              "points changed — read that part of the diff first; hook "
              "managers run at the `git commit` this staging leads to)")
    else:
        print("  auto-execute files: unchanged")


# --- verdict seam --------------------------------------------------------------

def handle_verdict(repo: str, pr: int | None, commit: str,
                   repo_path: str) -> None:
    """Surface a staged review verdict when one exists; never block. A
    whole-PR fetch reads the per-PR ledger name; a --commit fetch reads ONLY
    the per-SHA name (a whole-PR verdict is never shown against a single
    commit of it — PI decision), which today always prints the absent note
    (the per-commit reviewer is future work)."""
    if commit:
        src = VERDICT_DIR / repo / f"{commit}.json"
        dst_name = f".rs-review-{commit[:12]}.json"
        label = f"commit {commit[:12]}"
    elif pr is not None:
        src = verdict_path(repo, pr)
        dst_name = f".rs-review-pr{pr}.json"
        label = f"PR #{pr}"
    else:
        print("  (no review verdict: reviews are per-PR)")
        return
    if not src.is_file():
        print(f"  (no review verdict staged for {label} (reviewer not run))")
        return
    dst = Path(repo_path) / dst_name
    try:
        shutil.copyfile(src, dst)
    except OSError as e:
        print(f"  (verdict at {src} could not be copied: {e})")
        return
    print(f"  review verdict copied to {dst}")
    try:
        data = json.loads(src.read_text())
        summary = data.get("summary") if isinstance(data, dict) else None
        if summary:
            print(f"  review summary: {summary}")
    except (OSError, json.JSONDecodeError):
        pass


# --- apply ---------------------------------------------------------------------

def _new_work_patch(repo_path: str, target: str, shas: list[str]) -> bytes:
    """The binary-safe patch carrying exactly the NEW commits' cumulative
    diff: ``git diff <parent-of-first-new> <target>``. The parent-of-first-`+`
    base is the whole walk fix — merge-base stays pinned at the fork base
    forever (walked commits land under NEW shas, so no shared history ever
    accrues), which made a base-rooted `merge --squash` conflict on any
    commit that retouched an earlier step's file (add/add: ours v1, theirs
    v2). diff from the first new commit's parent contains only the un-landed
    delta, and the 3-way application absorbs identical already-landed content
    as a no-op. BYTES end-to-end: --binary patches are not text."""
    r = git(repo_path, "rev-parse", "--verify", "--quiet", shas[0] + "^")
    prev = r.stdout.strip()
    if not prev:
        # Root commit (no parent): diff against git's empty tree.
        prev = git(repo_path, "hash-object", "-t", "tree",
                   os.devnull).stdout.strip()
    r = subprocess.run(["git", "-C", repo_path, "diff", "--full-index",
                        "--binary", prev, target], capture_output=True)
    if r.returncode != 0:
        # A failed diff yields empty stdout, which would flow into the
        # empty-patch guard and read as a reassuring "nothing new" — die loud
        # instead (plain `git diff` exits non-zero only on real errors).
        die("could not build the patch: "
            + (r.stderr or b"").decode("utf-8", "replace").strip())
    return r.stdout


def apply_staged(repo_path: str, target: str, shas: list[str]) -> None:
    """Stage `target`'s new work (``git apply --3way`` of the new-commits
    patch — spike-verified: stages on success, leaves conflict markers +
    unmerged index entries on divergence, carries ZERO commit metadata) with
    the scrubbed message prefilled. NEVER commits — `git commit` is always
    the human's own act. Order is load-bearing: dirty-index refusal, then the
    nothing-new guards, then the apply, whose CONFLICT arm returns before the
    post-apply staged check (a conflicted index is not 'nothing staged')."""
    smsg = squash_msg_path(repo_path)
    # A dirty index would silently merge two fetches' content and the second
    # prefill overwrite would destroy the first — refuse up front.
    if git(repo_path, "diff", "--cached", "--quiet").returncode != 0:
        die("the index already holds staged changes; commit or unstage them "
            "first (a second fetch would merge into them and overwrite the "
            "prefilled message)")
    if not shas:
        # Every commit's diff is already in HEAD by patch-id. CHOSEN semantic:
        # this also covers landed-then-deliberately-REVERTED work — a re-fetch
        # does not fight a revert (re-applying is a manual git act if truly
        # wanted). Drop any stale prefill so it can't ride the next unrelated
        # commit.
        smsg.unlink(missing_ok=True)
        print("\nnothing new to fetch — every commit here is already in HEAD "
              "(by patch-id).")
        return
    message = build_message(repo_path, shas)
    r = git(repo_path, "rev-parse", "--abbrev-ref", "HEAD")
    local_branch = r.stdout.strip() if r.returncode == 0 else "unknown"
    patch = _new_work_patch(repo_path, target, shas)
    if not patch.strip():
        # New commits whose diffs cancel out — `git apply` hard-errors on an
        # empty patch, and there is genuinely nothing to stage.
        smsg.unlink(missing_ok=True)
        print("\nnothing new to fetch — the changes cancel out to an empty "
              "diff.")
        return
    r = subprocess.run(["git", "-C", repo_path, "apply", "--3way"],
                       input=patch, capture_output=True)
    if r.returncode != 0:
        detail = (r.stderr or b"").decode("utf-8", "replace").strip()
        # Only a REAL conflict (unmerged index entries) earns the prefilled
        # message — the human resolves and commits, and that commit reads
        # SQUASH_MSG (spike-verified, resolved-conflict case included). A
        # HARD abort (e.g. `git apply` "lacks the necessary blob" in a
        # shallow/partial clone) changed NOTHING, so a prefill would ride the
        # next unrelated commit — the no-stale-prefill guarantee applies.
        if git(repo_path, "ls-files", "-u").stdout.strip():
            try:
                smsg.write_text(message)
            except OSError as e:
                print(f"  (could not write the prefilled message: {e})")
            print(f"\nmerge failed:\n{detail}")
            print("resolve conflicts (`git status` shows the unmerged files), "
                  "`git add` them, then commit — the scrubbed message is "
                  "prefilled. Or `git reset --hard` and retry.")
        else:
            smsg.unlink(missing_ok=True)
            print(f"\napply failed (nothing was changed):\n{detail}")
            print("(a shallow clone lacks the blobs a 3-way apply needs — "
                  "`git fetch --unshallow` and retry)")
        return
    if git(repo_path, "diff", "--cached", "--quiet").returncode == 0:
        # The apply ran but staged nothing (an already-applied shape that
        # slipped past the cherry guard). Same no-stale-prefill guarantee.
        smsg.unlink(missing_ok=True)
        print("\nnothing new to fetch — the tree already matches.")
        return
    try:
        smsg.write_text(message)
    except OSError as e:
        print(f"  (could not write the prefilled message: {e})")
    n = len(shas)
    print(f"\n-- Staged on {local_branch} "
          f"({n} commit{'s' if n != 1 else ''} squashed) --")
    print(git(repo_path, "diff", "--staged", "--stat").stdout.rstrip())
    print("\ndone — changes are STAGED with the agent's (scrubbed) message "
          "prefilled.\nNOTE: staged work does not show in bare `git diff` — "
          "review with `git diff --staged`,\nthen `git commit` (the message "
          "opens prefilled; edit it) and push yourself.")


# --- main ----------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="rs-fetch",
        description="stage agent work from the shared rs-gitea into a local "
                    "clone, commit message prefilled (no selector: list the "
                    "fork's open PRs; rs-fetch never commits)")
    p.add_argument("repo", help="dev repo NAME (fetches its ACTIVE consumer fork)")
    p.add_argument("repo_path", nargs="?", default=None,
                   help="local git clone to apply into (default: cwd)")
    p.add_argument("--pr", type=int, default=None,
                   help="stage PR #N (whole PR, or the locator for --commit)")
    p.add_argument("--branch", default="",
                   help="stage a branch (whole, or the locator for --commit)")
    p.add_argument("--commit", default="",
                   help="stage ONE commit of the --pr/--branch work; walking "
                        "them oldest-first lands one commit's delta + message "
                        "per step")
    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    try:
        mode = pick_mode(args.pr, args.branch, args.commit)
    except ValueError as e:
        die(str(e))
    if args.commit and not valid_commit_sha(args.commit):
        die(f"--commit expects a hex SHA (4-64 chars), got {args.commit!r}")
    repo = args.repo
    token = read_token()

    if mode == "list":
        cmd_list(repo, token)
        return

    repo_path = os.path.abspath(args.repo_path) if args.repo_path else os.getcwd()
    r = subprocess.run(["git", "-C", repo_path, "rev-parse",
                        "--is-inside-work-tree"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        die(f"not a git repository: {repo_path}")
    # Anchor at the clone ROOT: a subdir cwd passes the work-tree check, but
    # `git apply` is cwd-sensitive in ways `merge` never was, and the verdict
    # copy + safety checks should land at the root regardless of where the
    # human pasted the command.
    r = subprocess.run(["git", "-C", repo_path, "rev-parse", "--show-toplevel"],
                       capture_output=True, text=True)
    if r.returncode == 0 and r.stdout.strip():
        repo_path = r.stdout.strip()

    fork_url = f"{GITEA_BASE}/{fork_owner(repo)}/{repo}.git"
    branch = args.branch
    if mode == "pr":
        branch = resolve_pr(repo, args.pr, token)

    ref = f"refs/rs-fetch/{branch}"
    print(f"\nfetching '{branch}' from {fork_url}...")
    r = git(repo_path, "fetch", fork_url, f"{branch}:{ref}", with_auth=True)
    if r.returncode != 0:
        print(f"error: branch '{branch}' not found at {fork_url}",
              file=sys.stderr)
        r = git(repo_path, "ls-remote", "--heads", fork_url, with_auth=True)
        if r.returncode == 0 and r.stdout.strip():
            print("available branches:")
            for ln in r.stdout.splitlines():
                parts = ln.split("\t")
                if len(parts) == 2 and parts[1].startswith("refs/heads/"):
                    print(f"  {parts[1][len('refs/heads/'):]}")
        sys.exit(1)

    target = ref
    commit_full = ""
    if args.commit:
        # Resolve the (possibly abbreviated) sha against the just-fetched
        # objects — the FULL sha keys the per-commit verdict seam — then
        # require it ON the fetched branch: --commit is a position on the
        # locator's history, never a free-floating object.
        r = git(repo_path, "rev-parse", "--verify", "--quiet",
                args.commit + "^{commit}")
        commit_full = r.stdout.strip()
        on_branch = bool(commit_full) and git(
            repo_path, "merge-base", "--is-ancestor",
            commit_full, ref).returncode == 0
        if not on_branch:
            git(repo_path, "update-ref", "-d", ref)
            die(f"commit {args.commit} not found on '{branch}' "
                f"(is it on a different PR/branch?)")
        target = commit_full

    try:
        shas = effective_commits(repo_path, target)
        run_safety_checks(repo_path, target)
        print("\n-- Review verdict --")
        handle_verdict(repo,
                       args.pr if (mode == "pr" and not args.commit) else None,
                       commit_full, repo_path)
        apply_staged(repo_path, target, shas)
    finally:
        git(repo_path, "update-ref", "-d", ref)


if __name__ == "__main__":
    main()
