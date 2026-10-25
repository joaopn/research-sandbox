"""Service registry — kinds the webui knows how to render.

Two kinds today: `ssh` (WS-wrapped, browser xterm.js terminal) and `http`
(reverse-proxied with a cookie-issued session). Adding a same-kind service
is a registry entry plus a per-supervisor entrypoint conditional. New
kinds need a server-side dispatcher in server.py and a renderer branch
in static/app.js. Per-project enablement layers on top of this registry
via supervisor docker labels (`research.service.<id>=...`), read by
project_services_handler.
"""

import re

# Each entry's `command` is what the SSH-kind service runs after auth — the
# byobu invocation lands the user in /workspace, the supervisor's project root.
# `default_port` is the in-container port the webui reaches via container DNS
# (rs-project-<proj>:<port>); for code-server this is the lazy-start stub's
# listen port, NOT the underlying code-server's. The stub takes the request
# and either spawns / reuses the code-server child behind 127.0.0.1.
# Tab order in the SPA = SERVICES dict insertion order. Editor (code-
# server) is the primary work surface (notebooks, file editing, code-
# reading); it leads. The project's terminal (byobu in its top-level
# container) follows. Boxes are synthesized per-project (pi-iso-<name>),
# not static entries here.
SERVICES = {
    "code-server": {
        "label": "Editor",
        "kind": "http",
        "always_on": False,
        "renderer": "iframe",
        "default_port": 8443,
        "upstream_path": "/",
    },
    # The mobile reader — a read-only markdown/notebook/text viewer over the
    # project's workspace, default OFF (opt-in via `--enable reader` / the create
    # tickbox). http like the editor: probe-gated on READER_PORT (rscore) and served
    # on its own origin port. Every dind workflow gets a useful view: the reader
    # shows the workspace minus a deny-list, so a dev project reads as its clone and
    # worktrees rather than as the empty listing a research-shaped allowlist gave it.
    # Still rejected on the docker substrate at create (no dist mount there).
    "reader": {
        "label": "Reader",
        "kind": "http",
        "always_on": False,
        "renderer": "iframe",
        "default_port": 8445,
        "upstream_path": "/",
    },
    # The project's terminal: byobu in its top-level container, attached via
    # SSH. Its static label names the research supervisor; every other project
    # type is relabelled SHELL_TAB_LABEL by project_services_handler. The id
    # stays `supervisor` everywhere. The new-session lands in bash (NON-login,
    # as before) after cat-ing the workflow greeting if one was staged
    # (STAGE_SPAWN_GREETING): the research workflow ships no greeting yet, so
    # this is a clean no-op today — the mechanism is in place for a later
    # manifest-only add.
    # The service id was once `xterm` (named after the renderer)
    # and is now `supervisor` (named after what it actually connects
    # to). Existing projects' `research.service.xterm` labels are
    # orphaned by the rename — harmless (label-reading iterates
    # KNOWN_SERVICES, so missing labels default to enabled) and cleared
    # naturally on the next `research project update`.
    "supervisor": {
        "label": "Supervisor (CLI)",
        "kind": "ssh",
        "always_on": True,
        "renderer": "xterm.js",
        "default_port": 22,
        "command": (
            "byobu attach -t main 2>/dev/null || "
            "byobu new-session -s main -c /workspace -- "
            "bash -c 'cat /workspace/.orchestrator/greeting 2>/dev/null; exec bash'"
        ),
    },
    # (The agent-less `management` tab was retired in STAGE_SANDBOX_DIND_AGENT —
    # sandbox-dind shows this same terminal tab, labelled SHELL_TAB_LABEL.)
    # Boxes (kind="sandbox") are NOT static entries — they're per-project and
    # arbitrarily named, synthesized on demand as `pi-iso-<name>` tabs by
    # project_services_handler (see pi_isolated_service below).
}

# The terminal tab's label for every project whose top-level container is not
# the research supervisor (image rs-supervisor): the dind workflows on
# rs-sandbox-dind (dev, sandbox-dind, store dind workflows) and every
# docker-substrate project. Their terminal is a shell: an agent may run there,
# but no research supervisor.
SHELL_TAB_LABEL = "Shell (CLI)"


# Boxes (kind="sandbox") are per-project and arbitrarily named, so they can't be
# static SERVICES entries. Their tab id is `pi-iso-<name>` and the tab is
# synthesized on demand: project_services_handler adds one per box entry in the
# project's extensions.json, and `resolve()` reconstructs the command
# server-side for the ssh handler.
import re as _re

PI_ISOLATED_ID_PREFIX = "pi-iso-"
# Same grammar as the box NAME_RE. Validated before the name is interpolated
# into the docker-exec command string — a non-matching id is rejected (404)
# rather than executed, closing shell-injection via the URL.
_PI_ISOLATED_NAME_RE = _re.compile(r"^[a-z][a-z0-9-]*$")


def pi_isolated_service(name: str) -> dict | None:
    """Synthesize the tab/service spec for box ``name``, or None if the name
    fails validation.

    The inner command is a **login shell**, NOT ``claude`` — a box boots
    un-authed and starting claude, authenticating, etc. are all the PI's to do,
    in whatever order; auto-launching claude would pre-empt that. ``-c /workspace``
    lands the shell where the box's work lives.

    The shell is reached via the shared greet helper (STAGE_SPAWN_GREETING),
    which cats a greeting then `exec bash -l` (full PATH, claude included). The
    greeting path is the box convention ``/workspace/.rs-greeting`` (rs-sandbox
    writes the box's CLAUDE.md / greeting at create). Using the helper also keeps
    the path out of single-quotes, so there's no nesting in this tab's
    ``docker exec … bash -c '…'`` wrapper."""
    if not _PI_ISOLATED_NAME_RE.match(name):
        return None
    return {
        "label": f"{name} (CLI)",
        "kind": "ssh",
        "always_on": False,
        "renderer": "xterm.js",
        "default_port": 22,
        "command": (
            f"docker exec -it rs-pi-iso-{name} bash -c "
            "'byobu attach -t pi 2>/dev/null || "
            "byobu new-session -s pi -c /workspace -- "
            "/opt/pi-templates/greet-and-shell.sh /workspace/.rs-greeting'"
        ),
    }


# A box that opted into the editor publishes its code-server stub onto the
# supervisor netns (inner `docker run -p <editor_port>:8443`), so the webui reaches
# it at rs-project-<proj>:<editor_port> like any http service. The editor tab's id
# uses a DISTINCT prefix (NOT `pi-iso-`): the box-remove ✕ in the SPA is gated on
# `id.startsWith("pi-iso-")`, which must stay on the terminal tab only, and a
# `pi-iso-editor-<name>` id would also collide with the terminal-tab id of a box
# literally named `editor-<name>`. `box-editor-` sidesteps both.
PI_ISOLATED_EDITOR_ID_PREFIX = "box-editor-"


def pi_isolated_editor_service(name: str, port: int) -> dict | None:
    """Synthesize the http editor tab for box ``name`` (its code-server published
    at supervisor-netns ``port``), or None if the name fails validation. Labelled
    with the bare box name — the terminal tab carries the ``(CLI)`` suffix."""
    if not _PI_ISOLATED_NAME_RE.match(name):
        return None
    return {
        "label": name,
        "kind": "http",
        "always_on": False,
        "renderer": "iframe",
        "default_port": int(port),
        "upstream_path": "/",
        # The SPA's tabTypeOf honors an explicit spec tab_type before its
        # derived default, so a box's editor tab gets the same editor-type
        # border color as the project's own Editor tab instead of "other".
        "tab_type": "editor",
    }


# Exported ports (STAGE_EXPORTED_PORTS): a port the PI is serving inside the
# supervisor, surfaced as an http tab. The id is `port-<n>`; project_services_handler
# synthesizes one per LISTENING registered port, and _resolve_origin_upstream_port
# (origin_proxy_handler) resolves the upstream port from `<n>` after confirming
# it's in the project's registry (the server-side membership gate — the webui is
# on every project's bridge, so a client-chosen port would otherwise be a
# general port-forwarder).
EXPORTED_PORT_ID_PREFIX = "port-"


def exported_port_service(port: int, label: str,
                          follow_address: bool = False) -> dict | None:
    """Synthesize the http tab spec for an exported port, or None if the port is
    outside the valid TCP range. Labelled with the operator-supplied label.
    `follow_address` marks a box's own web-app tab: before loading it, the SPA
    tells the box the address the tab is opened at (the app answers only
    there) and waits until the app answers."""
    port = int(port)
    if not (1 <= port <= 65535):
        return None
    spec = {
        "label": label,
        "kind": "http",
        "always_on": False,
        "renderer": "iframe",
        "default_port": port,
        "upstream_path": "/",
        "surface": "visual",
    }
    if follow_address:
        spec["follow_address"] = True
    return spec


# Detected web apps (STAGE_AUTO_PORTS): ports the broker reports listening in a
# project's own container get a `port-<n>` tab marked `detected`, the SAME id a
# hand-added port has — so adding the port by hand in Ports keeps the tab's browser address.
# Whether a listener is a web app is decided by one `HEAD / HTTP/1.0`; the pure
# halves of that decision live here so they are testable outside the image.
CHECK_PASS = "pass"         # the reply starts with HTTP/
CHECK_BYTES = "bytes"       # something answered, but not HTTP (redis, a greeting)
CHECK_SILENT = "silent"     # connected, then closed or timed out with nothing said
CHECK_REFUSED = "refused"   # nothing accepted the connection


def classify_http_reply(connected: bool, data: bytes | None) -> str:
    """The outcome of one web check: `data` is what was read before the reply
    timeout or the close (None/b"" = nothing)."""
    if not connected:
        return CHECK_REFUSED
    if not data:
        return CHECK_SILENT
    return CHECK_PASS if data.startswith(b"HTTP/") else CHECK_BYTES


def detected_needs_check(row: dict) -> bool:
    """A docker-published port is checked only once the app inside its container
    listens (a non-empty inner_key) — while it boots, a check could only fail.
    Where the inner view is unavailable, or the port is held directly by an
    app, it is checked as soon as it appears."""
    if row.get("published") and row.get("inner_readable") is True:
        return bool(row.get("inner_key"))
    return True


def detected_check_key(row: dict) -> tuple:
    """What a cached outcome is valid for: the port's own sockets plus the app's
    listener inside its container. Either changing (a restart, a dev server
    started later inside an already-published container) means a new check."""
    return (tuple(sorted(row.get("inodes") or [])), str(row.get("inner_key") or ""))


def detected_outcome_cacheable(outcome: str, row: dict,
                               publish_unknown: bool = False) -> bool:
    """Which outcomes stand until the key changes. A refusal never does (nothing
    is listening at this moment). A silent reply does, except where silence may
    only mean "still starting": a published port whose inner view is
    unavailable (its app may simply not be up yet), or any port when the inner docker
    lookup failed and whether it is published is unknown. Those are re-checked
    every refresh."""
    if outcome == CHECK_REFUSED:
        return False
    if outcome == CHECK_SILENT and (publish_unknown or (
            row.get("published") and row.get("inner_readable") is False)):
        return False
    return True


def span_membership(spans: list[tuple[int, int]]):
    """`member(port)` → True when `port` lies in any (first port, count) span, in
    O(log n): the registry the spans come from is writable from inside the
    project container, so neither the number of spans nor a count is trusted
    to be small, and nothing is expanded."""
    import bisect
    rs = sorted((lo, lo + max(int(n), 1) - 1) for lo, n in spans)
    los, reach, best = [lo for lo, _ in rs], [], None
    for lo, hi in rs:
        best = hi if best is None or hi > best else best
        reach.append(best)

    def member(port: int) -> bool:
        i = bisect.bisect_right(los, port) - 1
        return i >= 0 and reach[i] >= port
    return member


def detected_check_order(key: tuple, tried: tuple | None) -> tuple:
    """Sort key for the check batch: a port never tried with its current key
    first, then re-checks of an already-tried key, the longest-waiting first.
    `tried` is (key, monotonic time) of the last check started, or None."""
    if tried is None or tried[0] != key:
        return (0, 0.0)
    return (1, tried[1])


def opened_detected_ports(origin_entries: list[dict], project: str,
                          registered_page_ports: set[int]) -> list[int]:
    """`port-<n>` addresses `project` holds that no hand-added page port uses:
    opened detected apps, plus slots left behind by a removed box or port —
    every one the operator can free from the Ports section."""
    out: set[int] = set()
    for e in origin_entries:
        sid = e.get("service", "")
        if e.get("project") != project or not sid.startswith(EXPORTED_PORT_ID_PREFIX):
            continue
        raw = sid[len(EXPORTED_PORT_ID_PREFIX):]
        if raw.isdigit() and int(raw) not in registered_page_ports:
            out.add(int(raw))
    return sorted(out)


# Pass-through claims (STAGE_PASSTHROUGH_PORTS): a raw-TCP host-port claim
# surfaced as a LAUNCHER tab — the pane opens https://<host>:<host_port> in a
# NEW browser tab (encrypted and/or multi-port apps the page-relay tab
# structurally can't front). Distinct prefix from `port-`: a claim never
# allocates an origin slot, and its id keys on the HOST port. The SPA branches
# on `passthrough_port` in openHttpService BEFORE the session mint.
PASSTHROUGH_ID_PREFIX = "pass-"


def passthrough_service(host_port: int, label: str, count: int = 1) -> dict | None:
    """Synthesize the launcher-tab spec for a pass-through claim, or None when
    the host port is outside the valid TCP range."""
    host_port = int(host_port)
    if not (1 <= host_port <= 65535):
        return None
    return {
        "label": label,
        "kind": "http",     # load-bearing: surfaceOf/activateService dispatch
        "always_on": False,
        "renderer": "launcher",
        "default_port": host_port,
        "surface": "visual",
        "passthrough_count": int(count),
    }


# A dev consumer's gitea FORK (the dev-workflow project's own agent, or a dev
# box) — one tab per consumer+repo, linking to that fork's home page. Distinct
# from every other prefix (`pi-iso-`, `box-editor-`, `port-`).
DEV_FORK_ID_PREFIX = "git-"

# The organisation that owns every Gitea-history notes repo — MIRROR of
# cli/gitea.py::HISTORY_ORG (the webui image carries no cli/; pytest-pinned).
HISTORY_ORG = "notes"

# gitea usernames and repo names are alnum + `.`/`-`/`_`. The value is baked into
# BOTH the iframe path on the shared gitea origin AND the tab id, so anything
# outside that charset (a `/` retargeting the frame elsewhere in gitea, a `:`
# colliding in the id namespace) is refused rather than sanitized.
_DEV_FORK_NAME = re.compile(r"^[A-Za-z0-9._-]+$")


def dev_fork_service(label: str, user: str, repo: str) -> dict | None:
    """Synthesize the tab spec for one dev consumer's gitea fork, or None if the
    fork's owner/repo is not a plain gitea name.

    Carries `gitea_path`, NEVER `origin_url`: this tab serves off the SHARED
    `-gitea-` sentinel origin (one gitea, one login), not a per-container origin
    port — so the synthesizer must not call `_origin_url()` for it, and its id
    never enters ORIGIN_PORTS. `kind` is load-bearing: the SPA dispatches on it
    (an absent kind falls back to "cli" and renders the tab as a terminal)."""
    if not (_DEV_FORK_NAME.match(user or "") and _DEV_FORK_NAME.match(repo or "")):
        return None
    return {
        "label": label,
        "kind": "http",
        "always_on": False,
        "renderer": "iframe",
        "gitea_path": f"/{user}/{repo}",
    }


# A dev repo's Fetch window as a project-strip tab — one tab per DISTINCT repo
# among the project's dev consumers (content is repo-scoped + active-fork
# steered, so agent+box on the same repo share one tab). Distinct from every
# other prefix (`git-`, `pi-iso-`, `box-editor-`, `port-`).
DEV_FETCH_ID_PREFIX = "fetch-"


def dev_fetch_service(label: str, repo: str) -> dict | None:
    """Synthesize the per-project Fetch-tab spec for one dev repo, or None if
    the repo is not a plain gitea name.

    Carries `fetch_repo`, NEVER `origin_url`/`gitea_path`: the SPA renders
    this pane itself from the /broker/dev/repo-status relay — no iframe, no
    upstream, so its id never enters ORIGIN_PORTS and no session cookie is
    minted for it. `kind` stays load-bearing for the tab dispatch."""
    if not _DEV_FORK_NAME.match(repo or ""):
        return None
    return {
        "label": label,
        "kind": "http",
        "always_on": False,
        "renderer": "panel",
        "fetch_repo": repo,
    }


# The PROJECT-WIDE Fetch tab — ONE per rs-fetch-enabled project, listing EVERY
# mirrored repo. Deliberately not repo-keyed like the tab above: an rs-fetch
# consumer can pull ANY mirror (the staged ~/.dev-tokens/fetch-wiring.json maps
# every mirror -> its active fork), and the mirror list is host-side, off the
# webui's `/projects:ro` mount — so the repo set is resolved client-side from
# the /broker/dev relay when the tab is opened, never here.
#
# Shares the `fetch-` prefix ON PURPOSE: the mobile tab whitelist already
# exempts `[data-service^="fetch-"]`, so this tab reaches the phone with no CSS
# edit (and without touching that selector's exact-match neighbours). The `:` is
# what keeps the id collision-proof against a real per-repo tab — `:` is illegal
# in gitea repo names, so `_DEV_FORK_NAME` refuses it and dev_fetch_service can
# never mint this id. That matters because ONE project can carry both families
# at once (a dev box + a fetch box). Nothing parses the id back: it is a dict key
# and a quoted `data-service` attribute.
PROJECT_FETCH_ID = DEV_FETCH_ID_PREFIX + ":all"


def project_fetch_service(label: str) -> dict:
    """Synthesize the project-wide Fetch-tab spec.

    Carries `fetch_all`, NEVER `fetch_repo`/`origin_url`/`gitea_path`: the SPA
    renders every mirrored repo's card itself from the /broker/dev relay — no
    iframe, no upstream, so the id never enters ORIGIN_PORTS and no session
    cookie is minted for it. `kind` stays load-bearing for the tab dispatch.

    Returns a plain dict, not `dict | None` like its siblings: there is no
    user-supplied name to charset-guard here, so an Optional return would be
    dead shape at every call site."""
    return {
        "label": label,
        "kind": "http",
        "always_on": False,
        "renderer": "panel",
        "fetch_all": True,
    }


def resolve(service_id: str) -> dict | None:
    """Static registry lookup, falling back to a synthesized PI-isolated
    spec for `pi-iso-<name>` ids. Used by the ssh handler so it can run the
    command for a per-project agent that isn't in the static registry."""
    svc = SERVICES.get(service_id)
    if svc is not None:
        return svc
    if service_id.startswith(PI_ISOLATED_ID_PREFIX):
        return pi_isolated_service(service_id[len(PI_ISOLATED_ID_PREFIX):])
    return None


def get(service_id: str) -> dict | None:
    return SERVICES.get(service_id)
