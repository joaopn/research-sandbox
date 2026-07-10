#!/usr/bin/env python3
"""rs-fetch — pull agent work from the shared rs-gitea into a local clone.

Staged by the host into every project container and box (never baked;
the fetch surface is a standing utility of the dev lane). Runs as the
container user against the per-repo agent fork (``agent-<repo>/<repo>``)
using the READ-ONLY operator token staged at ``~/.dev-tokens/operator.token``
— it can fetch agent work; nothing it holds can write to gitea (the one-way
valve). The human's push credential is theirs alone and never involved here.

Usage:
    rs-fetch <repo> [<repo_path>]                 # list the fork's open PRs
    rs-fetch <repo> [<repo_path>] --pr N          # fetch PR #N (head branch)
    rs-fetch <repo> [<repo_path>] --branch NAME   # fetch a branch
    rs-fetch <repo> [<repo_path>] --commit SHA    # fetch a commit

``repo_path`` defaults to the current directory and must be a git work tree
(the human's local clone of the real repo). The fetched work is applied as
UNSTAGED modifications (``git merge --squash`` + ``git reset``) so it is
reviewed in the editor / ``git diff`` before anything is committed or pushed.

Ported from agentic-dev-sandbox's fetch-sandbox.py. The in-tool LLM review is
deliberately ABSENT: reviews run host-side in an ephemeral sandboxed container
and land as verdict files (see VERDICT_DIR below) — rs-fetch only surfaces a
verdict when one was staged, and never blocks when none exists.
"""

from __future__ import annotations

import argparse
import json
import os
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

# Verdict seam (S4 owns the transport INTO this path; rs-fetch only reads it):
# a review verdict for <repo> PR <n> lives at VERDICT_DIR/<repo>/<n>.json.
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
AUTO_EXEC_PATHS = [
    ".envrc", ".vscode/", ".husky/", ".pre-commit-config.yaml", ".gitmodules",
    "package.json", "setup.py", "setup.cfg", "Makefile", "CMakeLists.txt",
    ".cargo/config.toml", ".eslintrc.js", ".prettierrc.js",
]

HEX_DIGITS = set("0123456789abcdefABCDEF")


def die(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(1)


def fork_owner(repo: str) -> str:
    return f"{AGENT_USER_PREFIX}{repo}"


def valid_commit_sha(sha: str) -> bool:
    """Hex, 4-64 chars (the ADS validation)."""
    return 4 <= len(sha) <= 64 and all(c in HEX_DIGITS for c in sha)


def pick_mode(pr: int | None, branch: str, commit: str) -> str:
    """Exactly one selector → its mode; none → 'list'; several → ValueError."""
    selected = [m for m, v in (("pr", pr is not None),
                               ("branch", bool(branch)),
                               ("commit", bool(commit))) if v]
    if not selected:
        return "list"
    if len(selected) > 1:
        raise ValueError(
            "specify at most one of --pr <N>, --branch <name>, --commit <sha>")
    return selected[0]


def verdict_path(repo: str, pr: int) -> Path:
    return VERDICT_DIR / repo / f"{pr}.json"


def read_token() -> str:
    try:
        tok = TOKEN_PATH.read_text().strip()
    except OSError:
        tok = ""
    if not tok:
        die(f"operator token missing at {TOKEN_PATH}; the host stages it once "
            f"the dev lane exists — re-run `research start` (or restart this "
            f"box) and retry")
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
            f"{getattr(e, 'reason', e)} (is this container wired to the dev "
            f"lane? re-run `research start` or restart this box)")
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
    print(f"\nfetch one:  rs-fetch {repo} --pr <N>")


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
              "points changed — read that part of the diff first)")
    else:
        print("  auto-execute files: unchanged")


# --- verdict seam --------------------------------------------------------------

def handle_verdict(repo: str, pr: int | None, repo_path: str) -> None:
    """Surface a staged review verdict when one exists; never block. Verdicts
    are per-PR; branch/commit fetches get the absent note."""
    if pr is None:
        print("  (no review verdict: reviews are per-PR)")
        return
    src = verdict_path(repo, pr)
    if not src.is_file():
        print("  (no review verdict staged (reviewer not run))")
        return
    dst = Path(repo_path) / f".rs-review-pr{pr}.json"
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

def apply_squash(repo_path: str, ref: str) -> None:
    r = git(repo_path, "rev-parse", "--abbrev-ref", "HEAD")
    local_branch = r.stdout.strip() if r.returncode == 0 else "unknown"
    r = git(repo_path, "merge", "--squash", ref)
    if r.returncode != 0:
        print(f"\nmerge failed:\n{r.stderr.strip()}")
        print("resolve conflicts or stash local changes and retry.")
        return
    git(repo_path, "reset", "HEAD")
    print(f"\ndone — changes applied as UNSTAGED modifications on "
          f"{local_branch}; review, then commit + push yourself.")


# --- main ----------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="rs-fetch",
        description="fetch agent work from the shared rs-gitea into a local "
                    "clone (no selector: list the fork's open PRs)")
    p.add_argument("repo", help="dev repo NAME (the fork agent-<repo>/<repo>)")
    p.add_argument("repo_path", nargs="?", default=None,
                   help="local git clone to apply into (default: cwd)")
    p.add_argument("--pr", type=int, default=None, help="fetch PR #N")
    p.add_argument("--branch", default="", help="fetch a branch by name")
    p.add_argument("--commit", default="", help="fetch a commit SHA")
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

    fork_url = f"{GITEA_BASE}/{fork_owner(repo)}/{repo}.git"
    branch = args.branch
    if mode == "pr":
        branch = resolve_pr(repo, args.pr, token)

    if mode == "commit":
        ref = f"refs/rs-fetch/commit-{args.commit[:12]}"
        print(f"\nfetching commit {args.commit} from {fork_url}...")
        r = git(repo_path, "fetch", fork_url, f"+{args.commit}:{ref}",
                with_auth=True)
        if r.returncode != 0:
            print(f"error: cannot fetch commit {args.commit} from {fork_url}",
                  file=sys.stderr)
            print("(gitea must allow arbitrary-SHA fetch; prefer --pr/--branch)")
            print(f"git stderr:\n{r.stderr}")
            sys.exit(1)
    else:
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

    try:
        run_safety_checks(repo_path, ref)
        print("\n-- Review verdict --")
        handle_verdict(repo, args.pr, repo_path)
        apply_squash(repo_path, ref)
    finally:
        git(repo_path, "update-ref", "-d", ref)


if __name__ == "__main__":
    main()
