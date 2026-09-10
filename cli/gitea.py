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

import datetime
import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
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
# Per-repo rs-fetch VISIBILITY, the sibling of the active-fork map above: a
# {repo: bool} preference read with a default of True, so a repo that has never
# been touched is visible and the file only ever records deliberate choices.
# Display state ONLY — it steers which repos the webui's rs-fetch LISTS render
# and nothing else. Deliberately NOT gitea's own `archived` flag: archiving a
# repo makes it read-only (a mirror would stop syncing), and `archived` already
# means "retired consumer identity" to the Remove gate, the purge gate and the
# active-fork resolver. A display preference must not change what the backend does.
FETCH_PREFS_PATH = DEV_DIR / "fetch-repos.json"

# Reviewer surface (STAGE_DEV_GITEA S4). The verdict ledger is HOST-ONLY state:
# never bind-mounted, never written into gitea or any container (invariant 4 —
# verdicts are not agent-visible; the Development page, via the authenticated
# broker relay, is the only surface). Sibling of dev/, not inside it, so the
# webui-adjacent dev/ tree and the ledger stay separate concerns.
REVIEWS_DIR = Path.home() / ".research-sandbox" / "reviews"
# The dedicated reviewer Claude account's LONG-LIVED setup-token (PI ruling,
# 2026-07-24): minted once with `claude setup-token` (inference-only by
# design) and stored via `research dev reviewer-token` — a one-time host
# bootstrap step, the broker-passwd tier. Staged into each ephemeral reviewer
# container at spawn; it never rotates, so NOTHING credential-shaped ever
# returns from the untrusted container (the disposability principle — the
# OAuth stash + its capture-back are retired; an old .credentials.json here
# is inert residue).
REVIEWER_CRED_DIR = DEV_DIR / "reviewer"
REVIEWER_TOKEN_PATH = REVIEWER_CRED_DIR / "token"


def reviewer_token_state() -> dict:
    """Non-secret reviewer-token state for the Development/Fetch surfaces:
    {"present": bool, "set_at": str} — set_at is the file's mtime as UTC ISO
    ("" when absent/unreadable). NEVER reads token content into the payload:
    the value crosses to the browser, so this key set IS the boundary. Pure
    local file read — no docker, no network (the dev_status/dev_repo_status
    no-start posture depends on that)."""
    try:
        st = REVIEWER_TOKEN_PATH.stat()
    except OSError:
        return {"present": False, "set_at": ""}
    set_at = datetime.datetime.fromtimestamp(
        st.st_mtime, datetime.timezone.utc).isoformat(timespec="seconds")
    return {"present": True, "set_at": set_at}

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
# Email domain of every gitea account RS mints (`create_user`; the admin +
# operator bootstrap keep their own literal). A consumer account (`agent-…`)
# carries `<name>@rs.invalid`, and BOTH dev clone paths pin that same address
# as the agent's git `user.email` (rscore._run_dev_clone, and the dev block of
# agent/entrypoint.sandbox-box.sh — a bash MIRROR of this literal, since the
# entrypoint cannot import this module; pytest-pinned), so the fork's commits
# resolve to the gitea account that owns the fork (B36). `.invalid` is the
# RFC 2606 reserved TLD: never routable, never anyone else's.
EMAIL_DOMAIN = "rs.invalid"


def consumer_email(username: str) -> str:
    """The email of the gitea account `username` — ALSO the git `user.email`
    the dev clones pin as the agent's commit identity. One function for both,
    so the account and the commit identity cannot drift apart."""
    return f"{username}@{EMAIL_DOMAIN}"


# Mirror cron cadence. Gitea's default `mirror.MIN_INTERVAL` is 10m and it 500s
# a migrate that sets a shorter one — so 10m is the floor without weakening that
# guard. It's only a BACKSTOP anyway: `dev sync <repo>` (and the resume path)
# trigger an immediate pull when the human wants GitHub changes reflected now.
MIRROR_INTERVAL = "10m"
# `repos/search` page cap for `dev repo list`. A single-operator dev lane holds
# far fewer mirrors than this; truncation (loud, above) means "add pagination".
LIST_LIMIT = 100
# The Development-page read bound (repo_status: up to FIVE metadata GETs per
# repo on the broker's serial thread — forks list, mirror info, fork info,
# pulls, branches; an EMPTY or fork-less repo stops at three/two). A
# local-bridge gitea answers these in ms and a DOWN one refuses instantly —
# the bound only matters for a HALF-UP gitea, where the quick-call
# API_TIMEOUT_S (15s) would blow the webui's 30s relay window at a single
# repo. At 1s a gitea mid-GC could false-fail a healthy read; at 15s one repo
# exhausts the relay window. A many-repo half-up worst can still exceed the
# window — accepted (the daemon keeps working; only that one relayed page
# read reports unreachable), and dev_status's per-repo degradation keeps a
# slow/sick repo from taking the other rows with it.
STATUS_TIMEOUT_S = 5
# Bounded wait for gitea's async fork (202) to materialize. A small-repo fork is
# near-instant; 30×1s covers a busy gitea without hanging. At 5s a loaded gitea
# false-fails; at 300s a wedged fork would hold the caller 5 min.
FORK_WAIT_TRIES = 30

# Page size for the Development page's lazy per-row commit dropdowns (PR rows
# and branch rows alike — the dropdown pages, it does not clamp). The browser
# walks older history by asking for page 2, 3, … so this is a PAGE size, never
# a display ceiling. It MUST stay <= gitea's server-side list clamp
# (api.MAX_RESPONSE_ITEMS, default 50): a larger value is silently clamped
# server-side, which would make a full page look short and under-report the
# has-more flag below (which keys on len(rows) >= limit).
COMMITS_PAGE_SIZE = 5

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
                 "has_wiki": True, "has_projects": True,
                 # Linear-history contract: merge-commit and squash styles are
                 # BANNED on consumer forks so every landed change exists as an
                 # individual commit the human can walk (rs-fetch --auto-commit
                 # refuses merge-bearing ranges). fast-forward-only is the
                 # default; plain rebase stays allowed as the catch-up fallback
                 # DELIBERATELY — a server-side rebase rewrites shas, but the
                 # fetch walk keys on patch-id equivalence (sha-independent),
                 # so banning only the merge-commit producers (merge, squash,
                 # rebase_explicit) is sufficient.
                 "allow_merge_commits": False,
                 "allow_squash_merge": False,
                 "allow_rebase_explicit": False,
                 "allow_rebase": True,
                 "allow_fast_forward_only_merge": True,
                 "default_merge_style": "fast-forward-only"}
MIRROR_FEATURES = {"has_issues": False}


class GiteaError(Exception):
    """Any gitea-side failure. Message carries only method/path/status — never a
    request/response body, so a token in a migrate payload can never leak here.

    ``status`` is the HTTP status when one was actually received, else None. It
    exists so a caller can tell a DEFINITIVE 404 from "gitea is broken": _api
    otherwise collapses 404, 502, unreachable and timeout into one
    indistinguishable error, with the code surviving only inside the message
    string. Callers that refuse an operator request on a False MUST branch on
    it (see GiteaClient.branch_exists) — a swallow-everything bool there would
    report an outage as "that branch does not exist".

    NOTE: only _api sets it. The two inline urllib blocks (pull_diff /
    commit_diff) raise with status=None, which under the branch_exists rule
    means re-raise — the safe default."""

    def __init__(self, msg: str, status: int | None = None) -> None:
        super().__init__(msg)
        self.status = status


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

# The mirror's live reads (mirror_state / mirror_has_branch) are NOT here: they
# call gitea, so they live with the other client-using helpers below add_repo.


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
            raise GiteaError(f"gitea {method} {path} -> HTTP {e.code}",
                             status=e.code) from None
        except urllib.error.URLError as e:
            raise GiteaError(f"gitea {method} {path} unreachable: {e.reason}") from None
        except OSError as e:                    # timeout surfaces here
            raise GiteaError(f"gitea {method} {path} failed: {e}") from None
        if status not in ok:
            raise GiteaError(f"gitea {method} {path} -> HTTP {status}",
                             status=status)
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

    def branch_exists(self, owner: str, repo: str, branch: str) -> bool:
        """Does <branch> exist on <owner>/<repo>?

        DELIBERATELY NOT repo_exists' swallow-everything shape — do not
        "harmonize" the two. repo_exists' swallow is safe at ITS call sites:
        they are resume/idempotency decisions where False means "do the work"
        and a spurious retry costs nothing. Here False means "REFUSE the
        operator's request", so swallowing an outage would report a live branch
        as nonexistent and send them to fix their own input. Hence: True on a
        hit, False ONLY on a definitive 404, re-raise everything else so the
        caller can say "could not verify" instead of lying. Same strict-vs-
        best-effort split as consumer_forks vs list_forks."""
        try:
            self._api("GET", f"/repos/{owner}/{repo}/branches/"
                             f"{urllib.parse.quote(branch, safe='/')}")
            return True
        except GiteaError as e:
            if e.status == 404:
                return False
            raise

    def user_exists(self, username: str) -> bool:
        try:
            self._api("GET", f"/users/{username}")
            return True
        except GiteaError:
            return False

    def user_exists_strict(self, username: str) -> bool:
        """Does <username> exist? The STRICT sibling of user_exists — do not
        "harmonize" the two, for exactly the reason branch_exists spells out.
        user_exists' swallow is safe at ITS one call site (create_user, where
        False means "do the work" and a spurious retry costs nothing). Here
        False decides whether the caller REFUSES an operator's create, so
        swallowing an outage would report a surviving retired identity as
        absent and send the create on to a token mint that fails with the
        opaque message this whole surface exists to remove. True on a hit,
        False ONLY on a definitive 404, re-raise everything else."""
        try:
            self._api("GET", f"/users/{username}")
            return True
        except GiteaError as e:
            if e.status == 404:
                return False
            raise

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
        try:
            self._api("POST", "/repos/migrate", body, timeout=MIGRATE_TIMEOUT_S)
        except GiteaError as e:
            # Gitea CREATES the repo record and THEN clones into it, so a clone
            # that fails (no such GitHub repo, a bad PAT on a private source)
            # leaves a data-free mirror stub that `dev repo list` shows forever
            # while every consumer path correctly treats the repo as not added
            # (the stamp is never written). Sweep it — but ONLY when gitea
            # ANSWERED (a status-bearing error): a status-less error is a
            # timeout or an outage, and the clone may still be running
            # server-side, where deleting would kill a large migration that
            # merely outran MIGRATE_TIMEOUT_S; the resume path above heals that
            # stub on the next add instead.
            if e.status is not None:
                self._sweep_empty_stub(repo)
            raise

    def _sweep_empty_stub(self, repo: str) -> None:
        """Delete admin/<repo> if it exists and reads `empty` (the failed-migrate
        residue); never a repo carrying data. Best-effort: a failure here warns
        and the caller re-raises the migrate error it was already holding."""
        try:
            info = self._api("GET", f"/repos/{ADMIN_USER}/{repo}") or {}
        except GiteaError as e:
            if e.status == 404:
                return                           # nothing was left behind
            print(f"warning: could not inspect the failed migrate's leftover "
                  f"{ADMIN_USER}/{repo} ({e}); remove it from the Gitea tab if "
                  f"it lingers", file=sys.stderr)
            return
        if not isinstance(info, dict) or not info.get("empty"):
            return                               # has data: never delete
        self.delete_repo(ADMIN_USER, repo)

    def trigger_sync(self, repo: str) -> None:
        self._api("POST", f"/repos/{ADMIN_USER}/{repo}/mirror-sync")

    def create_user(self, username: str, password: str, admin: bool = False) -> None:
        """Idempotent create via the admin API."""
        if self.user_exists(username):
            return
        self._api("POST", "/admin/users", {
            "username": username,
            "password": password,
            "email": consumer_email(username),
            "must_change_password": False,
        })

    def fork_repo(self, src_owner: str, repo: str, as_user: str) -> None:
        """Fork admin/<repo> into <as_user>/<repo> (Sudo = act as that user).
        Resumable: skip if the fork already exists. Gitea forks ASYNCHRONOUSLY
        (202 Accepted), so accept 202 and wait, bounded, for the fork to
        materialize — the operator-grant that follows operates on it.

        The existence probe is INLINE (not repo_exists) so it can read the
        `archived` flag off the same GET — zero extra calls — because an
        ARCHIVED fork must NOT be adopted. Retiring a consumer archives its
        fork, and gitea archived repos are read-only (pushes, issues and
        comments all rejected), so silently reusing one hands the agent a fork
        it cannot write: a SILENT breakage, where refusing is loud. Un-archiving
        instead is deliberately not done — reviving a fork the operator chose to
        keep as history is not what reusing a name means.

        Strict semantics, the branch_exists split: definitive 404 => fork;
        archived => raise; anything else => raise (repo_exists would swallow an
        outage here and fall through to a POST that then fails)."""
        try:
            info = self._api("GET", f"/repos/{as_user}/{repo}") or {}
        except GiteaError as e:
            if e.status != 404:
                raise
            info = None                          # definitive miss -> fork below
        if info is not None:
            if isinstance(info, dict) and info.get("archived"):
                # Browser-reachable (box_add / dev_attach wrap this as a
                # ValidationError) — no CLI verb, and it must not promise the
                # purge is always one click away (a fork-less identity is not
                # listed anywhere).
                raise GiteaError(
                    f"the gitea identity {as_user!r} was retired and its fork "
                    f"of {repo!r} is archived (read-only), so it cannot be "
                    f"reused. Purge that retired identity from the Development "
                    f"page, or use a different project or box name")
            return                               # live fork — resume/heal path
        self._api("POST", f"/repos/{src_owner}/{repo}/forks", {}, sudo=as_user,
                  ok=(200, 201, 202))
        for _ in range(FORK_WAIT_TRIES):
            if self.repo_exists(as_user, repo):
                return
            time.sleep(1)
        raise GiteaError(f"fork {as_user}/{repo} did not appear in time")

    def set_default_branch(self, owner: str, repo: str, branch: str) -> None:
        """PATCH ONLY the default branch (unnamed EditRepoOption fields are
        preserved). Gitea answers 200 without changing anything when the repo
        does not carry <branch>, so the caller reads it back to know."""
        self._api("PATCH", f"/repos/{owner}/{repo}", {"default_branch": branch})

    def default_branch(self, owner: str, repo: str) -> str:
        data = self._api("GET", f"/repos/{owner}/{repo}") or {}
        return str(data.get("default_branch") or "") if isinstance(data, dict) else ""

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
        """One PR's metadata for the review header, the verdict's staleness
        stamp, and the verdict's own record of WHAT it reviewed (the branch and
        the fork the PR was opened on — a verdict outlives both its PR row and
        its branch, so anything it does not stamp is unrecoverable later).
        Fields picked by name.

        `fork_id` comes from the BASE side: the page lists PRs by the repo they
        were opened INTO, so that is the id a status read holds to match
        against. Gitea serves it flat (`base.repo_id`) and nested; the flat
        field survives a deleted head repo, where the nested object is null."""
        data = self._api("GET", f"/repos/{owner}/{repo}/pulls/{index}")
        if not isinstance(data, dict):
            raise GiteaError(f"gitea GET pull {owner}/{repo}#{index} -> no data")
        base = data.get("base") or {}
        return {"title": data.get("title") or "",
                "body": data.get("body") or "",
                "state": data.get("state") or "",
                "merged": bool(data.get("merged")),
                "head": (data.get("head") or {}).get("ref") or "",
                "fork_id": (base.get("repo_id")
                            or (base.get("repo") or {}).get("id") or 0),
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

    def commit_info(self, owner: str, repo: str, sha: str) -> dict:
        """One commit's metadata (subject + full message) for the commit-review
        header — the pull_info mold, fields picked by name. _COMMITS_QS keeps
        the read metadata-cheap (no diffstat/verification/files)."""
        data = self._api("GET", f"/repos/{owner}/{repo}/git/commits/{sha}"
                                f"?{_COMMITS_QS}")
        if not isinstance(data, dict):
            raise GiteaError(
                f"gitea GET commit {owner}/{repo}@{sha} -> no data")
        msg = (data.get("commit") or {}).get("message") or ""
        return {"subject": msg.splitlines()[0] if msg else "",
                "message": msg}

    def commit_diff(self, owner: str, repo: str, sha: str,
                    max_bytes: int) -> str:
        """One commit's raw unified diff (gitea's commit `.diff` endpoint) —
        the pull_diff shape verbatim: reads max_bytes + 1 so overflow is
        detected exactly; the overflow error names sizes only, never content
        (the diff is adversarial input and the message may reach a client
        envelope)."""
        path = f"/repos/{owner}/{repo}/git/commits/{sha}.diff"
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
        """Delete a user AND its repos (purge=true; gitea refuses to delete a
        user that still owns repos otherwise). RAISES — deliberately not the
        best-effort swallow the teardown helpers use: the sole caller
        (purge_consumer) exists to FREE a username, and a swallowed failure
        would report success while the name stays blocked, which is precisely
        the confusing state this surface was built to end."""
        self._api("DELETE", f"/admin/users/{username}?purge=true")


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
    # deleted token FILE whose gitea-side "rs-dev" token still lives fails
    # re-mint. There is NO admin-side delete for another user's token (gitea
    # gates /users/{u}/tokens behind reqBasicAuth AS that user; Sudo does not
    # bypass it and the admin CLI has no delete subcommand), and consumer
    # retirement (destroy / box remove / detach) keeps the user — so nothing
    # clears it at retirement. That is why reusing a retired identity is
    # REFUSED early (retired_identity, checked by create/box_add/dev_attach)
    # and freeing the name means purging the whole user (purge_consumer — via a
    # Development-page row, `dev fork purge`, or mirror Remove, which purges the
    # fully retired owners of the forks it deletes): the callers keep this
    # function from ever meeting a surviving token.
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
    tok = _mint_token(user, "write:repository,write:issue")
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


# Live mirror reads. Named 'state'/'has' rather than a *_present name: beside
# the pure-file mirror_present these DO call gitea and DO raise, and a
# *_present sibling would read as "cheap bool, never raises". Both need a
# RUNNING gitea — the stamp check is not a liveness signal.


def mirror_state(host_port: str, repo: str) -> dict:
    """The mirror's {empty, default_branch} in one GET. `empty` is a legitimate
    state, not a fault: gitea treats a zero-commit GitHub source as a SUCCESSFUL
    migrate, and an empty repo has no branches at all — so a caller resolving a
    branch must check it FIRST (the branch route 404s on an empty repo, which
    would otherwise surface as 'no such branch')."""
    client = GiteaClient(api_base(host_port), read_admin_token())
    data = client._api("GET", f"/repos/{ADMIN_USER}/{repo}")
    if not isinstance(data, dict):
        raise GiteaError(f"gitea GET repo {ADMIN_USER}/{repo} -> no data")
    return {"empty": bool(data.get("empty")),
            "default_branch": data.get("default_branch") or ""}


def mirror_has_branch(host_port: str, repo: str, branch: str) -> bool:
    """Does the mirror carry <branch>? Raises on anything but a definitive
    miss — see GiteaClient.branch_exists for why that matters here."""
    client = GiteaClient(api_base(host_port), read_admin_token())
    return client.branch_exists(ADMIN_USER, repo, branch)


def provision_consumer(host_port: str, repo: str, user: str,
                       default_branch: str = "") -> None:
    """Create-or-reuse one consumer's identity for one repo: gitea user +
    mirror read-grant + fork into the consumer namespace + operator read-grant
    on the fork (the universal-fetch valve) + the fork's issue channel + a
    user-scoped token file. Every stage is exists-checked/idempotent (the
    add-repo resume discipline), so a re-run heals a fork that predates the
    feature block.

    ``default_branch`` (the resolved dev base, when the caller has one) is set
    on the FORK — never the mirror, whose default is GitHub's — so a PR opened
    by hand in the Gitea UI pre-selects the base every RS tool already names.
    A convenience, never a floor: any failure warns and continues, and a silent
    no-change (gitea 200s a branch the fork lacks) is detected by read-back."""
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
    if default_branch:
        try:
            client.set_default_branch(user, repo, default_branch)
            got = client.default_branch(user, repo)
        except GiteaError as e:
            print(f"warning: could not set the default branch of {user}/{repo} "
                  f"to {default_branch!r} ({e}); PRs opened by hand in the "
                  f"Gitea tab will pre-select the fork's current default",
                  file=sys.stderr)
        else:
            if got != default_branch:
                print(f"warning: {user}/{repo} kept default branch {got!r} — "
                      f"it does not carry {default_branch!r} yet; PRs opened by "
                      f"hand in the Gitea tab will pre-select {got!r}",
                      file=sys.stderr)
    mint_or_rotate_token(user)                    # writes tokens/<user>.token


def archive_fork(host_port: str, user: str, repo: str) -> None:
    """Read-only-freeze a retired consumer's fork — history kept, never
    deleted. The gitea USER is deliberately kept inert until its mirror is
    removed (deleting a user PURGES its repos, including any other fork it
    holds; remove_repo purges the user only once its fork is gone and it owns
    nothing else). FULLY best-effort — an
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


def retired_identity(host_port: str, user: str) -> bool:
    """Is <user> a SURVIVING RETIREMENT — a gitea consumer whose host token file
    is gone? That pair is the exact precondition of the token-mint failure:
    retirement unlinks the file but never deletes the gitea user, whose
    fixed-name 'rs-dev' token then blocks the re-mint (see _mint_token). Callers
    (create / box_add / dev_attach) use this to REFUSE BEFORE any side effect,
    instead of dying deep in provisioning with a half-built project standing.

    STRICT by construction (user_exists_strict raises on anything but a
    definitive miss): a False here sends the caller on to provisioning, so an
    outage read as 'absent' would resurrect exactly the opaque failure this
    replaces. Read-only — no writes on any path."""
    client = GiteaClient(api_base(host_port), read_admin_token())
    return (client.user_exists_strict(user)
            and not consumer_token_path(user).is_file())


def user_repos(host_port: str, user: str) -> list[dict]:
    """Every repo <user> owns: [{repo, archived}] — the STRICT enumeration, a
    GiteaError PROPAGATES.

    Sibling of consumer_forks, and the posture is the point: this one backs a
    DESTRUCTIVE gate (dev_purge_consumer refuses when any of the identity's
    forks is still live), so a []-on-error form would read an unverifiable
    gitea as 'this identity owns nothing' and wave the purge through. Fail
    closed; never add a best-effort wrapper for a gate to call.

    An ABSENT user maps to [] rather than raising — a purge of something already
    gone is a no-op success, not an error (see purge_consumer)."""
    client = GiteaClient(api_base(host_port), read_admin_token())
    try:
        raw = client._api("GET", f"/users/{user}/repos?limit={LIST_LIMIT}",
                          timeout=STATUS_TIMEOUT_S) or []
    except GiteaError as e:
        if e.status == 404:
            return []
        raise
    rows = raw if isinstance(raw, list) else []
    # NO SILENT CAP on a fail-closed gate. A full page means there may be more
    # repos we never saw, and "we did not see a live fork" would then be an
    # artefact of pagination — fail-OPEN in the delete direction, the one
    # direction that cannot be taken back. list_repos WARNS here because it
    # feeds a display; this feeds a delete, so it takes the posture stated three
    # lines up: an unverifiable state is never a licence to delete.
    if len(rows) >= LIST_LIMIT:
        raise GiteaError(
            f"{user!r} owns at least {LIST_LIMIT} repos — cannot enumerate them "
            f"in one page, so its fork state cannot be verified")
    out = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        out.append({"repo": r.get("name") or "",
                    "archived": bool(r.get("archived"))})
    return out


def purge_consumer(host_port: str, user: str) -> list[str]:
    """Delete a consumer identity outright: the gitea USER and every repo it
    owns, plus its host token file. Returns the repo names deleted.

    ⚠ USER-SCOPED: this deletes EVERY fork the identity owns, on every repo —
    callers that surface it from a repo-scoped UI must say so.

    Carries NO fork-state gate, deliberately. Its callers need different
    things and each gates at its own entry point with the proof the others
    cannot hold: RECOVERY (dev_purge_consumer) lets an operator name an
    arbitrary user, so it fails closed on both the ledger and a live fork
    BEFORE calling this; MIRROR REMOVE (remove_repo → _purge_retired_owner)
    reaches only the owners of forks the verb's live-fork gate proved archived,
    and gates on the ledger + owns-nothing-else itself; a purge-at-RETIREMENT
    (destroy / box remove — pending B53, today those archive) would purge forks
    that are still LIVE, its proof of ownership being the ledger entry it is
    about to prune. Putting the archived-only check in here would make that
    last path structurally impossible.

    The prefix floor stays HERE as well as in the request validator: this is the
    function holding the delete, and 'operator' / 'sandbox-admin' match the
    username shape while owning zero (resp. unarchived) repos.

    Idempotent: an absent user is a no-op success. Ordering is load-bearing —
    delete_user RAISES now, so a failure leaves the token file in place rather
    than stranding a live gitea identity with no host record of it."""
    if not user.startswith(AGENT_USER_PREFIX):
        raise GiteaError(
            f"refusing to purge {user!r}: only dev-lane agent identities "
            f"({AGENT_USER_PREFIX}*) can be purged")
    client = GiteaClient(api_base(host_port), read_admin_token())
    if not client.user_exists_strict(user):
        return []                                  # already gone — no-op success
    repos = [r["repo"] for r in user_repos(host_port, user) if r["repo"]]
    client.delete_user(user)
    delete_consumer_token(user)
    return repos


def _purge_retired_owner(host_port: str, owner: str) -> bool:
    """Mirror Remove's per-owner purge, run AFTER the owner's fork on the
    removed mirror is deleted and AFTER the ledger gate (G1 in remove_repo)
    passed. Decides whether the owner is FULLY retired — owns nothing else in
    gitea — and if so deletes its user (purge_consumer). WARN-and-continue on
    every arm: this runs inside remove_repo's cascade and must never abort the
    mirror delete.

    G2 reads LIVE state (user_repos, strict), not the ledger: a repo the owner
    still holds may be a live orphan fork of another mirror, an archived fork
    listed on that mirror's Development-page card, or a hand-made repo — none
    of which mirror Remove was asked to delete, and deleting the user would
    purge them all. The owner is then KEPT, and the warning names both browser
    remedies (its repo card, or Site Administration in the Gitea tab) without
    promising a card that a non-fork or a live orphan does not have.

    Returns True when the user is GONE NOW: purge_consumer returns [] both for
    'deleted' and for 'already absent', and the only way to meet the second is
    an owner enumerated seconds earlier vanishing mid-cascade — accepted
    without a probe; the caller reports the name as purged either way."""
    try:
        owned = user_repos(host_port, owner)
    except GiteaError as e:
        print(f"warning: could not verify what {owner!r} owns ({e}); left in "
              f"Gitea — delete the user under Site Administration in the Gitea "
              f"tab", file=sys.stderr)
        return False
    names = sorted(r["repo"] for r in owned if r.get("repo"))
    if names:
        print(f"warning: kept {owner!r}: it also owns {', '.join(names)} — "
              f"purge it from that repo's card on the Development page if that "
              f"is an archived fork, or delete the user under Site "
              f"Administration in the Gitea tab", file=sys.stderr)
        return False
    try:
        purge_consumer(host_port, owner)
    except GiteaError as e:
        print(f"warning: could not delete {owner!r} ({e}); left in Gitea — "
              f"delete the user under Site Administration in the Gitea tab",
              file=sys.stderr)
        return False
    return True


def remove_repo(host_port: str, repo: str) -> list[str]:
    """Delete the mirror + best-effort every consumer FORK on it, and purge the
    gitea USER (+ host token) of every fork owner that is FULLY RETIRED: no
    attachment-ledger row names it anywhere (G1, a file read) and it owns
    nothing else once its fork is gone (G2, live state). An owner some project
    still records — the B53 truncation collision, two consumers on one gitea
    username sharing ONE token file — keeps BOTH its token and its user; an
    owner that owns anything else keeps its user (its token goes: this fork's
    retirement already ended the token's use). WARN-and-continue on every
    cascade arm: a fork that survives, an enumeration failure, or a refused
    user delete prints the manual-cleanup remedy instead of failing silently or
    aborting the mirror delete. Cleans the mirror stamp + the active-fork
    entry. Returns the users purged (the names freed for reuse).

    Why the user goes with the fork: a retired identity is visible on the
    Development page ONLY through an archived fork it owns, so deleting the
    fork while keeping the user used to leave an invisible identity that still
    blocked its project or box name (B52's normal-flow source)."""
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
    purged: list[str] = []
    for f in forks:
        owner = f["user"]
        # 1. The fork on THIS mirror goes regardless of what follows (the verb's
        #    live-fork gate already guarantees it is archived).
        deleted = True
        try:
            client._api("DELETE", f"/repos/{owner}/{repo}")
        except GiteaError as e:
            deleted = False
            print(f"warning: could not delete fork {owner}/{repo} ({e}); "
                  f"delete it manually in the gitea UI", file=sys.stderr)
        # 2. G1 — the ledger, BEFORE the token unlink: an owner any project
        #    still records shares its token file with that project (one file
        #    per gitea user), so unlinking it here would break that project at
        #    its next recreate. Reads `user`, never project/repo: this repo's
        #    rows were already refused by the verb's attached_projects gate, so
        #    a hit is a row for ANOTHER repo — the collision case.
        named = sorted({e["project"] for e in load_attachments()
                        if e.get("user") == owner and e.get("project")})
        if named:
            print(f"warning: kept {owner!r}: still recorded for "
                  f"{', '.join(named)} — its token and Gitea user stay",
                  file=sys.stderr)
            continue
        # 3. Nothing records the owner: its token file has no user left.
        delete_consumer_token(owner)
        # 4. A fork that survived the delete would 422 the user delete
        #    (ErrUserOwnRepos); the warning above already names the remedy.
        if not deleted:
            continue
        # 5. G2 + the purge.
        if _purge_retired_owner(host_port, owner):
            purged.append(owner)
    client.delete_repo(ADMIN_USER, repo)
    try:
        mirror_stamp_path(repo).unlink()
    except FileNotFoundError:
        pass
    active = load_active_forks()
    if repo in active:
        del active[repo]
        save_active_forks(active)
    # The rs-fetch visibility preference goes with the repo, exactly as the
    # active-fork entry above does. Without this, removing a HIDDEN repo and
    # later re-adding it under the same name (the documented way to refresh a
    # private repo's PAT) brings the mirror back already hidden, with nothing
    # on screen to explain why — and the "new repos are visible" default would
    # be false for the one case where it matters most.
    prefs = load_fetch_prefs()
    if repo in prefs:
        del prefs[repo]
        save_fetch_prefs(prefs)
    return purged


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


def load_fetch_prefs() -> dict:
    """The {repo: bool} rs-fetch visibility map. TOLERANT by design (the
    load_active_forks posture): absent, unreadable or malformed all read as "no
    preferences recorded" rather than raising — this is consulted on every
    Development-page read, and a corrupt preference file must never take the
    page down. Its worst case is that everything shows, which is the default."""
    if not FETCH_PREFS_PATH.is_file():
        return {}
    try:
        data = json.loads(FETCH_PREFS_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def save_fetch_prefs(entries: dict) -> None:
    DEV_DIR.mkdir(parents=True, exist_ok=True)
    tmp = FETCH_PREFS_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(entries, indent=2, sort_keys=True))
    tmp.replace(FETCH_PREFS_PATH)


def fetch_enabled(repo: str) -> bool:
    """Is `repo` shown on the webui's rs-fetch lists? Default TRUE — a repo with
    no recorded preference is visible, so a newly mirrored repo needs no write to
    appear and a deleted preference file restores the default everywhere."""
    return bool(load_fetch_prefs().get(repo, True))


def set_fetch_enabled(repo: str, enabled: bool) -> None:
    """Record the preference. The explicit True is STORED rather than the key
    deleted: the write stays idempotent and the file says what was chosen."""
    prefs = load_fetch_prefs()
    prefs[repo] = bool(enabled)
    save_fetch_prefs(prefs)


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


def _count_or_none(value: Any) -> "int | None":
    """A gitea counter as an int, or None when it is absent or not a number.
    None and 0 mean different things on the Development surfaces — "not known"
    vs "none open" — so a missing counter must never collapse into a zero."""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


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
    fork_empty = False
    # The ACTIVE fork's own gitea repo id — what a review verdict is filed
    # under, so the badge feed can tell this fork's PR #N from another
    # consumer's (or from a purged predecessor's, which gets a NEW id).
    active_id = 0
    # Gitea's own maintained counters on the ACTIVE FORK — None (not 0) when
    # there is no live consumer fork. See the field-picking block below.
    open_issues = open_prs = None
    if active:
        # The ACTIVE FORK's own info decides emptiness — the MIRROR's does
        # not suffice (a synced mirror + an agent that never pushed leaves
        # the fork empty). Gitea 404s the pulls list (and most repo
        # sub-APIs) on an EMPTY repo, and an empty fork is a NORMAL state: a
        # zero-commit GitHub source migrates "successfully" as empty (the
        # migrate_mirror stub-heal quirk) and its fork is born empty, as is
        # any fresh consumer fork before the agent's first push. So the
        # 404-prone GETs are SKIPPED, not attempted-and-tolerated (B37).
        fork_info = client._api("GET", f"/repos/{active}/{repo}",
                                timeout=STATUS_TIMEOUT_S) or {}
        fork_empty = bool(fork_info.get("empty"))
        active_id = fork_info.get("id") or 0
        # FREE: read off the fork info this call already made. `open_issues_count`
        # and `open_pr_counter` are DISJOINT on the repo JSON (measured on gitea
        # 1.25.1: filing an issue moved the first and left the second alone), so
        # this is not the issues API's "a list of issues also contains PRs"
        # behaviour. Read from the FORK, never the mirror: the mirror's issue unit
        # is off at migrate time and it has no pulls unit, so its counters are
        # structurally zero — reporting them would claim "0 issues, 0 PRs" about a
        # repo nobody has ever worked, which is exactly the state a reader needs to
        # be able to tell apart from a quiet one.
        open_issues = _count_or_none(fork_info.get("open_issues_count"))
        open_prs = _count_or_none(fork_info.get("open_pr_counter"))
        if not fork_empty:
            prs_raw = client._api(
                "GET",
                f"/repos/{active}/{repo}/pulls?state=open&limit={LIST_LIMIT}",
                timeout=STATUS_TIMEOUT_S) or []
            branches_raw = client._api(
                "GET", f"/repos/{active}/{repo}/branches",
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
            "active_id": active_id,
            "empty": fork_empty,
            "open_issues": open_issues,
            "open_prs": open_prs,
            "prs": prs,
            "branches": branches}


# --- per-row commit lists (the Development-page dropdowns) --------------------

def _require_active_fork(host_port: str, repo: str) -> str:
    """The active fork or a RAISE — the commit reads are row-level: the page
    only renders PR/branch rows when a fork exists, so reaching them with no
    live fork is abnormal and deserves a loud error, not an empty dropdown."""
    active = active_fork_for(host_port, repo)
    if not active:
        raise GiteaError(f"no live agent fork for {repo!r} (add a dev "
                         f"project/box on it first)")
    return active


def _commit_rows(raw: Any, limit: int) -> tuple[list[dict], bool]:
    """Field-picked commit rows, NEWEST-FIRST as displayed — gitea's own order,
    kept (the dropdown renders newest at the top and pages older downward, so
    the fetch WALK runs bottom-up). Picked by NAME so nothing unexpected can
    enter the result. has_more means a FULL page came back, so another page may
    exist — measured against the page gitea actually returned."""
    rows = []
    for c in raw if isinstance(raw, list) else []:
        if not isinstance(c, dict):
            continue
        commit = c.get("commit") or {}
        msg = commit.get("message") or ""
        rows.append({"sha": c.get("sha") or "",
                     "subject": msg.splitlines()[0] if msg else "",
                     "date": ((commit.get("committer") or {}).get("date")
                              or (commit.get("author") or {}).get("date")
                              or "")})
    has_more = len(rows) >= limit
    return rows, has_more


# Both commit endpoints compute per-commit diffstats by default; the dropdown
# needs none of that on the broker's serial accept thread — these toggles make
# the reads metadata-cheap.
_COMMITS_QS = "stat=false&verification=false&files=false"


def pr_commits(host_port: str, repo: str, pr: int,
               page: int = 1) -> tuple[list[dict], bool]:
    """One PAGE of a PR's commits on the ACTIVE fork, newest-first:
    (rows, has_more). `page` is 1-based; a page past the end returns []."""
    active = _require_active_fork(host_port, repo)
    client = GiteaClient(api_base(host_port), read_admin_token())
    raw = client._api(
        "GET", f"/repos/{active}/{repo}/pulls/{pr}/commits"
               f"?page={page}&limit={COMMITS_PAGE_SIZE}&{_COMMITS_QS}") or []
    return _commit_rows(raw, COMMITS_PAGE_SIZE)


def branch_commits(host_port: str, repo: str, branch: str,
                   page: int = 1) -> tuple[list[dict], bool]:
    """One PAGE of a branch's commits on the ACTIVE fork, newest-first:
    (rows, has_more). `page` is 1-based; a page past the end returns [].
    The branch name is the first client-supplied FREE-TEXT value to reach this
    client's URL builder (every other path piece is regex-validated or
    gitea-sourced), so it is URL-quoted — `&`/`#`/`?` are all legal in git ref
    names and would otherwise corrupt the query."""
    active = _require_active_fork(host_port, repo)
    client = GiteaClient(api_base(host_port), read_admin_token())
    raw = client._api(
        "GET", f"/repos/{active}/{repo}/commits"
               f"?sha={urllib.parse.quote(branch, safe='')}"
               f"&page={page}&limit={COMMITS_PAGE_SIZE}&{_COMMITS_QS}") or []
    return _commit_rows(raw, COMMITS_PAGE_SIZE)


def mirror_branch_head(host_port: str, repo: str, branch: str) -> str:
    """The MIRROR's head sha for <branch>, or "" when the mirror has no branch
    of that name. The Fetch-tab dropdown's boundary marker: everything newer
    than this sha in the fork's list is work the human's repo does not carry.

    EXACT ON A LINEAR BRANCH, which is what a consumer fork has: FORK_FEATURES
    bans the merge-commit producers repo-side and the dev instructions merge
    upstream fast-forward-only, so same-sha implies same transitive ancestry
    and "this commit and everything below it are in the mirror" holds. Those
    are PR-merge settings plus instruction, not branch protection — a locally
    merged commit pushed straight to the base branch would make the marker
    UNDER-report (rows below it not yet in the mirror). It can never
    over-report, because a divider is only drawn on an exact sha hit.

    STATUS_TIMEOUT_S, not branch_commits' default API_TIMEOUT_S: this is a
    decoration on a list that has already loaded, and it must not out-wait the
    data it decorates.

    404 -> "" (the ordinary "no counterpart upstream" case); every other error
    RAISES, the branch_exists split — the caller decides how to degrade. The
    branch is a path segment against gitea's wildcard branches route, so it is
    quoted with safe='/' (a `feature/foo` name must keep its slash); contrast
    branch_commits, where the branch is a query VALUE and safe='' is right."""
    client = GiteaClient(api_base(host_port), read_admin_token())
    try:
        resp = client._api(
            "GET", f"/repos/{ADMIN_USER}/{repo}/branches/"
                   f"{urllib.parse.quote(branch, safe='/')}",
            timeout=STATUS_TIMEOUT_S)
    except GiteaError as e:
        if e.status == 404:
            return ""
        raise
    # _api returns None on an empty or unparseable body, so the strict guard
    # (pull_info/mirror_info's shape) is what keeps a malformed 200 inside the
    # GiteaError channel instead of raising AttributeError past the broker's
    # catch set — an escape there truncates the reply with no envelope.
    if not isinstance(resp, dict):
        raise GiteaError(f"gitea GET branch {ADMIN_USER}/{repo}@{branch} "
                         f"-> no data")
    commit = resp.get("commit") or {}
    # `id` is the PayloadCommit sha field (the same object repo_status reads
    # `timestamp` off); the `sha` fallback costs one `or` and cannot invent a
    # wrong divider — an unexpected value simply matches no row — while a
    # silently-renamed field would otherwise mean "no divider, ever".
    return commit.get("id") or commit.get("sha") or ""


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
                      user: str = "", branch: str = "") -> None:
    """Add/refresh a consumer's entry. Keyed on (project, class, repo, box) so
    a re-wire refreshes gitea_ip in place rather than duplicating — and so a
    box entry never clobbers the project's own entry for the same repo.

    ``branch`` is the consumer's base branch. This REPLACES on the key like
    every other field, so a caller re-recording an existing entry must pass the
    value forward explicitly (see the IP-refresh loop and dev_attach, which both
    read the prior entry) — omitting it BLANKS a branch the operator chose.
    Deliberately not a None-means-preserve sentinel: `user` already replaces on
    every re-record, and mixing the two semantics in one function is a footgun."""
    entries = [e for e in load_attachments()
               if not (e.get("project") == project and e.get("class") == klass
                       and e.get("repo") == repo and e.get("box") == box)]
    entries.append({"project": project, "class": klass, "repo": repo,
                    "gitea_ip": gitea_ip, "box": box, "user": user,
                    "branch": branch})
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
# Host-only (see the REVIEWS_DIR comment). Three file shapes share one per-repo
# directory, kept disjoint by their name prefixes under every loader's glob:
#
#   pr-<pr>-<fork id>-<head sha>.json   one PR review, named by WHAT it read
#   commit-<sha>.json                   one commit review
#   <pr>.json                           LEGACY: the pre-content-addressing PR
#                                       shape — read, never written again
#
# A PR verdict is filed under the fork it belonged to AND the head commit it
# reviewed, so no later review can overwrite what an earlier one recorded: a
# re-review at a new head, a second consumer's fork, and a purged-and-recreated
# fork (gitea gives the new repo a NEW id) each land on their own file. That is
# what makes this a HISTORY rather than a set of current-state markers — a
# landed PR's review stays readable long after its row is gone, which is the
# whole point of the Development page's Reviews tab. Commit entries stay
# one-per-sha: the same sha is the same bytes, so a re-review supersedes.
#
# Schema — fields picked by name, coarse failure reasons only (a review error
# can carry host paths; raw detail lives in the broker's host-only full log):
#   {repo, pr, head_sha, fork, fork_id, title, head, status: "ok"|"failed",
#    reason?: <coarse token>, risk?: <free-text label, parse-capped>,
#    outcome?: "pass"|"fail", summary?, findings?: [{file, note}], reviewed_at,
#    model?}
# The commit twin carries {repo, commit, subject} in place of pr/head_sha/head.
# Failed entries are written too (from the diff-fetch step onward) so the page
# can show WHY nothing usable exists. Every stamp beyond the original set is
# OPTIONAL on read — an entry written before they existed still renders.

PR_ENTRY_PREFIX = "pr-"
# A head sha reaches a FILENAME, so it is validated exactly the way the commit
# locator already is (rscore's ReviewRequest): a gitea payload is not a trusted
# path segment, and one carrying a separator must not escape the repo's dir.
_SHA_LENGTHS = (40, 64)
_HEX_DIGITS = "0123456789abcdef"
# The one degenerate name: a PR entry whose head sha gitea never gave us. That
# fork+PR stays last-writer-wins, deliberately — there is nothing to key on.
UNKNOWN_SHA_TOKEN = "unknown"


def valid_sha(sha: Any) -> bool:
    """A full lowercase-hex commit id — the only shape allowed into a path."""
    return (isinstance(sha, str) and len(sha) in _SHA_LENGTHS
            and all(c in _HEX_DIGITS for c in sha))


def _sha_token(head_sha: Any) -> str:
    return head_sha if valid_sha(head_sha) else UNKNOWN_SHA_TOKEN


def _fork_token(fork_id: Any) -> str:
    """A fork's gitea repo id as a name segment. A missing or odd id degrades
    to "0" rather than raising: an unattributable entry is still worth keeping,
    and "0" simply never matches a live fork."""
    try:
        n = int(fork_id)
    except (TypeError, ValueError):
        return "0"
    return str(n) if n > 0 else "0"


def pr_verdict_path(repo: str, pr: int, fork_id: Any, head_sha: Any) -> Path:
    return (REVIEWS_DIR / repo
            / f"{PR_ENTRY_PREFIX}{int(pr)}-{_fork_token(fork_id)}"
              f"-{_sha_token(head_sha)}.json")


def _atomic_write(path: Path, payload: dict) -> Path:
    """The tmp name carries the writer's PID: reviews run in PARALLEL (S4/F5),
    and the shared `.with_suffix(".json.tmp")` idiom would let two writers
    interleave on one tmp file (A truncated by B, then A renames B's
    half-written bytes)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    tmp.replace(path)
    return path


def save_verdict(repo: str, pr: int, payload: dict) -> Path:
    """Write ONE PR verdict, named from the payload's OWN fork id and head sha
    so the filename and the record can never disagree."""
    return _atomic_write(
        pr_verdict_path(repo, pr, payload.get("fork_id"),
                        payload.get("head_sha")), payload)


def _read_entry(f: Path) -> dict | None:
    """Tolerant read: absent/corrupt/non-dict → None (a lost verdict re-reviews)."""
    try:
        data = json.loads(f.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def load_repo_pr_verdicts(repo: str) -> list[dict]:
    """EVERY PR verdict recorded for a repo — the content-addressed files and
    the legacy flat ones alike. Legacy entries stay readable so an operator's
    existing history, and the badges on their currently-open PRs, survive this
    change. Pure host-file reads (the page read stays structurally no-start).

    A list, not a map: one PR can now hold several verdicts (one per head it
    was reviewed at, per fork), which is exactly the history that was being
    thrown away before."""
    d = REVIEWS_DIR / repo
    if not d.is_dir():
        return []
    out: list[dict] = []
    for f in d.glob("*.json"):
        # isdigit() is true for characters int() rejects (superscripts, other
        # unicode digits), and the legacy name is converted, not just matched —
        # so the ascii test is what keeps a hand-made file from raising through
        # the page read.
        legacy = f.stem.isdigit() and f.stem.isascii()
        if not legacy and not f.stem.startswith(PR_ENTRY_PREFIX):
            continue                       # commit-<sha>.json, or anything else
        data = _read_entry(f)
        if data is None:
            continue
        entry = dict(data)
        entry.setdefault("repo", repo)
        if legacy:
            entry.setdefault("pr", int(f.stem))
        out.append(entry)
    return out


def select_pr_verdicts(entries: list[dict], prs: list[Any],
                       fork: str, fork_id: Any) -> dict[str, dict]:
    """The badge feed: per OPEN PR row, the ONE verdict that may be shown
    against it, keyed by str(pr) — the shape the page has always consumed.

    A verdict qualifies only when it belongs to the SAME fork, so a second
    consumer's PR #1, or a purged fork's, can never decorate this one. Among a
    row's own verdicts an exact head-sha match wins (the page renders it as a
    current review); failing that the newest is offered and the page renders it
    stale against the row's head. A LEGACY entry predates the fork stamp and
    cannot be attributed, so it qualifies only on an exact head-sha match — it
    is trusted precisely when it demonstrably read the code now on the row."""
    # want_id is "0" only when the caller has no active fork, and a repo with
    # no active fork has no PR rows either — so the username fallback below is
    # never the sole discriminator on a real page.
    want_id = _fork_token(fork_id)
    heads: dict[str, str] = {}
    for p in prs if isinstance(prs, list) else []:
        if isinstance(p, dict) and p.get("number") is not None:
            heads[str(p["number"])] = p.get("sha") or ""
    best: dict[str, tuple] = {}
    out: dict[str, dict] = {}
    for e in entries:
        key = str(e.get("pr"))
        if key not in heads:
            continue
        exact = bool(e.get("head_sha")) and e["head_sha"] == heads[key]
        stamped = bool(e.get("fork_id")) or bool(e.get("fork"))
        if stamped:
            same_fork = (_fork_token(e.get("fork_id")) == want_id
                         if e.get("fork_id") and want_id != "0"
                         else e.get("fork") == fork and bool(fork))
            if not same_fork:
                continue
        elif not exact:
            continue                       # unattributable and not this code
        rank = (1 if exact else 0, str(e.get("reviewed_at") or ""))
        if key not in best or rank > best[key]:
            best[key], out[key] = rank, e
    return out


def load_all_verdicts() -> list[dict]:
    """Every verdict on the host, newest first — the review history read. A
    pure filesystem walk: no gitea call, no docker, nothing to start, so the
    history stands with gitea stopped and after the last repo mirror is gone
    (which is the durability the badges never had)."""
    if not REVIEWS_DIR.is_dir():
        return []
    out: list[dict] = []
    for d in sorted(REVIEWS_DIR.iterdir()):
        if not d.is_dir():
            continue
        for e in load_repo_pr_verdicts(d.name):
            out.append({**e, "repo": e.get("repo") or d.name, "kind": "pr"})
        for sha, e in load_repo_commit_verdicts(d.name).items():
            out.append({**e, "repo": e.get("repo") or d.name, "kind": "commit",
                        "commit": e.get("commit") or sha})
    out.sort(key=lambda e: str(e.get("reviewed_at") or ""), reverse=True)
    return out


# Per-COMMIT verdicts live beside the PR ones as commit-<sha>.json. The
# `commit-` prefix is load-bearing: the PR loader takes a digit stem (legacy)
# or the `pr-` prefix, and a 40-hex sha CAN be all-decimal — a bare <sha>.json
# could masquerade as a legacy PR entry. The three shapes stay structurally
# disjoint in every direction: a sha is hex so it can never start `pr-`, a PR
# number is an integer so it can never start `commit-`, and neither loader's
# `*.json` glob matches the `.tmp` files an in-flight write leaves.
# Schema mirrors the PR entry minus head_sha/pr, plus the commit subject:
#   {repo, commit, status: "ok"|"failed", reason?, risk?, outcome?, summary?,
#    findings?, reviewed_at}

def commit_verdict_path(repo: str, sha: str) -> Path:
    return REVIEWS_DIR / repo / f"commit-{sha}.json"


def save_commit_verdict(repo: str, sha: str, payload: dict) -> Path:
    """Atomic per-commit write — one file per sha, superseded by a re-review."""
    return _atomic_write(commit_verdict_path(repo, sha), payload)


def load_repo_commit_verdicts(repo: str) -> dict[str, dict]:
    """All of a repo's COMMIT ledger entries keyed by sha — the dev_commits
    merge. Pure host-file reads (the page read stays structurally no-start)."""
    d = REVIEWS_DIR / repo
    if not d.is_dir():
        return {}
    out: dict[str, dict] = {}
    for f in d.glob("commit-*.json"):
        sha = f.stem[len("commit-"):]
        if not sha:
            continue
        data = _read_entry(f)
        if data is not None:
            out[sha] = data
    return out
