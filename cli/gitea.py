"""Gitea client + dev-lane host state (STAGE_DEV_GITEA S1).

Stdlib-only satellite module (the ``cli/mcp_registry.py`` precedent): rscore
imports it, so it must NOT import rscore (that would be circular). It holds:

  - the bounded Gitea REST client (`GiteaClient`) + the `GiteaError` channel;
  - first-run account bootstrap (admin + operator) via `docker exec … gitea
    admin …` (the token is not re-readable after mint, so it's captured out);
  - the per-repo mirror→fork→token→grant sequence (idempotent/resumable — a
    bounded call can time out mid-migrate while gitea finishes, so a re-run must
    heal, ADS `sandboxcore.py:1225-1266`);
  - the host-side attachment record (`attachments.json`): which project works
    which repo (agent-class), driving the per-repo token staging and the
    dev-box repo association. (Wire-at-start is UNIVERSAL now — every project
    is connected once gitea exists, like the registry; the record is no longer
    what selects the wire set.)

Host state lives under ``~/.research-sandbox/dev/`` — OUTSIDE the webui-mounted
``run/`` subdir, never bind-mounted. Secrets (the GitHub PAT, the gitea agent
tokens) never enter a `GiteaError` message: errors carry only method/path/status,
never request or response bodies.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

# --- constants --------------------------------------------------------------

GITEA_CONTAINER = "rs-gitea"
GITEA_INNER_PORT = "3000"
GITEA_DATA_VOLUME = "rs-gitea-data"
DEFAULT_GITEA_VERSION = "1.25.1"
DEFAULT_GITEA_HOST_PORT = "3000"

DEV_DIR = Path.home() / ".research-sandbox" / "dev"
PAT_PATH = DEV_DIR / "github.pat"
ADMIN_TOKEN_PATH = DEV_DIR / "admin.token"
OPERATOR_TOKEN_PATH = DEV_DIR / "operator.token"
ATTACHMENTS_PATH = DEV_DIR / "attachments.json"
TOKENS_DIR = DEV_DIR / "tokens"                 # per-repo agent tokens, 0700 dir

# Bounded so an unresponsive gitea can't wedge the broker's serial accept thread:
# must be < the webui's 30s BROKER_CALL_TIMEOUT_S (rscore._UPSTREAM_RESOLVE_MAX_TIME_S
# is the sibling precedent). At half this, a slow-but-alive API call false-fails;
# at 10x, a dead gitea holds the daemon ~2.5 min. This bounds the QUICK calls
# (metadata GETs, user/fork/grant); the one inherently-long call (migrate) has its
# own bound below.
API_TIMEOUT_S = 15
# `POST /repos/migrate` BLOCKS until gitea finishes the initial git clone + the
# GitHub metadata fetch (api.github.com), so it needs a bound matched to a clone,
# not to a metadata GET — bounding it at API_TIMEOUT_S made even a tiny repo
# fail-then-resume on every add. 120s covers a normal code repo comfortably; a
# multi-GB repo still exceeds it and heals on re-run (the D2 resume posture). At
# 60s a medium repo on a slow link false-fails; at 1200s a wedged clone would hold
# the caller 20 min. This is the CLI-path value; repo add has NO webui route —
# a future webui repo-add must go through the detached build lane, never inline
# (an inline relay would hold the serial daemon for the whole migrate).
MIGRATE_TIMEOUT_S = 120

ADMIN_USER = "sandbox-admin"
OPERATOR_USER = "operator"
AGENT_USER_PREFIX = "agent-"
# Mirror cron cadence. Gitea's default `mirror.MIN_INTERVAL` is 10m and it 500s
# a migrate that sets a shorter one — so 10m is the floor without weakening that
# guard. It's only a BACKSTOP anyway: `dev sync <repo>` (and the resume path)
# trigger an immediate pull when the human wants GitHub changes reflected now.
MIRROR_INTERVAL = "10m"
# `repos/search` page cap for `dev repo list`. A single-operator dev lane holds
# far fewer mirrors than this; truncation (loud, above) means "add pagination".
LIST_LIMIT = 100
# The Development-page read bound (repo_status: three metadata GETs per repo on
# the broker's serial thread). A local-bridge gitea answers these in ms and a
# DOWN one refuses instantly — the bound only matters for a HALF-UP gitea,
# where the quick-call API_TIMEOUT_S (15s) would blow the webui's 30s relay
# window at a single repo (3 calls). At 1s a gitea mid-GC could false-fail a
# healthy read; at 15s one repo exhausts the relay window. A many-repo half-up
# worst can still exceed the window — accepted (the daemon keeps working; only
# that one relayed page read reports unreachable).
STATUS_TIMEOUT_S = 5
# Bounded wait for gitea's async fork (202) to materialize. A small-repo fork is
# near-instant; 30×1s covers a busy gitea without hanging. At 5s a loaded gitea
# false-fails; at 300s a wedged fork would hold the caller 5 min.
FORK_WAIT_TRIES = 30


class GiteaError(Exception):
    """Any gitea-side failure. Message carries only method/path/status — never a
    request/response body, so a token in a migrate payload can never leak here."""


# --- paths / identity -------------------------------------------------------

def agent_user_for(repo: str) -> str:
    """The per-repo agent gitea username. `repo` is already the validated,
    path-anchored segment (rscore.DevRepoAddRequest.from_kwargs) — one choke
    point, so no re-validation here."""
    return f"{AGENT_USER_PREFIX}{repo}"


def agent_token_path(repo: str) -> Path:
    return TOKENS_DIR / f"{AGENT_USER_PREFIX}{repo}.token"


def api_base(host_port: str) -> str:
    """The host-loopback API root. rscore publishes 127.0.0.1:<host_port>:3000."""
    return f"http://127.0.0.1:{host_port}/api/v1"


def _write_secret(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    # Create with 0600 from the start (never a wider window).
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(value)


# --- docker exec (bootstrap uses the gitea CLI, not the API) ----------------

def _docker_exec(argv: list[str]) -> subprocess.CompletedProcess:
    """`docker exec -u git rs-gitea <argv>`, capturing output. Bootstrap runs the
    gitea admin CLI (token mint is CLI-only — the API can't mint another user's
    token without that user's basic-auth)."""
    return subprocess.run(
        ["docker", "exec", "-u", "git", GITEA_CONTAINER, *argv],
        capture_output=True, text=True,
    )


def _gitea_admin(argv: list[str]) -> subprocess.CompletedProcess:
    return _docker_exec(["gitea", "admin", *argv])


# --- the REST client --------------------------------------------------------

class GiteaClient:
    """Bounded admin-token client. Every call is API_TIMEOUT_S-bounded; failures
    raise GiteaError with method/path/status only."""

    def __init__(self, base: str, token: str) -> None:
        self._base = base
        self._token = token

    def _api(self, method: str, path: str, body: Any = None,
             sudo: str | None = None, ok: tuple[int, ...] = (200, 201, 204),
             timeout: int | None = None) -> Any:
        url = self._base + path
        to = API_TIMEOUT_S if timeout is None else timeout
        data = None
        headers = {
            "Authorization": f"token {self._token}",
            "Accept": "application/json",
        }
        if sudo:
            headers["Sudo"] = sudo
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=to) as resp:
                status = resp.status
                raw = resp.read()
        except urllib.error.HTTPError as e:
            # e.code is the status; DO NOT include the response body (it can echo
            # the request, which for migrate carries auth_token).
            raise GiteaError(f"gitea {method} {path} -> HTTP {e.code}") from None
        except urllib.error.URLError as e:
            raise GiteaError(f"gitea {method} {path} unreachable: {e.reason}") from None
        except OSError as e:                    # timeout surfaces here
            raise GiteaError(f"gitea {method} {path} failed: {e}") from None
        if status not in ok:
            raise GiteaError(f"gitea {method} {path} -> HTTP {status}")
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None

    def repo_exists(self, owner: str, repo: str) -> bool:
        try:
            self._api("GET", f"/repos/{owner}/{repo}")
            return True
        except GiteaError:
            return False

    def user_exists(self, username: str) -> bool:
        try:
            self._api("GET", f"/users/{username}")
            return True
        except GiteaError:
            return False

    def migrate_mirror(self, url: str, repo: str, private: bool,
                       pat: str | None) -> None:
        """Create the read-only mirror admin/<repo> from GitHub. Resumable: a
        COMPLETE existing mirror → trigger a sync; an INCOMPLETE stub (an `empty`
        repo left by an interrupted migrate — the D2 tail) → delete + re-migrate,
        since gitea 400s "not a mirror" on syncing a half-migrated repo. `auth_token`
        (the PAT) is sent only for a private source; it never appears in a GiteaError."""
        if self.repo_exists(ADMIN_USER, repo):
            info = self._api("GET", f"/repos/{ADMIN_USER}/{repo}") or {}
            if info.get("mirror") and not info.get("empty"):
                self.trigger_sync(repo)         # healthy mirror → refresh + done
                return
            if not info.get("empty"):
                # A NON-empty, non-mirror repo under sandbox-admin is the human's
                # own repo (Q5: sandbox-admin is the operator's interactive gitea
                # identity) that collides on name — NEVER delete data.
                raise GiteaError(
                    f"{ADMIN_USER}/{repo} exists as a non-mirror repo (name "
                    f"collision); remove it manually or use a different repo name")
            # An EMPTY repo carries no data, so the stub-heal deletes + re-migrates.
            # Two in-model quirks (converge, harmless): a zero-commit GitHub source
            # stays empty after a SUCCESSFUL migrate, so each re-add re-migrates it;
            # and a re-run WHILE the first migrate is still cloning server-side sees
            # empty and restarts from zero — the D2 "re-run heals" posture assumes
            # the user waits for the prior add to return, not a tight retry loop.
            self.delete_repo(ADMIN_USER, repo)
        body = {
            "clone_addr": url,
            "repo_name": repo,
            "repo_owner": ADMIN_USER,
            "mirror": True,
            "mirror_interval": MIRROR_INTERVAL,
            "private": True,          # private always (D1: cross-tenant isolation)
            "service": "github",
            # Mirror the GIT TREE ONLY — not GitHub's issue tracker. The dev lane
            # works with code (the agent pushes to its fork; issues/PRs live on
            # GitHub). Importing issues/PRs/releases is both semantically wrong here
            # AND catastrophically slow for a high-traffic repo (octocat/Spoon-Knife
            # has thousands of demo PRs — its metadata import runs many minutes).
            "wiki": False,
            "issues": False,
            "pull_requests": False,
            "releases": False,
            "labels": False,
            "milestones": False,
        }
        if private and pat:
            body["auth_token"] = pat
        self._api("POST", "/repos/migrate", body, timeout=MIGRATE_TIMEOUT_S)

    def trigger_sync(self, repo: str) -> None:
        self._api("POST", f"/repos/{ADMIN_USER}/{repo}/mirror-sync")

    def create_user(self, username: str, password: str, admin: bool = False) -> None:
        """Idempotent create via the admin API."""
        if self.user_exists(username):
            return
        self._api("POST", "/admin/users", {
            "username": username,
            "password": password,
            "email": f"{username}@rs.invalid",
            "must_change_password": False,
        })

    def fork_repo(self, src_owner: str, repo: str, as_user: str) -> None:
        """Fork admin/<repo> into <as_user>/<repo> (Sudo = act as that user).
        Resumable: skip if the fork already exists. Gitea forks ASYNCHRONOUSLY
        (202 Accepted), so accept 202 and wait, bounded, for the fork to
        materialize — the operator-grant that follows operates on it."""
        if self.repo_exists(as_user, repo):
            return
        self._api("POST", f"/repos/{src_owner}/{repo}/forks", {}, sudo=as_user,
                  ok=(200, 201, 202))
        for _ in range(FORK_WAIT_TRIES):
            if self.repo_exists(as_user, repo):
                return
            time.sleep(1)
        raise GiteaError(f"fork {as_user}/{repo} did not appear in time")

    def grant_read(self, owner: str, repo: str, collaborator: str) -> None:
        """PUT is idempotent — safe to re-run on resume."""
        self._api("PUT", f"/repos/{owner}/{repo}/collaborators/{collaborator}",
                  {"permission": "read"})

    def delete_repo(self, owner: str, repo: str) -> None:
        try:
            self._api("DELETE", f"/repos/{owner}/{repo}")
        except GiteaError:
            pass                                 # already gone — best-effort teardown

    def delete_user(self, username: str) -> None:
        try:
            self._api("DELETE", f"/admin/users/{username}?purge=true")
        except GiteaError:
            pass


# --- bootstrap (first stand-up) ---------------------------------------------

def bootstrap_present() -> bool:
    return ADMIN_TOKEN_PATH.is_file()


def bootstrap_accounts(host_port: str, admin_password: str,
                       operator_password: str) -> None:
    """Create the admin + operator gitea accounts and capture their tokens.
    Idempotent: no-op once admin.token exists (the stale-token guard in
    rscore._ensure_gitea_running catches a token-present-but-gitea-gone mismatch
    BEFORE this runs). Tokens are minted via the gitea CLI (`docker exec`) — the
    only way to obtain another user's token without that user's basic-auth."""
    if bootstrap_present():
        return
    # Admin: create + mint an all-scope token.
    _gitea_admin(["user", "create", "--username", ADMIN_USER,
                  "--password", admin_password, "--email", f"{ADMIN_USER}@rs.invalid",
                  "--admin", "--must-change-password=false"])
    admin_tok = _mint_token(ADMIN_USER, "all")
    _write_secret(ADMIN_TOKEN_PATH, admin_tok)
    # Operator: create + mint a read-only token (fetch-side identity; per-fork
    # read grants are added at repo-add).
    _gitea_admin(["user", "create", "--username", OPERATOR_USER,
                  "--password", operator_password,
                  "--email", f"{OPERATOR_USER}@rs.invalid",
                  "--must-change-password=false"])
    op_tok = _mint_token(OPERATOR_USER, "read:repository")
    _write_secret(OPERATOR_TOKEN_PATH, op_tok)


def _mint_token(username: str, scopes: str) -> str:
    # Fixed token name: gitea rejects a duplicate name for one user, so a
    # manually-deleted token FILE whose gitea-side "rs-dev" token still lives
    # will fail re-mint until the repo is removed+re-added (which purges the
    # user). Acceptable — the file is the source of truth in the normal flow.
    r = _gitea_admin(["user", "generate-access-token", "--username", username,
                      "--scopes", scopes, "--raw", "--token-name", "rs-dev"])
    if r.returncode != 0:
        # Never echo stderr verbatim — a mint failure detail is coarse.
        raise GiteaError(f"could not mint a gitea token for {username!r}")
    tok = (r.stdout or "").strip().splitlines()[-1] if r.stdout.strip() else ""
    if not tok:
        raise GiteaError(f"empty gitea token for {username!r}")
    return tok


def mint_or_rotate_agent_token(repo: str) -> str:
    """Return the agent's gitea token for <repo>, minting it (0600) if absent.
    Mint-if-absent: on a re-add resume the file already exists, so the token is
    kept — a rotation happens only after delete_repo_set removed the file
    (decision 8: never rotate at wire/attach). The secret is written to a file,
    NOT returned into any result/report."""
    path = agent_token_path(repo)
    if path.is_file():
        val = path.read_text().strip()
        if val:
            return val
    tok = _mint_token(agent_user_for(repo), "write:repository")
    _write_secret(path, tok)
    return tok


def read_admin_token() -> str:
    if not ADMIN_TOKEN_PATH.is_file():
        raise GiteaError("gitea admin token missing (rs-gitea not bootstrapped)")
    return ADMIN_TOKEN_PATH.read_text().strip()


# --- repo lifecycle (composed sequences) ------------------------------------

def add_repo(host_port: str, url: str, repo: str, private: bool) -> None:
    """The resumable mirror→user→fork→token→grants sequence. Every stage is
    exists-checked, so a re-run after a mid-migrate timeout heals the state.
    Reads the PAT (0600) only when the source is private, in memory for the call
    only."""
    import secrets as _secrets
    client = GiteaClient(api_base(host_port), read_admin_token())
    pat = None
    if private and PAT_PATH.is_file():
        pat = PAT_PATH.read_text().strip() or None
    client.migrate_mirror(url, repo, private, pat)
    agent = agent_user_for(repo)
    client.create_user(agent, _secrets.token_urlsafe(24))
    client.grant_read(ADMIN_USER, repo, agent)          # agent reads the private mirror
    client.fork_repo(ADMIN_USER, repo, agent)           # fork into the agent namespace
    client.grant_read(agent, repo, OPERATOR_USER)        # operator reads the fork (fetch)
    mint_or_rotate_agent_token(repo)                     # writes tokens/agent-<repo>.token


def remove_repo(host_port: str, repo: str) -> None:
    """Delete the fork, agent user, and mirror, plus the token file."""
    client = GiteaClient(api_base(host_port), read_admin_token())
    agent = agent_user_for(repo)
    client.delete_repo(agent, repo)
    client.delete_user(agent)
    client.delete_repo(ADMIN_USER, repo)
    try:
        agent_token_path(repo).unlink()
    except FileNotFoundError:
        pass


def list_repos(host_port: str) -> list[dict]:
    """The mirrors under sandbox-admin — one bounded `repos/search` call, no
    per-repo fan-out (keeps the total inside the relay budget). LIST_LIMIT caps
    the page: a single-operator dev lane holds far fewer than this; if it ever
    truncates, the fix is pagination, not a bigger magic number."""
    client = GiteaClient(api_base(host_port), read_admin_token())
    data = client._api("GET", f"/repos/search?limit={LIST_LIMIT}&q=") or {}
    rows = data.get("data") or []
    if len(rows) >= LIST_LIMIT:
        # Loud, not silent (the no-silent-caps rule): surface truncation rather
        # than quietly return a short list.
        print(f"warning: dev repo list truncated at {LIST_LIMIT} results")
    out = []
    for r in rows:
        owner = (r.get("owner") or {}).get("login")
        if owner == ADMIN_USER:
            out.append({"repo": r.get("name"), "mirror": bool(r.get("mirror")),
                        "private": bool(r.get("private"))})
    return out


def sync_repo(host_port: str, repo: str) -> None:
    GiteaClient(api_base(host_port), read_admin_token()).trigger_sync(repo)


def repo_status(host_port: str, repo: str) -> dict:
    """Per-repo Development-page status: mirror sync time + the fork's open PRs
    + branches. Three STATUS_TIMEOUT_S-bounded GETs; fields are picked by NAME,
    so no secret can enter the result (and GiteaError never carries bodies)."""
    client = GiteaClient(api_base(host_port), read_admin_token())
    agent = agent_user_for(repo)
    info = client._api("GET", f"/repos/{ADMIN_USER}/{repo}",
                       timeout=STATUS_TIMEOUT_S) or {}
    prs_raw = client._api(
        "GET", f"/repos/{agent}/{repo}/pulls?state=open&limit={LIST_LIMIT}",
        timeout=STATUS_TIMEOUT_S) or []
    branches_raw = client._api("GET", f"/repos/{agent}/{repo}/branches",
                               timeout=STATUS_TIMEOUT_S) or []
    prs = []
    for p in prs_raw if isinstance(prs_raw, list) else []:
        if not isinstance(p, dict):
            continue
        prs.append({"number": p.get("number"),
                    "title": p.get("title") or "",
                    "head": (p.get("head") or {}).get("ref") or "",
                    "user": (p.get("user") or {}).get("login") or "",
                    "updated_at": p.get("updated_at") or ""})
    branches = []
    for b in branches_raw if isinstance(branches_raw, list) else []:
        if not isinstance(b, dict):
            continue
        branches.append({
            "name": b.get("name") or "",
            "committed_at": (b.get("commit") or {}).get("timestamp") or ""})
    return {"repo": repo,
            "private": bool(info.get("private")),
            "mirror_synced_at": (info.get("mirror_updated")
                                 or info.get("updated_at") or ""),
            "prs": prs,
            "branches": branches}


# --- attachment record ------------------------------------------------------
#
# Which project works which repo (agent tokens + dev-box association). Entries:
#   {"project": str, "class": "agent", "repo": str, "gitea_ip": str}
# `class` is ALWAYS "agent" now (the control class was retired when fetch went
# universal); the key is kept for record-shape stability — one writer. A project
# may hold multiple entries (one per repo). Host-only state, never bind-mounted,
# so a plain tmp+rename is correct (no inode pin).

def load_attachments() -> list[dict]:
    if not ATTACHMENTS_PATH.is_file():
        return []
    try:
        data = json.loads(ATTACHMENTS_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return []
    return data if isinstance(data, list) else []


def save_attachments_atomic(entries: list[dict]) -> None:
    DEV_DIR.mkdir(parents=True, exist_ok=True)
    tmp = ATTACHMENTS_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(entries, indent=2, sort_keys=True))
    tmp.replace(ATTACHMENTS_PATH)


def record_attachment(project: str, klass: str, repo: str | None,
                      gitea_ip: str) -> None:
    """Add/refresh a project's entry. Keyed on (project, class, repo) so a
    re-wire refreshes gitea_ip in place rather than duplicating."""
    entries = [e for e in load_attachments()
               if not (e.get("project") == project and e.get("class") == klass
                       and e.get("repo") == repo)]
    entries.append({"project": project, "class": klass, "repo": repo,
                    "gitea_ip": gitea_ip})
    save_attachments_atomic(entries)


def prune_entry(project: str, repo: str | None) -> list[dict]:
    """Remove one entry (project, repo). Returns the project's REMAINING entries
    so the caller decides whether to disconnect the network."""
    kept, remaining = [], []
    for e in load_attachments():
        if e.get("project") == project and e.get("repo") == repo:
            continue
        kept.append(e)
        if e.get("project") == project:
            remaining.append(e)
    save_attachments_atomic(kept)
    return remaining


def prune_project(project: str) -> None:
    """Drop ALL of a project's entries (destroy path)."""
    save_attachments_atomic(
        [e for e in load_attachments() if e.get("project") != project])


def project_entries(project: str) -> list[dict]:
    return [e for e in load_attachments() if e.get("project") == project]


def attached_projects(repo: str) -> list[str]:
    """Projects holding an entry that references `repo` (the refuse-while-attached
    guard for repo remove)."""
    return sorted({e["project"] for e in load_attachments()
                   if e.get("repo") == repo and e.get("project")})
