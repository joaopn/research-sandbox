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

import hashlib
import json
import os
import subprocess
import sys
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
ADMIN_TOKEN_PATH = DEV_DIR / "admin.token"
OPERATOR_TOKEN_PATH = DEV_DIR / "operator.token"
ATTACHMENTS_PATH = DEV_DIR / "attachments.json"
TOKENS_DIR = DEV_DIR / "tokens"                 # per-CONSUMER agent tokens, 0700 dir
# Mirror stamps: one empty file per added mirror — the host-side "repo is
# added" floor (from_kwargs / attach / the box dev gate). Replaces the old
# token-file floor: `dev repo add` is mirror-only now and mints no tokens.
MIRRORS_DIR = DEV_DIR / "mirrors"
# The global per-repo ACTIVE fork map {repo: agent username}. Steers the
# Development-page reads, the reviewer's fork resolution, and the staged
# dev-gitea.json `user` rows. Absent entry = derived (single live fork, else
# the most recently created live fork).
ACTIVE_FORKS_PATH = DEV_DIR / "active-forks.json"

# Reviewer surface (STAGE_DEV_GITEA S4). The verdict ledger is HOST-ONLY state:
# never bind-mounted, never written into gitea or any container (invariant 4 —
# verdicts are not agent-visible; the Development page, via the authenticated
# broker relay, is the only surface). Sibling of dev/, not inside it, so the
# webui-adjacent dev/ tree and the ledger stay separate concerns.
REVIEWS_DIR = Path.home() / ".research-sandbox" / "reviews"
# The dedicated reviewer Claude account's OAuth creds (Q3): minted once by
# `research dev reviewer-login`, staged into each ephemeral reviewer container
# at spawn, updated by the post-review capture-back (token rotation).
REVIEWER_CRED_DIR = DEV_DIR / "reviewer"
REVIEWER_CRED_PATH = REVIEWER_CRED_DIR / ".credentials.json"

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
# the caller 20 min. This bound applies on the CLI path (research.py calls rscore
# directly) and inside the broker's DETACHED dev-box child — the only broker-side
# repo-add (there is no inline repo-add verb). A future standalone webui repo-add
# must also run detached, never inline (an inline relay would hold the serial
# daemon for the whole migrate).
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

# Repo FEATURES (gitea "units") on the two dev-lane repo kinds. Gitea's built-in
# DefaultForkRepoUnits is code+pulls ONLY, so a fresh fork ships with NO issue
# tracker — but the fork IS the human<->agent channel: repo-watch
# (container/dev/repo-watch.sh) polls /repos/<agent>/<repo>/issues on the fork,
# and gitea 404s that route while the unit is off. The MIRROR is a read-only
# upstream nobody watches, and gitea's DefaultMirrorRepoUnits DOES include
# issues — so issues are turned OFF there, leaving exactly ONE place to file.
# Unnamed units are PRESERVED by gitea's Edit (each is touched only when its
# option is non-nil), so a mirror keeps its code unit and its private flag.
FORK_FEATURES = {"has_issues": True, "has_pull_requests": True,
                 "has_wiki": True, "has_projects": True}
MIRROR_FEATURES = {"has_issues": False}


class GiteaError(Exception):
    """Any gitea-side failure. Message carries only method/path/status — never a
    request/response body, so a token in a migrate payload can never leak here."""


# --- paths / identity -------------------------------------------------------
#
# PER-CONSUMER identities: a consumer is a project's own dev agent
# ("<project>") or one dev box ("<project>.<box>"). Each consumer gets its OWN
# gitea user + fork + token — simultaneous consumers on one repo are legal
# (each works its own fork); retirement ARCHIVES the fork (history kept) and
# the user stays inert forever (deleting a gitea user PURGES its repos).

# Gitea's username length cap. The '.' consumer separator is legal in gitea's
# AlphaDashDot username charset but ILLEGAL in both the project- and box-name
# charsets — so '<project>.<box>' can never collide with a plain project's
# consumer string (project 'a_b' + box 'c' vs a project named 'a_b_c').
GITEA_USERNAME_MAX = 40


def consumer_for(project: str, box: str | None = None) -> str:
    """The consumer string. Inputs are already validated names (project regex /
    box regex choke points); the '.' join is the only added character."""
    return f"{project}.{box}" if box else project


def agent_username(consumer: str) -> str:
    """Gitea username for a consumer: 'agent-<consumer>', capped at gitea's
    limit. The cap applies to the FULL prefixed name; an over-long name keeps
    its head and appends '-XX' (2 hex chars of a stable hash of the full
    consumer string) so truncation-collided names stay distinct in practice."""
    name = f"{AGENT_USER_PREFIX}{consumer}"
    if len(name) <= GITEA_USERNAME_MAX:
        return name
    tail = hashlib.sha256(consumer.encode()).hexdigest()[:2]
    return f"{name[:GITEA_USERNAME_MAX - 3]}-{tail}"


def consumer_token_path(user: str) -> Path:
    """Host token file for a consumer's gitea user (the file name IS the
    username, already prefix+capped by agent_username)."""
    return TOKENS_DIR / f"{user}.token"


def mirror_stamp_path(repo: str) -> Path:
    return MIRRORS_DIR / repo


def mirror_present(repo: str) -> bool:
    """The 'repo is added' floor — a pure host-file check (no gitea call), so
    from_kwargs and the box gate can run it pre-side-effect."""
    return mirror_stamp_path(repo).is_file()


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

    def migrate_mirror(self, url: str, repo: str, pat: str | None) -> None:
        """Create the read-only mirror admin/<repo> from GitHub. Resumable: a
        COMPLETE existing mirror → trigger a sync; an INCOMPLETE stub (an `empty`
        repo left by an interrupted migrate — the D2 tail) → delete + re-migrate,
        since gitea 400s "not a mirror" on syncing a half-migrated repo. `auth_token`
        (the caller-supplied per-repo PAT; private source = bool(pat)) is sent only
        when present; gitea persists it in its own per-repo mirror config, so a
        private mirror keeps auto-syncing with no host-side copy — and a NEW PAT
        for an already-mirrored repo is NOT re-delivered (the healthy-mirror resume
        path above returns before the migrate): refresh = repo remove + re-add.
        It never appears in a GiteaError."""
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
        if pat:
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

    def set_repo_features(self, owner: str, repo: str, features: dict) -> None:
        """PATCH a repo's unit flags. Idempotent (re-runnable on resume); every
        EditRepoOption field is optional, so unnamed fields (private, default
        branch, mirror interval) are untouched. Legal on a mirror and on an
        archived fork — gitea's Edit special-cases neither."""
        self._api("PATCH", f"/repos/{owner}/{repo}", dict(features))

    def grant_collaborator(self, owner: str, repo: str, collaborator: str,
                           permission: str) -> None:
        """PUT is idempotent — safe to re-run on resume."""
        self._api("PUT", f"/repos/{owner}/{repo}/collaborators/{collaborator}",
                  {"permission": permission})

    def grant_read(self, owner: str, repo: str, collaborator: str) -> None:
        self.grant_collaborator(owner, repo, collaborator, "read")

    def subscribe(self, owner: str, repo: str) -> None:
        """The ACTING identity watches the repo. We call with the admin token,
        so the watcher is sandbox-admin — the human's gitea identity — and agent
        activity on the fork raises notifications for them."""
        self._api("PUT", f"/repos/{owner}/{repo}/subscription")

    def pull_info(self, owner: str, repo: str, index: int) -> dict:
        """One PR's metadata (title, state, head sha) for the review header +
        the verdict's staleness stamp. Fields picked by name."""
        data = self._api("GET", f"/repos/{owner}/{repo}/pulls/{index}")
        if not isinstance(data, dict):
            raise GiteaError(f"gitea GET pull {owner}/{repo}#{index} -> no data")
        return {"title": data.get("title") or "",
                "body": data.get("body") or "",
                "state": data.get("state") or "",
                "merged": bool(data.get("merged")),
                "head_sha": (data.get("head") or {}).get("sha") or ""}

    def pull_diff(self, owner: str, repo: str, index: int,
                  max_bytes: int) -> str:
        """The PR's raw unified diff (gitea's `.diff` endpoint) — a non-JSON
        sibling of _api with the same headers/timeout/no-bodies discipline.
        Reads max_bytes + 1 so overflow is detected exactly; the overflow error
        names sizes only, never content (the diff is adversarial input and the
        message may reach a client envelope)."""
        path = f"/repos/{owner}/{repo}/pulls/{index}.diff"
        req = urllib.request.Request(self._base + path, headers={
            "Authorization": f"token {self._token}",
        })
        try:
            with urllib.request.urlopen(req, timeout=API_TIMEOUT_S) as resp:
                raw = resp.read(max_bytes + 1)
        except urllib.error.HTTPError as e:
            raise GiteaError(f"gitea GET {path} -> HTTP {e.code}") from None
        except urllib.error.URLError as e:
            raise GiteaError(f"gitea GET {path} unreachable: {e.reason}") from None
        except OSError as e:
            raise GiteaError(f"gitea GET {path} failed: {e}") from None
        if len(raw) > max_bytes:
            raise GiteaError(
                f"gitea GET {path} -> diff exceeds the review cap "
                f"({max_bytes} bytes)")
        return raw.decode("utf-8", "replace")

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
    rscore._provision_gitea catches a token-present-but-gitea-gone mismatch
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


def mint_or_rotate_token(user: str) -> str:
    """Return a CONSUMER's gitea token, minting it (0600) if absent. The token
    is USER-scoped: a project consumer reuses one token across all its attached
    repos. Mint-if-absent (never rotate at wire/attach); the secret is written
    to a file, NOT returned into any result/report."""
    path = consumer_token_path(user)
    if path.is_file():
        val = path.read_text().strip()
        if val:
            return val
    tok = _mint_token(user, "write:repository")
    _write_secret(path, tok)
    return tok


def read_admin_token() -> str:
    if not ADMIN_TOKEN_PATH.is_file():
        raise GiteaError("gitea admin token missing (rs-gitea not bootstrapped)")
    return ADMIN_TOKEN_PATH.read_text().strip()


# --- repo lifecycle (composed sequences) ------------------------------------

def add_repo(host_port: str, url: str, repo: str, pat: str | None = None) -> None:
    """MIRROR-ONLY now: migrate + the host mirror-stamp (the 'added' floor).
    Consumer identities (user/fork/token) are minted per consumer at the point
    that needs an agent — provision_consumer, called by the dev-workflow create,
    the box dev path, and an explicit dev attach. Resumable: migrate is
    exists-checked upstream; re-stamping is a no-op. ``pat`` is the per-repo
    GitHub token (private source = bool(pat)); in memory for this call only."""
    client = GiteaClient(api_base(host_port), read_admin_token())
    client.migrate_mirror(url, repo, pat)
    # The mirror's OWN units, set where the mirror is minted. migrate_mirror
    # early-returns on a healthy existing mirror, so THIS call — not the migrate
    # — is what heals an already-added mirror's units on a re-add.
    client.set_repo_features(ADMIN_USER, repo, MIRROR_FEATURES)
    MIRRORS_DIR.mkdir(parents=True, exist_ok=True)
    mirror_stamp_path(repo).write_text("")


def provision_consumer(host_port: str, repo: str, user: str) -> None:
    """Create-or-reuse one consumer's identity for one repo: gitea user +
    mirror read-grant + fork into the consumer namespace + operator read-grant
    on the fork (the universal-fetch valve) + the fork's issue channel + a
    user-scoped token file. Every stage is exists-checked/idempotent (the
    add-repo resume discipline), so a re-run heals a fork that predates the
    feature block."""
    import secrets as _secrets
    client = GiteaClient(api_base(host_port), read_admin_token())
    client.create_user(user, _secrets.token_urlsafe(24))
    client.grant_read(ADMIN_USER, repo, user)     # consumer reads the private mirror
    client.fork_repo(ADMIN_USER, repo, user)      # fork into the consumer namespace
    client.grant_read(user, repo, OPERATOR_USER)  # operator reads the fork (fetch)
    # The fork's units MUST outlive a failure here: without the issue unit, gitea
    # 404s repo-watch's poll and the human->agent channel is dead. So features +
    # the human's grant are STRICT; the watch is a notification nicety and warns.
    client.set_repo_features(user, repo, FORK_FEATURES)
    client.grant_collaborator(user, repo, ADMIN_USER, "admin")
    try:
        client.subscribe(user, repo)
    except GiteaError as e:
        print(f"warning: could not watch {user}/{repo} ({e})", file=sys.stderr)
    mint_or_rotate_token(user)                    # writes tokens/<user>.token


def archive_fork(host_port: str, user: str, repo: str) -> None:
    """Read-only-freeze a retired consumer's fork — history kept, never
    deleted. The gitea USER is deliberately kept inert (deleting a user PURGES
    its repos, including any other fork it holds). FULLY best-effort — an
    unbootstrapped/sick gitea, an already-archived or an absent fork are all
    no-ops (retirement paths must never die on this). Token cleanup is the
    CALLER's decision (a project consumer's token is shared across repos)."""
    try:
        client = GiteaClient(api_base(host_port), read_admin_token())
        client._api("PATCH", f"/repos/{user}/{repo}", {"archived": True})
    except GiteaError:
        pass


def delete_consumer_token(user: str) -> None:
    try:
        consumer_token_path(user).unlink()
    except FileNotFoundError:
        pass


def remove_repo(host_port: str, repo: str) -> None:
    """Delete the mirror + best-effort every consumer FORK (repos only — NEVER
    delete_user; archived-fork users stay inert). WARN-and-continue on every
    cascade arm: a fork that survives (or an enumeration failure) prints the
    manual-cleanup remedy instead of failing silently or aborting the mirror
    delete. Cleans the mirror stamp + the active-fork entry + the forks'
    token files."""
    client = GiteaClient(api_base(host_port), read_admin_token())
    # The STRICT enumeration under this lane's own posture: warn-and-continue
    # (NOT list_forks, whose []-on-error would silently skip the cascade).
    try:
        forks = consumer_forks(host_port, repo)
    except GiteaError as e:
        forks = []
        print(f"warning: could not enumerate {repo!r}'s consumer forks ({e}); "
              f"delete any leftover agent-* forks in the gitea UI",
              file=sys.stderr)
    for f in forks:
        owner = f["user"]
        try:
            client._api("DELETE", f"/repos/{owner}/{repo}")
        except GiteaError as e:
            print(f"warning: could not delete fork {owner}/{repo} ({e}); "
                  f"delete it manually in the gitea UI", file=sys.stderr)
        delete_consumer_token(owner)
    client.delete_repo(ADMIN_USER, repo)
    try:
        mirror_stamp_path(repo).unlink()
    except FileNotFoundError:
        pass
    active = load_active_forks()
    if repo in active:
        del active[repo]
        save_active_forks(active)


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


# --- consumer forks + the active-fork map ------------------------------------

def consumer_forks(host_port: str, repo: str) -> list[dict]:
    """The mirror's consumer forks: [{user, archived, created_at}] — the STRICT
    enumeration: a GiteaError PROPAGATES. Only agent-prefixed owners count (a
    manual human fork is not a consumer).

    This is the single enumeration body; its callers pick the failure posture,
    and the posture is the whole point:
      * A destructive GATE (rscore.dev_repo_remove) must FAIL CLOSED — it calls
        this and refuses when it raises. Reading a sick gitea as "no forks" would
        mean "safe to delete the mirror and every fork on it".
      * A STATUS read degrades to empty (list_forks).
      * The delete CASCADE warns and continues (remove_repo).
    """
    client = GiteaClient(api_base(host_port), read_admin_token())
    raw = client._api("GET", f"/repos/{ADMIN_USER}/{repo}/forks",
                      timeout=STATUS_TIMEOUT_S) or []
    out = []
    for f in raw if isinstance(raw, list) else []:
        if not isinstance(f, dict):
            continue
        owner = (f.get("owner") or {}).get("login") or ""
        if owner.startswith(AGENT_USER_PREFIX):
            out.append({"user": owner,
                        "archived": bool(f.get("archived")),
                        "created_at": f.get("created_at") or ""})
    return out


def list_forks(host_port: str, repo: str) -> list[dict]:
    """The best-effort posture over consumer_forks: a sick gitea → empty list,
    so a STATUS read degrades to a no-fork view instead of failing the page.
    NEVER use this to gate a destructive action — it fails OPEN by design."""
    try:
        return consumer_forks(host_port, repo)
    except GiteaError:
        return []


def load_active_forks() -> dict:
    if not ACTIVE_FORKS_PATH.is_file():
        return {}
    try:
        data = json.loads(ACTIVE_FORKS_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def save_active_forks(entries: dict) -> None:
    DEV_DIR.mkdir(parents=True, exist_ok=True)
    tmp = ACTIVE_FORKS_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(entries, indent=2, sort_keys=True))
    tmp.replace(ACTIVE_FORKS_PATH)


def resolve_active_fork(repo: str, forks: list[dict]) -> str:
    """PURE resolution over an already-fetched forks list (unit-testable):
    the explicit map entry if it names a LIVE fork, else the single live fork,
    else the most recently created live fork (gitea `created_at` — the pinned
    recency source), else ''. Archived forks are never active."""
    live = [f for f in forks if not f.get("archived")]
    explicit = load_active_forks().get(repo)
    if explicit and any(f.get("user") == explicit for f in live):
        return explicit
    if len(live) == 1:
        return live[0].get("user") or ""
    if live:
        return max(live, key=lambda f: f.get("created_at") or "").get("user") or ""
    return ""


def active_fork_for(host_port: str, repo: str) -> str:
    """Fetch + resolve. '' when the repo has no live consumer fork."""
    return resolve_active_fork(repo, list_forks(host_port, repo))


def repo_status(host_port: str, repo: str) -> dict:
    """Per-repo Development-page status: mirror sync time + the ACTIVE fork's
    open PRs + branches (multi-fork reads are steered by the active-fork map —
    the deliberate simplification; no cross-fork aggregation), plus the forks
    list + active marker for the Management dropdown. STATUS_TIMEOUT_S-bounded
    GETs; fields are picked by NAME, so no secret can enter the result."""
    client = GiteaClient(api_base(host_port), read_admin_token())
    forks = list_forks(host_port, repo)
    active = resolve_active_fork(repo, forks)
    info = client._api("GET", f"/repos/{ADMIN_USER}/{repo}",
                       timeout=STATUS_TIMEOUT_S) or {}
    prs_raw, branches_raw = [], []
    if active:
        prs_raw = client._api(
            "GET", f"/repos/{active}/{repo}/pulls?state=open&limit={LIST_LIMIT}",
            timeout=STATUS_TIMEOUT_S) or []
        branches_raw = client._api("GET", f"/repos/{active}/{repo}/branches",
                                   timeout=STATUS_TIMEOUT_S) or []
    prs = []
    for p in prs_raw if isinstance(prs_raw, list) else []:
        if not isinstance(p, dict):
            continue
        prs.append({"number": p.get("number"),
                    "title": p.get("title") or "",
                    "head": (p.get("head") or {}).get("ref") or "",
                    "sha": (p.get("head") or {}).get("sha") or "",
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
            "forks": forks,
            "active": active,
            "prs": prs,
            "branches": branches}


# --- attachment record ------------------------------------------------------
#
# The CONSUMER ledger: which agent works which repo. Entries:
#   {"project": str, "class": "agent", "repo": str, "gitea_ip": str,
#    "box": str | None, "user": str}
# box=None → the project's own dev agent; box=<name> → that dev box. `user` is
# the consumer's gitea username (agent_username(consumer_for(...))). `class` is
# ALWAYS "agent" (the control class was retired); kept for record-shape
# stability. Dedupe key = (project, class, repo, box) — a box entry and the
# project's own entry for the SAME repo coexist. Host-only state, never
# bind-mounted, so a plain tmp+rename is correct (no inode pin).

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
                      gitea_ip: str, *, box: str | None = None,
                      user: str = "") -> None:
    """Add/refresh a consumer's entry. Keyed on (project, class, repo, box) so
    a re-wire refreshes gitea_ip in place rather than duplicating — and so a
    box entry never clobbers the project's own entry for the same repo."""
    entries = [e for e in load_attachments()
               if not (e.get("project") == project and e.get("class") == klass
                       and e.get("repo") == repo and e.get("box") == box)]
    entries.append({"project": project, "class": klass, "repo": repo,
                    "gitea_ip": gitea_ip, "box": box, "user": user})
    save_attachments_atomic(entries)


def prune_entry(project: str, repo: str | None,
                box: str | None = None) -> list[dict]:
    """Remove one consumer entry (project, repo, box). Returns the project's
    REMAINING entries so the caller decides network disconnect / token
    cleanup (the detach-to-zero rule)."""
    kept, remaining = [], []
    for e in load_attachments():
        if (e.get("project") == project and e.get("repo") == repo
                and e.get("box") == box):
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


# --- review verdict ledger (STAGE_DEV_GITEA S4) -------------------------------
#
# One JSON file per reviewed PR: REVIEWS_DIR/<repo>/<pr>.json, host-only (see
# the REVIEWS_DIR comment). Schema — fields picked by name, coarse failure
# reasons only (a review error can carry host paths; raw detail lives in the
# broker's host-only full log):
#   {repo, pr, head_sha, status: "ok"|"failed", reason?: <coarse token>,
#    risk?: "low"|"medium"|"high", summary?, findings?: [{file, note}],
#    reviewed_at}
# Failed entries are written too (from the diff-fetch step onward) so the
# Development page can show WHY nothing usable exists.

def verdict_ledger_path(repo: str, pr: int) -> Path:
    return REVIEWS_DIR / repo / f"{pr}.json"


def load_verdict(repo: str, pr: int) -> dict | None:
    """Tolerant read: absent/corrupt → None (a lost verdict re-reviews)."""
    try:
        data = json.loads(verdict_ledger_path(repo, pr).read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def save_verdict(repo: str, pr: int, payload: dict) -> Path:
    """Atomic per-PR write. The tmp name carries the writer's PID: reviews run
    in PARALLEL (S4/F5), and the shared `.with_suffix(".json.tmp")` idiom would
    let two same-PR writers interleave on one tmp file (A truncated by B, then
    A renames B's half-written bytes). Per-writer tmp + rename makes concurrent
    writes genuinely last-writer-wins."""
    path = verdict_ledger_path(repo, pr)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    tmp.replace(path)
    return path


def load_repo_verdicts(repo: str) -> dict[str, dict]:
    """All of a repo's ledger entries keyed by str(pr) — the dev_status merge.
    Pure host-file reads (the page read stays structurally no-start)."""
    d = REVIEWS_DIR / repo
    if not d.is_dir():
        return {}
    out: dict[str, dict] = {}
    for f in d.glob("*.json"):
        if not f.stem.isdigit():
            continue
        try:
            data = json.loads(f.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict):
            out[f.stem] = data
    return out
