"""rscore — the host-side project-lifecycle core.

Stdlib-only. One implementation of the lifecycle verbs (create / destroy /
start / stop / update / list / status), shared by every front-end so they can
never drift apart. Today the only front-end is the terminal CLI (research.py);
a browser-driven one will call the same verbs later.

The shape each verb follows:

    request kwargs ─▶ <Req>.from_kwargs(**kw)   # the one validation point
                          │  (name rule, string→enum, csv/list→tuple)
                          ▼
                     verb(req) ─▶ <Result> dataclass
                          │
              ┌───────────┴────────────┐
        terminal: format text     browser: serialise JSON

Dependency direction is one-way: callers import rscore; rscore imports nothing
of theirs. The lifecycle substrate (Config + constants + the docker/network
helpers below the verbs) lives in this module so the verbs are self-contained;
research.py imports it all back via `from rscore import *` so its non-lifecycle
cmd_* keep resolving the same helpers.

Two failure channels, deliberately distinct:

  • ValidationError — bad input (name, enum value). Raised by from_kwargs
    BEFORE any side effect, so a front-end can reject cleanly with no partial
    state. The terminal maps it to its error-print + exit; the browser to a
    400-style reply.
  • SystemExit — a failure mid-execution (missing image, a component that
    won't start). Raised by the shared die() deep in the helper cone and left
    as SystemExit so it propagates and aborts the verb (fail-explicit). The
    terminal lets it exit the process; the browser wraps it into an error
    reply. Component failures are NOT swallowed — see create()'s notes.
"""

from __future__ import annotations

import argparse
import base64
import datetime
import enum
import ipaddress
import json
import os
import re
import secrets
import shlex
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

# rscore lives in cli/; make sibling helper modules importable even when
# imported directly (e.g. by the broker), not only via research.py.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import box_catalog  # noqa: E402  (box-preset catalog; staged into the supervisor)
import defaults  # noqa: E402
import mcp_registry  # noqa: E402
import pi_isolated_registry  # noqa: E402
import role_mcp  # noqa: E402
import extension  # noqa: E402
import workflow  # noqa: E402  (manifest store catalog; from_kwargs resolves --workflow)


# ---------------------------------------------------------------------------
# Enums — closed vocabularies (one per CLI choices=[...] today). str-mixin so a
# member compares/round-trips to its string value with no adapter code.
# ---------------------------------------------------------------------------


class ProjectType(str, enum.Enum):
    RESEARCH = "research"
    SANDBOX_DIND = "sandbox-dind"


class Substrate(str, enum.Enum):
    """Containment runtime — an INTERNAL axis, never a user-facing flag
    (WORKFLOW_TAXONOMY Q7). ``dind-sysbox`` is today's sysbox DIND host (both
    research and sandbox-dind flavors run on it); ``docker`` is a single runc
    container with no inner daemon. Resolved from the user-selected workflow's
    manifest in ``CreateRequest.from_kwargs`` (via ``_resolve_workflow``) and set
    on the request, so create() consumes it without re-deriving."""
    DOCKER = "docker"
    DIND_SYSBOX = "dind-sysbox"


class Egress(str, enum.Enum):
    OPEN = "open"
    LOCKED = "locked"


class DindMode(str, enum.Enum):
    AUTO = "auto"
    SYSBOX = "sysbox"
    PRIVILEGED = "privileged"


# ---------------------------------------------------------------------------
# Input-validation failure channel.
# ---------------------------------------------------------------------------


class ValidationError(ValueError):
    """Bad request input. Always raised before any side effect."""


class HarnessError(Exception):
    """A light-path harness step (clone/setup) failed AFTER side effects began
    (WORKFLOW_TAXONOMY_S4). Distinct from ValidationError (pre-side-effect) and
    from die()/SystemExit (whose message is teed to the host-only full log by
    the broker): it carries TWO fields so dispatch can SPLIT the sinks —
    ``log_msg`` (step name only) → the durable full log + view log, and
    ``client_detail`` (token-scrubbed git/setup stderr) → the client error
    envelope ONLY. The command argv (which carries the PAT) is never stringified
    into either. The partially-built box is left standing for inspection."""

    def __init__(self, log_msg: str, client_detail: str = ""):
        super().__init__(log_msg)
        self.log_msg = log_msg
        self.client_detail = client_detail


# ---------------------------------------------------------------------------
# Validators + coercion (the choke point's building blocks).
# ---------------------------------------------------------------------------

# Project name: ASCII letters/digits, plus '-'/'_' after the first character,
# and must start with a letter or digit. Stricter than a bare alnum check on
# purpose — unicode names and leading '-' are downstream Docker footguns (and a
# leading '-' can be read as a flag), with no use case here.
_PROJECT_NAME_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_-]*\Z")

# Sandbox-box name grammar + agent enum — LOCKSTEP COPIES of rs_sandbox._NAME_RE
# and the `rs-sandbox create --agent` choices (cli/rs_sandbox.py). rscore cannot
# import rs_sandbox (it's baked into the supervisor image under conda python), so
# the box_* verbs re-declare the validation here; keep both in agreement.
_BOX_NAME_RE = re.compile(r"\A[a-z][a-z0-9-]*\Z")
_BOX_AGENTS = frozenset({"claude", "none"})


def valid_project_name(name: str) -> bool:
    return isinstance(name, str) and bool(_PROJECT_NAME_RE.match(name))


def _require_name(name: Any) -> str:
    if not valid_project_name(name):
        raise ValidationError(
            "project name must be ASCII letters/digits, may contain '-' or "
            "'_', and must start with a letter or digit")
    return name


def _as_enum(cls: type, value: Any, *, field_name: str, default: Any) -> Any:
    """Coerce a string/enum into ``cls``. ``None`` ⇒ ``default`` (which may be
    ``None`` to mean 'resolve from config at run time', as egress/dind do). An
    unknown value is a ValidationError listing the valid choices."""
    if value is None:
        return default
    if isinstance(value, cls):
        return value
    try:
        return cls(value)
    except ValueError:
        choices = ", ".join(m.value for m in cls)
        raise ValidationError(
            f"invalid {field_name} value: {value!r} (expected {choices})")


def _as_tuple(value: Any) -> tuple[str, ...]:
    """Normalise a list-like field from EITHER a comma-string (terminal) OR a
    list (browser) into a tuple of stripped, non-empty tokens. ``None`` ⇒
    ``()``. One choke point serves both front-ends unchanged."""
    if value is None:
        return ()
    if isinstance(value, str):
        parts = value.split(",")
    elif isinstance(value, Sequence):
        parts = list(value)
    else:
        raise ValidationError(
            f"expected a string or list, got {type(value).__name__}")
    return tuple(p.strip() for p in parts if isinstance(p, str) and p.strip())


# ---------------------------------------------------------------------------
# Request objects — the verb vocabulary. Frozen + built only via from_kwargs
# (the validation point); never instantiate raw from untrusted input.
# ---------------------------------------------------------------------------


# The user-facing create selector is the WORKFLOW. The substrate is internal
# (never a flag), and the legacy project_type flavor is DERIVED from the
# resolved manifest — both are set by from_kwargs so create()'s body is unchanged
# (WORKFLOW_TAXONOMY_S3). A bare `create <name>` defaults to the research lab.
DEFAULT_WORKFLOW = "research"


def _resolve_workflow(workflow_id: str) -> tuple[dict, "ProjectType", "Substrate"]:
    """Resolve a workflow id against the store catalog into (manifest,
    project_type, substrate). Raises ValidationError (the pre-side-effect
    channel) on an unknown id or a broken catalog. The manifest is returned so
    from_kwargs can read the light-path payload (repo/ref/setup) presets from the
    same single load (WORKFLOW_TAXONOMY_S4) — no second catalog read.

    The legacy project_type flavor is derived from manifest DATA — NOT a manifest
    field (which would entrench the very key the deferred refactor removes) and
    NOT a workflow-name lookup (couples to names): a docker box is the research
    flavor (matches slice 1's --substrate docker), the rs-sandbox-dind overlay is
    the agent-less sandbox-dind flavor, everything else is research. Built-in-only for
    now; BYO-overlay flavor derivation is a later slice."""
    try:
        catalog = {m["name"]: m for m in workflow.load_catalog()}
    except workflow.WorkflowError as e:
        raise ValidationError(f"workflow catalog error: {e}")
    m = catalog.get(workflow_id)
    if m is None:
        raise ValidationError(
            f"unknown workflow {workflow_id!r}; see `research workflow list`")
    substrate = Substrate(m["substrate"])  # manifest substrate is schema-validated
    mgmt_overlay = SANDBOX_DIND_IMAGE.split(":", 1)[0]  # "rs-sandbox-dind"
    if m["substrate"] == Substrate.DOCKER.value:
        project_type = ProjectType.RESEARCH
    elif m.get("image_overlay") == mgmt_overlay:
        project_type = ProjectType.SANDBOX_DIND
    else:
        project_type = ProjectType.RESEARCH
    return m, project_type, substrate


def workflow_has_worker_layer(manifest: dict) -> bool:
    """True iff a project created from this workflow runs the worker/extension
    enable cone — i.e. its derived flavor is the research lab. create() skips the
    cone on the docker substrate (no inner daemon) and on the sandbox-dind flavor
    (sandbox-dind: an agent-less management host with no worker layer), so only a
    dind-sysbox workflow that ISN'T the management overlay qualifies. Derived
    from manifest DATA, mirroring _resolve_workflow — never a name lookup — so a
    BYO dind workflow classifies correctly. The webui uses this (surfaced via the
    broker `workflows` verb) to show the --enable presets only where they take
    effect, instead of offering a silent no-op on a box with no worker layer."""
    if manifest.get("substrate") != Substrate.DIND_SYSBOX.value:
        return False
    mgmt_overlay = SANDBOX_DIND_IMAGE.split(":", 1)[0]  # "rs-sandbox-dind"
    return manifest.get("image_overlay") != mgmt_overlay


def workflow_is_sandbox_dind(manifest: dict) -> bool:
    """True iff a project created from this workflow is the sandbox-dind flavor
    (dind-sysbox substrate + the rs-sandbox-dind overlay). Derived from manifest
    DATA, mirroring _resolve_workflow — never a name lookup. The webui uses this
    (surfaced via the broker `workflows` verb as `box_capable`) to show the agents +
    light-path group for sandbox-dind, the same way it keys off `has_worker_layer`
    for the research --enable presets."""
    if manifest.get("substrate") != Substrate.DIND_SYSBOX.value:
        return False
    mgmt_overlay = SANDBOX_DIND_IMAGE.split(":", 1)[0]  # "rs-sandbox-dind"
    return manifest.get("image_overlay") == mgmt_overlay


def _light_clone_basename(repo: str) -> str:
    """Derive the clone-dir basename from a repo URL: the last path segment with
    a trailing '/' and a trailing '.git' stripped (``…/u/foo.git`` → ``foo``).
    Reject an empty / '.' / '..' / separator-bearing result so a crafted URL
    cannot target a clone outside /workspace (WORKFLOW_TAXONOMY_S4, in-box but
    cheap). Raises ValidationError (pre-side-effect)."""
    base = repo.rstrip("/").rsplit("/", 1)[-1]
    if base.endswith(".git"):
        base = base[: -len(".git")]
    if base in ("", ".", "..") or "/" in base or "\\" in base:
        raise ValidationError(
            f"could not derive a clone directory from repo {repo!r}")
    return base


def _light_exec(container: str, script: str, *, step: str, github_pat: str,
                workdir: str | None = None) -> None:
    """Run one harness `script` via ``docker exec -u research [-w workdir] bash
    -lc`` (the box's unprivileged user). `capture_output=True` so the output
    NEVER enters the broker's teed stderr → the durable full log. On failure
    raise HarnessError (NOT die()): step-name only as log_msg, the captured
    stderr (literal PAT scrubbed) as client_detail — the split-sink contract
    (WORKFLOW_TAXONOMY_S4 rule 4). The argv (which carries the token in the clone
    URL) is never stringified into the error."""
    cmd = ["docker", "exec", "-u", "research"]
    if workdir:
        cmd += ["-w", workdir]
    cmd += [container, "bash", "-lc", script]
    r = run(cmd, capture_output=True)
    if r.returncode != 0:
        detail = (r.stderr or r.stdout or "").strip()
        if github_pat:
            detail = detail.replace(github_pat, "***")
        raise HarnessError(f"{step} failed (exit {r.returncode})", detail)


def _run_light_harness(container: str, repo: str, ref: str, setup: str,
                       github_pat: str, progress) -> str:
    """Light-path harness (WORKFLOW_TAXONOMY_S4): clone ``repo``@``ref`` into
    /workspace/<basename> and run ``setup`` inside the docker box, as the
    unprivileged `research` user. No-op when neither repo nor setup is set;
    returns the clone dir ("" if no repo). Create-time only — the artifacts ride
    the bind-mount volume, so start/update never re-run it.

    Must run AFTER inject_route (egress). Fail-explicit via HarnessError so the
    partial box is left standing and the broker can split the diagnostic from the
    durable log + the token (see _light_exec)."""
    if not repo and not setup:
        return ""
    workdir = "/workspace"
    if repo:
        base = _light_clone_basename(repo)        # already validated in from_kwargs
        workdir = f"/workspace/{base}"
        clone_url = repo
        if github_pat:
            # Token-in-remote so it persists in .git/config for later `git pull`
            # (PI decision). https-validated; the token reaches git as an argv
            # element via bash -lc, never a host shell (run() is list-form).
            clone_url = "https://x-access-token:" + github_pat + "@" + repo[len("https://"):]
        progress.step("clone-repo", "cloning workflow repo")
        _light_exec(
            container,
            f"git clone {shlex.quote(clone_url)} {shlex.quote(workdir)} && "
            f"git -C {shlex.quote(workdir)} checkout {shlex.quote(ref)}",
            step="workflow clone", github_pat=github_pat)
    if setup:
        progress.step("run-setup", "running workflow setup")
        _light_exec(container, setup, step="workflow setup",
                    github_pat=github_pat, workdir=workdir)
    return workdir if repo else ""


@dataclass(frozen=True)
class CreateRequest:
    name: str
    workflow: str = DEFAULT_WORKFLOW      # the USER-facing selector
    type: ProjectType = ProjectType.RESEARCH   # DERIVED from workflow (internal flavor)
    substrate: Substrate | None = None    # DERIVED from workflow (internal); never a flag
    egress: Egress | None = None          # None ⇒ config/flavor default at run time
    dind: DindMode | None = None          # None ⇒ cfg.default_dind at run time
    profile: str = "python"               # accepted for CLI parity; currently unused
    data: tuple[str, ...] = ()
    memory: str = ""                      # "" ⇒ cfg.default_memory
    cpus: str = ""
    ssh_port: int | None = None
    inner_firewall: bool = False
    enable: tuple[str, ...] = ()
    disable: tuple[str, ...] = ()
    role_mcp_upstream: tuple[str, ...] = ()
    mcp: str = "all-enabled"
    # Light-path harness payload (WORKFLOW_TAXONOMY_S4): clone repo@ref into the
    # docker box + run setup. Manifest-preset, explicit-overridable; in-box, not
    # host-shaped (see the boundary note). github_pat is a SECRET — repr=False so
    # an accidental repr(req)/log masks it; it is also never a CreateResult field.
    repo: str = ""
    ref: str = ""
    setup: str = ""
    github_pat: str = field(default="", repr=False)
    # Which agent dists a docker box deploys at boot (STAGE_MULTI_AGENT; was the
    # single STAGE_AGENT_DIST_S1 `agent`). An independent on/off set — one writable
    # ~/.local launcher per enabled agent. In-box field (selects software run
    # *inside* the box, not host-shaped); () = clean box, no agent.
    agents: tuple[str, ...] = ()
    # Per-flavor service defaults from the workflow manifest's `services` field
    # (STAGE_EDITOR_DIST slice 2): e.g. the `sandbox` workflow declares
    # {"code-server": false} so a bare box is lean. DERIVED in from_kwargs from the
    # resolved manifest (like type/substrate) — never a user/broker input, so it
    # stays out of CREATE_WEBUI_FIELDS. Mutable default ⇒ default_factory.
    service_defaults: dict[str, bool] = field(default_factory=dict)
    # The in-container starting message (STAGE_SPAWN_GREETING), printed once on
    # first open of the workflow's main shell — distinct from the webui card
    # `description` (which never enters the container). DERIVED in from_kwargs
    # from the resolved manifest's `greeting` (like service_defaults) — never a
    # user/broker input, so it stays out of CREATE_WEBUI_FIELDS. Staged to
    # /workspace/.orchestrator/greeting at create for the management/research
    # flavor; "" ⇒ no file staged.
    greeting: str = ""

    @classmethod
    def from_kwargs(cls, **kw: Any) -> "CreateRequest":
        ssh_port = kw.get("ssh_port")
        if ssh_port is not None and not isinstance(ssh_port, int):
            raise ValidationError("ssh_port must be an integer")
        # Resolve the workflow → (manifest, type, substrate) here, the single
        # validation choke point, so create() consumes the same fields as before.
        workflow_id = kw.get("workflow") or DEFAULT_WORKFLOW
        manifest, project_type, substrate = _resolve_workflow(workflow_id)
        # Per-flavor service defaults from the manifest (STAGE_EDITOR_DIST slice 2;
        # validated at catalog-load time): research/sandbox-dind omit `services` ⇒ {}
        # ⇒ editor on; `sandbox` declares {"code-server": false} ⇒ lean box.
        service_defaults = dict(manifest.get("services") or {})
        # Starting message (STAGE_SPAWN_GREETING): manifest-derived, coerced to a
        # str so an explicit-null manifest never carries None into the typed field.
        greeting = manifest.get("greeting") or ""
        # Effective light-path payload: manifest PRESET ⊕ explicit input, explicit
        # wins per field (the store-template-auto-fills-the-create-fields model).
        # The PAT is explicit-only — never in an operator-curated manifest.
        repo = (kw.get("repo") or manifest.get("repo") or "").strip()
        ref = (kw.get("ref") or manifest.get("ref") or "").strip()
        setup = (kw.get("setup") or manifest.get("setup") or "").strip()
        github_pat = kw.get("github_pat") or ""
        if repo:
            if not ref:
                raise ValidationError(
                    "a workflow repo requires a ref (pin the clone — no drift)")
            if not repo.startswith("https://"):
                raise ValidationError(
                    f"workflow repo must be an https:// URL (got {repo!r}); the "
                    "docker box is locked-egress (no ssh/port 22) and a private "
                    "repo clones over https via a PAT")
            _light_clone_basename(repo)   # reject a path-escaping basename early
        # Agent dists (STAGE_MULTI_AGENT): an enable-SET. Each must be a known agent,
        # and on the docker substrate each must already be pulled. Non-docker is
        # noted-and-ignored in create() (only the docker-box cp-deploy path is wired
        # for the explicit set; dind uses the DEFAULT_AGENT floor below). Dedup while
        # preserving order so a box never double-mounts the same agent.
        agents = tuple(dict.fromkeys(_as_tuple(kw.get("agents"))))
        for a in agents:
            if a not in KNOWN_AGENTS:
                raise ValidationError(
                    f"unknown agent {a!r} (known: {', '.join(KNOWN_AGENTS)})")
            if substrate is Substrate.DOCKER and not dist_present(a):
                raise ValidationError(
                    f"agent {a!r}: no cached dist — run "
                    f"`research agent pull --agent {a}` first")
        # Fleet floor (STAGE_AGENT_DIST slice 2): EVERY dind project deploys claude
        # from the dist (no bake) — research flavor for the supervisor + worker/
        # role-MCP/PI fleet, sandbox-dind flavor for its rs-sandbox-box boxes (FROM
        # rs-analysis-base). So a dind create needs a pulled dist. (docker boxes use
        # the explicit --agent path above.) `research start` auto-pulls if absent,
        # so this floor rarely trips.
        if substrate is Substrate.DIND_SYSBOX and not dist_present(DEFAULT_AGENT):
            raise ValidationError(
                f"no cached {DEFAULT_AGENT} dist — run `research agent pull` first "
                f"(or `research start`, which auto-pulls)")
        # Editor floor (STAGE_EDITOR_DIST slice 2 — PI chose fail-fast): a dind
        # project whose editor will be ENABLED needs a pulled editor dist (no bake
        # anymore), so you never get a supervisor with a missing Editor tab.
        # Flag-aware (NOT raw substrate): a `--disable code-server` project
        # deliberately has no tab, so it must not be blocked. Resolve the effective
        # flag through the SAME function + manifest base that create() uses at the
        # service_flags call, so the floor and that flag can't disagree by
        # construction. `research start` auto-pulls if absent, so this rarely trips.
        svc_en = _split_enable_tokens(",".join(_as_tuple(kw.get("enable"))))[0]
        svc_dis = _split_disable_tokens(",".join(_as_tuple(kw.get("disable"))))[0]
        editor_on = _compute_service_flags(
            svc_en, svc_dis, base=(service_defaults or None)).get("code-server", True)
        if substrate is Substrate.DIND_SYSBOX and editor_on and not editor_dist_present():
            raise ValidationError(
                "no cached editor dist — run `research editor pull` first "
                "(or `research start`, which auto-pulls)")
        return cls(
            name=_require_name(kw.get("name")),
            workflow=workflow_id,
            type=project_type,
            substrate=substrate,
            egress=_as_enum(Egress, kw.get("egress"),
                            field_name="--egress", default=None),
            dind=_as_enum(DindMode, kw.get("dind"),
                          field_name="--dind", default=None),
            profile=kw.get("profile") or "python",
            data=_as_tuple(kw.get("data")),
            memory=kw.get("memory") or "",
            cpus=kw.get("cpus") or "",
            ssh_port=ssh_port,
            inner_firewall=bool(kw.get("inner_firewall", False)),
            enable=_as_tuple(kw.get("enable")),
            disable=_as_tuple(kw.get("disable")),
            role_mcp_upstream=_as_tuple(kw.get("role_mcp_upstream")),
            mcp=kw.get("mcp") if kw.get("mcp") is not None else "all-enabled",
            repo=repo, ref=ref, setup=setup, github_pat=github_pat,
            agents=agents,
            service_defaults=service_defaults,
            greeting=greeting,
        )


@dataclass(frozen=True)
class DestroyRequest:
    name: str
    # Confirmation is a FRONT-END concern (terminal prompt; the browser's own
    # gate). The verb just destroys — no prompting in rscore.

    @classmethod
    def from_kwargs(cls, **kw: Any) -> "DestroyRequest":
        return cls(name=_require_name(kw.get("name")))


@dataclass(frozen=True)
class StatusRequest:
    name: str

    @classmethod
    def from_kwargs(cls, **kw: Any) -> "StatusRequest":
        return cls(name=_require_name(kw.get("name")))


@dataclass(frozen=True)
class StartStopRequest:
    """stop/start take a single project OR --all (exactly one)."""
    name: str | None = None
    all: bool = False

    @classmethod
    def from_kwargs(cls, **kw: Any) -> "StartStopRequest":
        name = kw.get("name")
        all_ = bool(kw.get("all", False))
        if all_ == (name is not None):
            raise ValidationError(
                "specify exactly one of: project name, or --all")
        return cls(name=_require_name(name) if name is not None else None,
                   all=all_)


@dataclass(frozen=True)
class UpdateRequest:
    name: str
    rebuild: bool = False
    keep_claude: bool = False
    enable: tuple[str, ...] = ()
    disable: tuple[str, ...] = ()
    role_mcp_upstream: tuple[str, ...] = ()

    @classmethod
    def from_kwargs(cls, **kw: Any) -> "UpdateRequest":
        return cls(
            name=_require_name(kw.get("name")),
            rebuild=bool(kw.get("rebuild", False)),
            keep_claude=bool(kw.get("keep_claude", False)),
            enable=_as_tuple(kw.get("enable")),
            disable=_as_tuple(kw.get("disable")),
            role_mcp_upstream=_as_tuple(kw.get("role_mcp_upstream")),
        )


@dataclass(frozen=True)
class AttachRequest:
    """The JIT-keyring verb: 'give me the SSH coordinates to reach this
    project'. Name-only, same regex choke point as the other verbs."""
    name: str

    @classmethod
    def from_kwargs(cls, **kw: Any) -> "AttachRequest":
        return cls(name=_require_name(kw.get("name")))


# ---------------------------------------------------------------------------
# Result objects — what each verb returns. The terminal formats these into its
# text; the browser serialises them to JSON. The SSH password rides back in the
# result (in memory), never written to disk.
# ---------------------------------------------------------------------------


@dataclass
class CreateResult:
    project: str
    container: str
    workspace: str
    network: str
    egress: str
    dind_mode: str
    inner_firewall: bool
    project_type: str
    substrate: str
    workflow: str
    ssh_port: int
    ssh_password: str                       # in-memory only
    data_mounts: dict[str, str] = field(default_factory=dict)   # basename → host src
    mcps: list[str] = field(default_factory=list)               # granted (best-effort)
    workers: list[str] = field(default_factory=list)            # enabled (all-or-abort)
    extensions: list[str] = field(default_factory=list)          # enabled (all-or-abort)
    # Light-path harness result (non-secret; for the report). NEVER github_pat —
    # CreateResult is asdict'd into the broker reply + lingers in webui OP_RUNS.
    repo: str = ""
    clone_dir: str = ""                     # /workspace/<basename> if a repo cloned
    agents: list[str] = field(default_factory=list)   # agent dists deployed into a docker box


@dataclass
class ProjectSummary:
    project: str
    state: str
    ssh: str | None                         # "localhost:<port>" or None


@dataclass
class ProjectStatus:
    project: str
    container: str
    state: str
    workspace: str
    ssh_port: str | None
    inner_workers: list[str] = field(default_factory=list)   # "name\tstatus\timage" lines
    registry_count: int = 0


@dataclass
class ActionResult:
    """One per container for stop/start, incl. skips."""
    name: str
    project: str | None
    action: str                             # "stop" | "start"
    outcome: str                            # "ok" | "skip:absent" | "skip:already"


@dataclass
class UpdateResult:
    project: str
    rebuilt: bool
    refreshed_claude: bool
    workers_enabled: list[str] = field(default_factory=list)
    extensions_enabled: list[str] = field(default_factory=list)
    workers_disabled: list[str] = field(default_factory=list)
    extensions_disabled: list[str] = field(default_factory=list)


@dataclass
class AttachInfo:
    """SSH coordinates for the webui to reach a RUNNING supervisor over its
    per-project bridge: container DNS + the internal sshd port — NEVER the
    published localhost host-port that list/status expose. The password rides
    back in-memory (browser-transient on the webui side); never logged."""
    name: str
    host: str                               # rs-project-<name> (container DNS)
    port: int                               # the container's internal sshd port
    username: str
    password: str                           # in-memory only


@dataclass(frozen=True)
class BoxAddRequest:
    """Add a sandbox box to a RUNNING dind project (webui-driven). All fields act
    INSIDE the locked-egress, credential-free inner box — none is host-shaped — so
    they are broker-relayable (cf. the host-root boundary): ``preset`` is the box
    TYPE (catalog-gated in box_add); ``agent`` overrides the preset's default
    (None ⇒ rs-sandbox applies it); ``editor`` bundles code-server into the box;
    ``mcps`` are project MCP names wired into the box (⊆ allow, gated in box_add;
    a non-empty set forces the agent on); ``repo``/``ref``/``setup`` seed a `byo`
    box and run INSIDE it (relayable in-box fields per WORKFLOW_TAXONOMY_S4)."""
    project: str
    name: str | None = None                 # None ⇒ rs-sandbox auto-names box-N
    preset: str = "empty"
    agent: str | None = None                # claude | none | None (preset default)
    editor: bool = False
    mcps: tuple[str, ...] = ()
    repo: str = ""
    ref: str = ""
    setup: str = ""

    @classmethod
    def from_kwargs(cls, **kw: Any) -> "BoxAddRequest":
        name = kw.get("name")
        if name is not None and not (isinstance(name, str) and _BOX_NAME_RE.match(name)):
            raise ValidationError(
                "box name must be lowercase, start with a letter, and contain "
                "only letters, digits, or '-'")
        preset = kw.get("preset") or "empty"
        # Preset names share the box-name charset; ∈-catalog is gated in box_add
        # (which has the staged catalog). Here only the shape.
        if not (isinstance(preset, str) and _BOX_NAME_RE.match(preset)):
            raise ValidationError(
                "box preset must be lowercase, start with a letter, and contain "
                "only letters, digits, or '-'")
        agent = kw.get("agent")
        if agent is not None and agent not in _BOX_AGENTS:
            raise ValidationError(
                f"invalid agent {agent!r} (expected one of {sorted(_BOX_AGENTS)})")
        raw_mcps = kw.get("mcps") or ()
        if isinstance(raw_mcps, str):
            raw_mcps = [t.strip() for t in raw_mcps.split(",") if t.strip()]
        if not isinstance(raw_mcps, (list, tuple)):
            raise ValidationError("mcps must be a list of MCP names")
        mcps: list[str] = []
        for m in raw_mcps:
            if not (isinstance(m, str) and m.strip()):
                raise ValidationError("each MCP name must be a non-empty string")
            mcps.append(m.strip())
        # Selecting any MCP forces the agent on — nothing else can reach an MCP
        # (STAGE_BOX_EXT_UX D-B). Overrides an explicit agent="none".
        if mcps:
            agent = "claude"
        for fld in ("repo", "ref", "setup"):
            v = kw.get(fld)
            if v is not None and not isinstance(v, str):
                raise ValidationError(f"{fld} must be a string")
        return cls(
            project=_require_name(kw.get("project")), name=name, preset=preset,
            agent=agent, editor=bool(kw.get("editor", False)),
            mcps=tuple(mcps), repo=(kw.get("repo") or "").strip(),
            ref=(kw.get("ref") or "").strip(), setup=(kw.get("setup") or ""))


@dataclass(frozen=True)
class BoxRemoveRequest:
    """Discard a sandbox box. Wipes its workspace unless ``keep_workspace`` is
    set (then the box is removed but its artifacts stay on disk). The step-up
    password is verified + consumed in broker dispatch and never reaches here
    (mirrors DestroyRequest)."""
    project: str
    name: str
    keep_workspace: bool = False

    @classmethod
    def from_kwargs(cls, **kw: Any) -> "BoxRemoveRequest":
        name = kw.get("name")
        if not (isinstance(name, str) and _BOX_NAME_RE.match(name)):
            raise ValidationError(
                "box name must be lowercase, start with a letter, and contain "
                "only letters, digits, or '-'")
        return cls(project=_require_name(kw.get("project")), name=name,
                   keep_workspace=bool(kw.get("keep_workspace")))


@dataclass(frozen=True)
class BoxListRequest:
    project: str

    @classmethod
    def from_kwargs(cls, **kw: Any) -> "BoxListRequest":
        return cls(project=_require_name(kw.get("project")))


@dataclass
class BoxAddResult:
    project: str
    name: str
    ip: str
    preset: str
    browser: bool
    agent: str
    editor: bool
    container: str


@dataclass
class BoxRemoveResult:
    project: str
    name: str


@dataclass
class BoxListResult:
    """The project's sandbox boxes with live container state. No credentials in
    any row — safe to serialise straight into the broker reply."""
    project: str
    boxes: list[dict]                       # {name, ip, agent, browser, state}


@dataclass(frozen=True)
class ExtEnableRequest:
    """Enable a baked/BYO extension on a RUNNING research project (webui-driven).
    ``name`` is catalog-gated; ``upstreams`` is the extension's explicit
    proxy-routed MCP set (project-scoped names, allowlist-validated downstream —
    NOT host-shaped, so relayable) or None for auto. ``force_auto`` re-derives =
    every allowed MCP. Auto wins if both are supplied."""
    project: str
    name: str
    upstreams: list[str] | None = None
    force_auto: bool = False

    @classmethod
    def from_kwargs(cls, **kw: Any) -> "ExtEnableRequest":
        name = kw.get("name")
        if name not in extension.known_type_names():
            raise ValidationError(
                f"unknown extension {name!r} (not a baked role or registered BYO "
                f"type)")
        force_auto = bool(kw.get("auto", False))
        upstreams: list[str] | None
        if force_auto:
            upstreams = None
        else:
            raw = kw.get("upstream")
            if raw is None:
                upstreams = None
            elif isinstance(raw, list) and all(
                    isinstance(u, str) and u for u in raw):
                upstreams = list(raw)
            else:
                raise ValidationError(
                    "upstream must be a list of non-empty MCP-name strings")
        return cls(project=_require_name(kw.get("project")), name=name,
                   upstreams=upstreams, force_auto=force_auto)


@dataclass(frozen=True)
class ExtDisableRequest:
    project: str
    name: str

    @classmethod
    def from_kwargs(cls, **kw: Any) -> "ExtDisableRequest":
        name = kw.get("name")
        # _BOX_NAME_RE charset (\A[a-z][a-z0-9-]*\Z) ⊇ the extension/BYO name
        # grammar, so a now-unregistered-but-enabled type can still be disabled.
        if not (isinstance(name, str) and _BOX_NAME_RE.match(name)):
            raise ValidationError(
                "extension name must be lowercase, start with a letter, and "
                "contain only letters, digits, or '-'")
        return cls(project=_require_name(kw.get("project")), name=name)


@dataclass(frozen=True)
class ExtListRequest:
    project: str

    @classmethod
    def from_kwargs(cls, **kw: Any) -> "ExtListRequest":
        return cls(project=_require_name(kw.get("project")))


@dataclass
class ExtEnableResult:
    project: str
    name: str
    kind: str
    upstream_source: str | None
    upstream_mcps: list[str]


@dataclass
class ExtDisableResult:
    project: str
    name: str


@dataclass
class ExtListResult:
    """A research project's extension catalog + enabled set (with live state) +
    the project's allowed MCPs (to drive the enable dialog's upstream picker). No
    secrets — safe to serialise into the broker reply."""
    project: str
    catalog: list[dict]
    enabled: list[dict]
    allowed_mcps: list[str]


# ===========================================================================
# Verbs
# ===========================================================================


class _NullProgress:
    """No-op milestone sink — the default when a verb is driven from the CLI
    (no webui op log). The broker passes a real sink (duck-typed: anything with
    a ``step(key, msg)`` method) for webui-fired ops; rscore never imports the
    broker, so the contract is structural, not a shared type."""

    def step(self, key: str, msg: str = "") -> None:  # noqa: D401
        pass


_NULL_PROGRESS = _NullProgress()


def create(req: CreateRequest, cfg: "Config" | None = None,
           progress=None) -> CreateResult:  # type: ignore[name-defined]
    """Create a project. ``req`` is already validated (no name re-check).

    Failure policy:
      • Missing prerequisites / bad inputs → die() (SystemExit), aborts.
      • A requested worker or sandbox that won't enable → aborts the whole
        create (fail-explicit; the partial project is left standing so it can
        be inspected or destroyed and retried).
      • External MCP allow-listing is best-effort: a single MCP that can't be
        granted prints a warning and creation continues (MCPs are external and
        can be transient).

    Raw progress lines keep printing to stdout (captured into the host-only
    full log). The ``progress`` sink additionally publishes ~5 allowlist-emit
    milestones at phase boundaries to the webui-visible view log — only the
    step key + a safe description, never a host path or credential.
    """
    if cfg is None:
        cfg = load_config()
    progress = progress or _NULL_PROGRESS
    project = req.name

    container_name = container_name_for(project)
    workspace_path = workspace_path_for(project, cfg)

    if container_exists(container_name):
        die(f"project {project!r} already exists (container {container_name}). "
            f"Use destroy first.")

    project_type = (PROJECT_TYPE_SANDBOX_DIND
                    if req.type is ProjectType.SANDBOX_DIND
                    else PROJECT_TYPE_RESEARCH)
    # substrate is always set by CreateRequest.from_kwargs (resolved from the
    # workflow); no derivation fallback needed here.
    substrate = req.substrate
    is_docker = substrate is Substrate.DOCKER

    # Light-path harness is the docker box's capability (WORKFLOW_TAXONOMY_S4) AND
    # the sandbox-dind flavor's (STAGE_SANDBOX_DIND_AGENT); on the research/overlay
    # workflow an effective repo/setup is noted-and-ignored, mirroring the docker-
    # side note for ignored worker/extension tokens below.
    if (not is_docker and project_type != PROJECT_TYPE_SANDBOX_DIND
            and (req.repo or req.setup)):
        print("note: --repo/--setup-script apply only to the light-path docker "
              "box / sandbox-dind; ignoring for this workflow", file=sys.stderr)
    # --agent(s) deploys an agent-dist set into the docker box (STAGE_MULTI_AGENT);
    # sandbox-dind shows the same checklist but deploys the DEFAULT_AGENT via
    # _stage_agent_dist (B1 single-agent). On the research/overlay workflow the
    # explicit set isn't wired (the fleet uses the floor), so it's noted-and-ignored.
    if (not is_docker and project_type != PROJECT_TYPE_SANDBOX_DIND
            and req.agents):
        print(f"note: --agent {','.join(req.agents)} applies only to the docker "
              "box / sandbox-dind; ignoring for this workflow", file=sys.stderr)

    substrate_image = (
        MINIMAL_IMAGE if is_docker
        else SANDBOX_DIND_IMAGE if project_type == PROJECT_TYPE_SANDBOX_DIND
        else SUPERVISOR_IMAGE)

    progress.step("validate", "checking prerequisites")

    # Verify prerequisites.
    if not run_quiet(["docker", "image", "inspect", substrate_image]):
        die(f"image {substrate_image} not found. Run `research setup` first.")
    if not container_running(ROUTER_CONTAINER):
        die(f"{ROUTER_CONTAINER} is not running. Run `research setup` first.")

    # The `docker` substrate is a single runc container — no sysbox runtime, no
    # inner dockerd. dind_mode is meaningless there; mark it "none" so the label
    # is honest and build_supervisor_docker_args skips the runtime block.
    dind_mode = "none" if is_docker else select_dind_mode(
        (req.dind.value if req.dind is not None else None) or cfg.default_dind)

    # No inner dockerd ⇒ no inner-bridge firewall on the docker substrate.
    inner_firewall = req.inner_firewall and not is_docker

    # Egress. Sandbox flavor and the docker substrate default to `locked`;
    # research to cfg default.
    egress = (req.egress.value if req.egress is not None else None) or (
        "locked" if (project_type == PROJECT_TYPE_SANDBOX_DIND or is_docker)
        else cfg.default_egress)
    if egress not in ("open", "locked"):
        die(f"invalid --egress value: {egress!r} (expected open|locked)")

    # Optional --data bind-mounts (RO inside supervisor), each at
    # /workspace/shared/data/<basename>/. Missing paths are mkdir -p'd;
    # basename collisions are a hard error.
    extra_mounts: list[str] = []
    data_basenames: dict[str, Path] = {}
    for raw in req.data:
        raw = raw.strip()
        if not raw:
            continue
        p = Path(raw).expanduser().resolve()
        if p.exists() and not p.is_dir():
            die(f"--data path exists but is not a directory: {p}")
        if not p.exists():
            p.mkdir(parents=True, exist_ok=True)
            print(f"created data directory: {p}")
        base = p.name
        if not base:
            die(f"--data path has no basename (refusing to mount root): {p}")
        if base in data_basenames:
            die(f"--data basename collision: {base!r} appears in both "
                f"{data_basenames[base]} and {p}. Rename or symlink "
                "one of the host paths so the container destinations "
                "stay distinct.")
        data_basenames[base] = p
        extra_mounts += ["-v", f"{p}:/workspace/shared/data/{base}:ro"]

    print(f"=== Creating project: {project} ===")
    ssh_port = req.ssh_port or find_free_port()
    ssh_pass = gen_password()

    # 1. Workspace dir (host bind-mount) + optional privileged-DIND volume.
    workspace_path.mkdir(parents=True, exist_ok=True)
    os.chmod(workspace_path, 0o2770)
    (workspace_path / "shared").mkdir(parents=True, exist_ok=True)
    if dind_mode == "privileged" and not volume_exists(docker_volume_name_for(project)):
        run_check(["docker", "volume", "create", docker_volume_name_for(project)])

    # 1b. Materialize the MCP bind-mount sources (files, not dirs). dind only —
    #     the docker substrate has no mcp-proxy / worker layer.
    if not is_docker:
        ensure_mcp_files(project, cfg)

    # 1c. Project-flavor + substrate + workflow marker on the volume (survives a
    #     supervisor recreate via the bind-mount; "type" is read by the webui off
    #     /projects:ro). "workflow" is provenance — what the user selected; the
    #     derived "type"/"substrate" are what the machinery branches on.
    orch_dir = workspace_path / ".orchestrator"
    orch_dir.mkdir(parents=True, exist_ok=True)
    deployed_agents = list(req.agents) if is_docker else []
    marker = {"type": project_type, "substrate": substrate.value,
              "workflow": req.workflow, "agents": deployed_agents}
    if project_type == PROJECT_TYPE_SANDBOX_DIND:
        # Freeze the box-image pins (lane-3): sandbox-dind eager-stages the box
        # harness (STAGE_DIND_UNIFY — the harness is a standing dind utility now,
        # no --with-boxes opt-in), so its boxes pull these snapshot refs at create +
        # recreate; a versions.env bump reaches the project only on a fresh
        # box-create, never on restart. (Research carries no frozen pins — it stages
        # boxes LAZILY via box_add against the current versions.env pins.)
        marker["box_image_pins"] = _box_image_pins(load_versions())
    (orch_dir / "project.json").write_text(json.dumps(marker, indent=2) + "\n")
    # Starting message (STAGE_SPAWN_GREETING) for the workflow's main shell, read
    # by the Management/Supervisor tab on first byobu new-session. Manifest-
    # derived; skip on empty so research/docker (no greeting) stage no stray file.
    if req.greeting:
        (orch_dir / "greeting").write_text(req.greeting)

    # 2. Per-project network + router wiring.
    progress.step("network", "creating project network")
    network, router_ip = ensure_project_network(project, egress)
    wire_webui_to_projects()

    # 3. Build docker run argv. Peel --enable/--disable tokens into the three
    #    registries before computing service flags. The docker substrate has no
    #    worker/extension/role-mcp layer, so only service tokens (code-server)
    #    apply there — worker/extension tokens are noted and ignored.
    enable_services, enable_workers, enable_extensions = \
        _split_enable_tokens(",".join(req.enable))
    disable_services, disable_workers, disable_extensions = \
        _split_disable_tokens(",".join(req.disable))
    role_mcp_explicit: dict[str, list[str]] = {}
    if is_docker:
        if (enable_workers or enable_extensions or disable_workers
                or disable_extensions):
            print("note: the docker substrate has no worker/extension layer; "
                  "ignoring those --enable/--disable tokens", file=sys.stderr)
        enable_workers, enable_extensions = [], []
    else:
        _dis_w, _dis_s = set(disable_workers), set(disable_extensions)
        if project_type == PROJECT_TYPE_SANDBOX_DIND:
            for w in enable_workers:
                print(f"note: --enable {w!r}: sandbox projects have no worker layer; "
                      f"ignoring (for a browser box use the websearcher box preset)",
                      file=sys.stderr)
            enable_workers = []
            enable_extensions = [s for s in enable_extensions if s not in _dis_s]
        else:
            enable_workers = [w for w in _ordered_union(defaults.enabled("worker"), enable_workers)
                              if w not in _dis_w]
            enable_extensions = [s for s in _ordered_union(defaults.enabled("extension"), enable_extensions)
                                if s not in _dis_s]
        _known_sb = extension.known_type_names()
        for s in list(enable_extensions):
            if s not in _known_sb:
                print(f"note: default sandbox {s!r} is no longer a known type; "
                      f"skipping its auto-enable", file=sys.stderr)
                enable_extensions.remove(s)
        role_mcp_explicit = _parse_role_mcp_upstream(
            list(req.role_mcp_upstream), valid_roles=set(enable_workers))
        for role in enable_workers:
            try:
                role_mcp.validate_role(role)
            except ValueError as e:
                die(str(e))
    # base = per-flavor manifest defaults (STAGE_EDITOR_DIST slice 2): the sandbox
    # workflow declares code-server off so a bare box is lean; --enable flips it on.
    service_flags = _compute_service_flags(
        enable_services, disable_services, base=(req.service_defaults or None))
    docker_args = build_supervisor_docker_args(
        container_name=container_name,
        project=project,
        network=network,
        workspace_path=workspace_path,
        ssh_port=ssh_port,
        ssh_pass=ssh_pass,
        dns_servers=cfg.sandbox_dns,
        memory=req.memory or cfg.default_memory,
        cpus=req.cpus or "",
        image=substrate_image,
        dind_mode=dind_mode,
        inner_firewall=inner_firewall,
        project_type=project_type,
        substrate=substrate.value,
        service_flags=service_flags,
    )
    if extra_mounts:
        docker_args = docker_args[:-1] + extra_mounts + [docker_args[-1]]
    # Agent dists: one RO copy-source mount per enabled agent at
    # /opt/agent-dist/<agent> + a comma-joined provenance label (STAGE_MULTI_AGENT).
    # docker-substrate only; the entrypoint loops over the mounted subdirs and cp's
    # each into the box's OWN writable ~/.local on first boot. The mounts ARE the
    # enabled set (the entrypoint reads the mounts, not the label) — empty set => no
    # mount => /opt/agent-dist absent => the boot loop no-ops (lean box). Inserted
    # before the image (last arg).
    if deployed_agents:
        agent_args = ["--label", f"{AGENT_LABEL}={','.join(deployed_agents)}"]
        for a in deployed_agents:
            agent_args += ["-v", f"{agent_dist_path(a)}:/opt/agent-dist/{a}:ro"]
        docker_args = docker_args[:-1] + agent_args + [docker_args[-1]]
    # Editor dist for the docker box (STAGE_EDITOR_DIST slice 2): a single runc box
    # has no inner dockerd to stage into, so it RO-mounts the HOST editor cache
    # directly (source = EDITOR_DIST_DIR, NOT the supervisor-side EDITOR_DIST_MOUNT).
    # Flag-gated like the agent mount above — a default-off sandbox mounts nothing
    # (lean); --enable code-server + a pulled dist wires it, and the entrypoint
    # dist block cp's it into the box's own ~/.local at first boot.
    if is_docker and service_flags.get("code-server") and editor_dist_present():
        docker_args = (docker_args[:-1]
                       + ["-v", f"{EDITOR_DIST_DIR}:{EDITOR_DIST_MOUNT}:ro"]
                       + [docker_args[-1]])
    if not is_docker:
        ext_mounts = _extension_external_mounts(project)
        if ext_mounts:
            docker_args = docker_args[:-1] + ext_mounts + [docker_args[-1]]

    # 4. Create container.
    progress.step("create-container", "creating project container")
    run_check(["docker", *docker_args])

    # 5. Inject default route via router (egress traverses iptables).
    inject_route(container_name, router_ip)

    granted: list[str] = []
    workers_enabled: list[str] = []
    extensions_enabled: list[str] = []

    clone_dir = ""
    if is_docker:
        # No inner dockerd: nothing to stage, no proxy, no worker/extension cone.
        # 6'. Light-path harness: clone the workflow repo + run setup on the box
        #     (after inject_route, so egress works). Create-time only; raises
        #     HarnessError on failure (box left standing).
        clone_dir = _run_light_harness(
            container_name, req.repo, req.ref, req.setup, req.github_pat, progress)
        progress.step("ready", "project ready")
    else:
        # 6. Wait for inner dockerd, then stage the inner images.
        progress.step("stage-images", "staging inner images")
        wait_for_inner_dockerd(container_name)
        is_management = project_type == PROJECT_TYPE_SANDBOX_DIND
        if is_management:
            # Sandbox-dind is the docker `sandbox` flavor + DIND
            # (STAGE_SANDBOX_DIND_AGENT): the supervisor RUNS an agent itself and
            # gets the light-path harness; the rs-sandbox box harness is OPT-IN.
            # Stage the agent dist into the supervisor's OWN ~/.local
            # (deploy_local=True) — that staging ALSO populates /opt/agent-dist for
            # any boxes the harness later spawns.
            _stage_agent_dist(container_name)
            # Editor gated on the resolved code-server flag, like research (the
            # sandbox-dind manifest defaults code-server OFF ⇒ lean box unless
            # --enable code-server). Staged before any box spawn so a box can
            # RO-mount /opt/editor-dist.
            if editor_dist_present():
                _stage_editor_dist(container_name,
                                   deploy_local=service_flags.get("code-server", True))
            # Box harness (STAGE_DIND_UNIFY — a standing dind utility, no longer an
            # opt-in): stage the rs-sandbox CLI (no longer baked) + deliver the box
            # images (MINT site, push=True; pins were frozen into the marker above).
            # Eager for sandbox-dind; research delivers the same harness LAZILY on
            # first box_add (research create/recreate never touch boxes — frozen lane).
            _stage_rs_sandbox(container_name)
            _deliver_box_images(container_name, network,
                                _box_image_pins(load_versions()), push=True)
            # Stage the resolved box-preset catalog so the in-supervisor rs-sandbox
            # can resolve presets for a directly-invoked `rs-sandbox create`
            # (box_add refreshes it live; STAGE_BOX_EXT_UX). Best-effort.
            _stage_box_catalog(workspace_path, strict=False)
            # Light-path harness: clone repo@ref + run setup on the supervisor
            # (after inject_route; raises HarnessError on failure, box left standing).
            clone_dir = _run_light_harness(
                container_name, req.repo, req.ref, req.setup, req.github_pat, progress)
        else:
            stage_worker_image(container_name, ANALYSIS_IMAGE)
            # Stage the agent dist into the supervisor (its own ~/.local + the
            # /opt/agent-dist the worker/role-MCP/PI fleet RO-mounts). No bake
            # anywhere now (STAGE_AGENT_DIST slice 2). ORDER IS LOAD-BEARING: this
            # MUST stay before the worker/extension enable cone below — a role-MCP or
            # PI container brought up by that cone mounts /opt/agent-dist and would
            # boot claude-less (failing only on first send_job) if staged after.
            _stage_agent_dist(container_name)
            # Editor dist (STAGE_EDITOR_DIST): staged before the enable cone too so
            # interactive PI containers RO-mount a populated /opt/editor-dist.
            # deploy_local brings up the supervisor's OWN editor from the dist (no
            # bake now), gated on the resolved code-server flag so a
            # --disable code-server project deploys nothing. The create-time floor
            # already guaranteed a dist is present when the editor is enabled.
            if editor_dist_present():
                _stage_editor_dist(container_name,
                                   deploy_local=service_flags.get("code-server", True))

        # 6b/6c. The MCP proxy/reload/auto-allow cone runs for ALL dind now
        #        (STAGE_DIND_UNIFY): research AND sandbox-dind get the proxy so
        #        extensions with proxy-routed upstreams (wrangler-style) work on
        #        both. rs-sandbox-dind bakes mcp-reload + mcp_render_config.py
        #        (Dockerfile.sandbox-dind) so the reload exec resolves; the proxy
        #        image is staged just below. The guard is `not is_docker` (always
        #        true inside this dind branch) so research's executed statements stay
        #        byte-identical — only the gate widened from `not is_management`.
        if not is_docker:
            stage_worker_image(container_name, MCP_PROXY_IMAGE)

            # 6b. Re-run mcp-reload now that the proxy image is staged.
            run(["docker", "exec", container_name, "/usr/local/bin/mcp-reload"],
                capture_output=True)

            # 6c. Auto-allow MCPs per --mcp. BEST-EFFORT: external MCPs can be
            #     transient, so a single failure warns and creation continues.
            requested = _resolve_create_mcp_arg(req.mcp)
            for mcp_name in requested:
                ok, msg = _allow_mcp_for_project(project, cfg, mcp_name, do_reload=False)
                if ok:
                    granted.append(mcp_name)
                else:
                    print(f"warning: skip auto-allow {mcp_name!r}: {msg}", file=sys.stderr)
            if granted:
                _supervisor_mcp_reload(container_name)

        # 6d. worker sugar (--enable <worker>). FAIL-EXPLICIT: a worker that
        #     won't enable aborts the create (no swallow). Workers and the
        #     same-named extension are independent surfaces (no auto-mirror).
        #     This is the slow tail — a role-MCP like websearcher builds/starts
        #     a Chromium container — so the "wire" milestone is emitted AFTER
        #     the loops, not before: its view-log row stays pending (the UI shows
        #     it in-progress) until the enabling actually finishes, instead of
        #     checking ✓ up front while the box sits on a silent wait.
        for role in enable_workers:
            _role_mcp_enable(project, cfg, role, role_mcp_explicit.get(role))
            workers_enabled.append(role)

        # 6e. extension sugar (--enable <extension>). FAIL-EXPLICIT, same as
        #     workers. Twin names resolve worker-first in _split_enable_tokens,
        #     so a name here is never also in enable_workers. Baked extensions
        #     auto-derive their upstreams (= all allowed MCPs) on first enable.
        for name in enable_extensions:
            _extension_enable(project, cfg, name)
            extensions_enabled.append(name)

        progress.step("wire", "enabling workers and sandboxes")

    # 7. Return result (the front-end formats the report from this).
    return CreateResult(
        project=project,
        container=container_name,
        workspace=str(workspace_path),
        network=network,
        egress=egress,
        dind_mode=dind_mode,
        inner_firewall=inner_firewall,
        project_type=project_type,
        substrate=substrate.value,
        workflow=req.workflow,
        ssh_port=int(ssh_port),
        ssh_password=ssh_pass,
        data_mounts={b: str(src) for b, src in data_basenames.items()},
        mcps=granted,
        workers=workers_enabled,
        extensions=extensions_enabled,
        repo=req.repo,
        clone_dir=clone_dir,
        agents=deployed_agents,
    )


def destroy(req: DestroyRequest, cfg: "Config" | None = None,
            progress=None) -> None:  # type: ignore[name-defined]
    """Tear a project down: container + workspace dir + DIND volume + network,
    plus its router MCP-allow rules. Confirmation is the front-end's job — this
    verb just destroys. die()s if the project doesn't exist."""
    if cfg is None:
        cfg = load_config()
    progress = progress or _NULL_PROGRESS
    project = req.name
    container = container_name_for(project)
    if not container_exists(container):
        die(f"project {project!r} does not exist")
    progress.step("validate", "located project")

    project_root = project_root_for(project, cfg)
    docker_volume = docker_volume_name_for(project)
    network = project_network_for(project)

    # Clean up per-project MCP rules in the router so iptables doesn't
    # accumulate orphan ACCEPTs after the project network is gone.
    progress.step("router", "removing router rules")
    if network_exists(network) and container_running(ROUTER_CONTAINER):
        try:
            subnet = get_network_subnet(network)
        except SystemExit:
            subnet = ""
        for ent in load_project_allowlist(project, cfg):
            ip = ent.get("ip")
            port = ent.get("port")
            if subnet and isinstance(ip, str) and isinstance(port, int):
                run(["docker", "exec", ROUTER_CONTAINER,
                     "/scripts/mcp-deny.sh", subnet, ip, str(port)],
                    capture_output=True)

    progress.step("remove-container", "removing container")
    run(["docker", "rm", "-f", container], capture_output=True)
    progress.step("cleanup", "removing workspace, volume and network")
    if project_root.exists():
        shutil.rmtree(project_root, ignore_errors=True)
    if volume_exists(docker_volume):
        run(["docker", "volume", "rm", docker_volume], capture_output=True)
    remove_project_network(project)


def start(req: StartStopRequest, cfg: "Config" | None = None,
          progress=None) -> list[ActionResult]:  # type: ignore[name-defined]
    """Start a stopped project (or all). On sysbox a plain `docker start` after
    `docker stop` fails, so dind-sysbox projects route through
    _recreate_supervisor: fresh container ID + bindings, workspace/creds/network
    preserved. A docker-substrate project is a plain runc container with no
    sysbox bindings — it uses _start_docker_substrate (docker start + route
    re-inject). Fail-explicit: a recreate/start that dies aborts (no swallow)."""
    if cfg is None:
        cfg = load_config()
    progress = progress or _NULL_PROGRESS
    if not container_running(ROUTER_CONTAINER):
        die(f"{ROUTER_CONTAINER} is not running. Run `research start` first.")
    progress.step("validate", "checking project")
    if req.all:
        containers = get_supervisor_containers()
    else:
        containers = [{"name": container_name_for(req.name), "project": req.name}]
    results: list[ActionResult] = []
    for c in containers:
        if not container_exists(c["name"]):
            results.append(ActionResult(c["name"], c.get("project"), "start", "skip:absent"))
            continue
        if container_running(c["name"]):
            results.append(ActionResult(c["name"], c.get("project"), "start", "skip:already"))
            continue
        print(f"=== Starting project: {c['project']} ===")
        if _container_substrate(c["name"]) == Substrate.DOCKER.value:
            progress.step("start", "starting container")
            _start_docker_substrate(c["project"], cfg)
        else:
            progress.step("recreate", "recreating supervisor")
            _recreate_supervisor(c["project"], cfg)
        results.append(ActionResult(c["name"], c["project"], "start", "ok"))
    return results


def stop(req: StartStopRequest, cfg: "Config" | None = None,
         progress=None) -> list[ActionResult]:  # type: ignore[name-defined]
    """Stop a project (or all). Fail-explicit: a `docker stop` that fails dies."""
    progress = progress or _NULL_PROGRESS
    progress.step("validate", "checking project")
    if req.all:
        names = [c["name"] for c in get_supervisor_containers()]
    else:
        names = [container_name_for(req.name)]
    results: list[ActionResult] = []
    for name in names:
        if not container_exists(name):
            results.append(ActionResult(name, None, "stop", "skip:absent"))
            continue
        progress.step("stop", "stopping container")
        run_check(["docker", "stop", name])
        results.append(ActionResult(name, None, "stop", "ok"))
    return results


def list_projects(cfg: "Config" | None = None) -> list[ProjectSummary]:  # type: ignore[name-defined]
    """Every supervisor container with its state + SSH endpoint (if running)."""
    out: list[ProjectSummary] = []
    for c in get_supervisor_containers():
        ssh = None
        if c["state"] == "running":
            port = get_ssh_port(c["name"])
            ssh = f"localhost:{port}" if port else None
        out.append(ProjectSummary(project=c["project"], state=c["state"], ssh=ssh))
    return out


def status(req: StatusRequest, cfg: "Config" | None = None) -> ProjectStatus:  # type: ignore[name-defined]
    """Project state + workspace + (if running) inner-worker lines and the
    .workers/ registry count. die()s if the project doesn't exist."""
    if cfg is None:
        cfg = load_config()
    container = container_name_for(req.name)
    if not container_exists(container):
        die(f"project {req.name!r} does not exist")
    state = run_check(["docker", "inspect", "-f", "{{.State.Status}}", container]).stdout.strip()
    ssh_port = get_ssh_port(container) if state == "running" else None
    workspace = workspace_path_for(req.name, cfg)

    inner: list[str] = []
    reg_count = 0
    if state == "running":
        r = run(["docker", "exec", container, "docker", "ps", "-a",
                 "--format", "{{.Names}}\t{{.Status}}\t{{.Image}}"],
                capture_output=True)
        if r.returncode == 0 and r.stdout.strip():
            inner = r.stdout.strip().splitlines()
        reg = run(["docker", "exec", container, "sh", "-c",
                   "ls /workspace/.workers/*.json 2>/dev/null | wc -l"],
                  capture_output=True)
        if reg.returncode == 0:
            try:
                reg_count = int(reg.stdout.strip() or "0")
            except ValueError:
                reg_count = 0
    return ProjectStatus(
        project=req.name, container=container, state=state,
        workspace=str(workspace), ssh_port=ssh_port,
        inner_workers=inner, registry_count=reg_count)


def update(req: UpdateRequest, cfg: "Config" | None = None,
           progress=None) -> UpdateResult:  # type: ignore[name-defined]
    """Push edited code into a running project. Always recreates the supervisor
    (the only safe shape on sysbox): file-only mode docker-cp's edited files
    into the fresh container before first start; --rebuild rebuilds images
    first. Fail-explicit: a worker/extension enable or disable that dies aborts.
    Defaults are NOT re-folded (create-time only)."""
    if cfg is None:
        cfg = load_config()
    progress = progress or _NULL_PROGRESS
    project = req.name
    container = container_name_for(project)
    if not container_exists(container):
        die(f"project {project!r} does not exist")
    # The docker substrate has no in-container editable surface yet (no
    # templates, no worker/extension cone) and no sysbox store to re-stage — update
    # is deferred for it (WORKFLOW_TAXONOMY_S1.md). Refuse cleanly rather than
    # run the dind recreate path against a runc container.
    if _container_substrate(container) == Substrate.DOCKER.value:
        die("`update` is not yet supported for the docker substrate; "
            "destroy + recreate instead")
    if not container_running(ROUTER_CONTAINER):
        die(f"{ROUTER_CONTAINER} is not running. Run `research start` first.")
    progress.step("validate", "validating update")

    print(f"=== Updating project: {project} ===")

    # Validate --enable worker tokens up front so a typo doesn't waste a rebuild.
    enable_services, enable_workers, enable_extensions = \
        _split_enable_tokens(",".join(req.enable))
    disable_services, disable_workers, disable_extensions = \
        _split_disable_tokens(",".join(req.disable))
    role_mcp_explicit = _parse_role_mcp_upstream(
        list(req.role_mcp_upstream), valid_roles=set(enable_workers))
    for role in enable_workers:
        try:
            role_mcp.validate_role(role)
        except ValueError as e:
            die(str(e))

    if req.rebuild:
        progress.step("rebuild", "rebuilding images")
        print("rebuilding images...")
        _build_images(force=True)

    hook = None
    if not req.rebuild:
        def hook(c: str) -> None:
            print(f"copying edited files into {c}...")
            for rel in _docker_cp_supervisor_files(c):
                print(f"  {rel}")

    flags_override: dict[str, bool] | None = None
    if enable_services or disable_services:
        base = _read_service_flags(container)
        flags_override = _compute_service_flags(
            enable_services, disable_services, base=base)

    # Disables run BEFORE the recreate (the recreate's restart loops read
    # role-mcps.json / extensions.json; a removed entry must not come back up).
    # Fail-explicit: a disable that dies aborts.
    workers_disabled: list[str] = []
    extensions_disabled: list[str] = []
    if disable_workers or disable_extensions:
        progress.step("disable", "applying disables")
    for role in disable_workers:
        _role_mcp_disable(project, cfg, role)
        workers_disabled.append(role)
    for name in disable_extensions:
        _extension_disable(project, cfg, name)
        extensions_disabled.append(name)

    progress.step("recreate", "recreating supervisor")
    _recreate_supervisor(
        project, cfg,
        force_restage=req.rebuild,
        post_create_hook=hook,
        service_flags=flags_override,
    )

    # Sandbox projects have no /workspace/.claude/ by design — skip the refresh.
    refreshed = False
    if not req.keep_claude and _container_project_type(container) != PROJECT_TYPE_SANDBOX_DIND:
        progress.step("refresh", "refreshing workspace templates")
        print("refreshing /workspace/.claude/ from templates...")
        _refresh_workspace_claude_templates(container)
        refreshed = True

    # worker + extension enables (idempotent on re-run). Fail-explicit.
    if enable_workers or enable_extensions:
        progress.step("enable", "applying enables")
    workers_enabled: list[str] = []
    extensions_enabled: list[str] = []
    for role in enable_workers:
        _role_mcp_enable(project, cfg, role, role_mcp_explicit.get(role))
        workers_enabled.append(role)
    for name in enable_extensions:
        _extension_enable(project, cfg, name)
        extensions_enabled.append(name)

    return UpdateResult(
        project=project, rebuilt=req.rebuild, refreshed_claude=refreshed,
        workers_enabled=workers_enabled, extensions_enabled=extensions_enabled,
        workers_disabled=workers_disabled, extensions_disabled=extensions_disabled)


# ===========================================================================
# Relocated substrate — moved verbatim out of research.py. These lifecycle
# helpers + Config + constants back the verbs above; research.py imports them
# back via `from rscore import *`.
# ===========================================================================


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent.parent  # repo root (rscore lives in cli/)

# Shared per-project substrate (DIND + ssh + byobu + code-server). Both
# per-project container images FROM it (STAGE_SANDBOX_PROJECT.md image split).
# No-DIND base + its runnable leaf — the `docker` containment substrate
# (WORKFLOW_TAXONOMY_S1.md). rs-substrate-base now FROMs rs-minimal-base, adding
# only the sysbox-DIND delta; rs-minimal FROMs rs-minimal-base directly (single
# runc container, no inner dockerd, no agent — agents are dist-delivered).
MINIMAL_BASE_IMAGE = "rs-minimal-base:latest"
MINIMAL_IMAGE = "rs-minimal:latest"                # `docker` substrate leaf
SUBSTRATE_BASE_IMAGE = "rs-substrate-base:latest"
SUPERVISOR_IMAGE = "rs-supervisor:latest"          # research flavor (agent leaf)
SANDBOX_DIND_IMAGE = "rs-sandbox-dind:latest"        # sandbox-dind flavor (agent-less)
ANALYSIS_IMAGE = "rs-analysis-base:latest"
MCP_PROXY_IMAGE = "rs-mcp-proxy:latest"
ROLE_MCP_BASE_IMAGE = "rs-role-mcp-base:latest"
PI_BASE_IMAGE = "rs-pi-base:latest"
# Generic PI-isolated image (one for every BYO type). FROM rs-ext-base (lane-3) +
# git + clone/setup entrypoint, fully private (no artifact-contract). The :latest
# tag is the build output + the GENERIC_REGISTRY_IMAGES host base; the build retags
# it :<PI_ISOLATED_VERSION> for the registry push, and a project's inner dockerd
# PULLS the snapshot ref (frozen into extensions.json at enable).
PI_ISOLATED_IMAGE = "rs-pi-isolated:latest"
# Clean, research-DECOUPLED base for EXTENSION node images (STAGE_FEATURE_STAGING
# C1). FROM miniconda3 (NOT rs-analysis-base) — sibling of rs-minimal-base, forked
# on rebuild-blast-radius. Ext leaves (rs-ext-<name>) AND the lane-3 generic images
# (pi-isolated, sandbox-box) FROM it. See cli/extension.py EXT_REGISTRY_REFS /
# GENERIC_REGISTRY_IMAGES.
EXT_BASE_IMAGE = "rs-ext-base:latest"
# Disposable box image for the agent-less sandbox-project flavor
# (STAGE_SANDBOX_PROJECT.md). FROM rs-ext-base (lane-3) — lean (no data-science
# stack), no artifact-contract. Registry-delivered: the build retags :latest →
# :<SANDBOX_BOX_VERSION>, the host pushes it, and a sandbox-dind project's inner
# dockerd PULLS the snapshot ref (frozen in project.json) + retags it back to this
# :latest the in-supervisor rs-sandbox runs.
SANDBOX_BOX_IMAGE = "rs-sandbox-box:latest"
# Browser variant — FROM rs-sandbox-box + @playwright/mcp + Chromium, wired
# into the box's claude as a stdio MCP (the playwright bundle lifted from the
# websearcher image, WITHOUT its role.md harness). Selected by the websearcher
# box preset (image="browser"). Registry-delivered like the plain box.
SANDBOX_BOX_BROWSER_IMAGE = "rs-sandbox-box-browser:latest"
INNER_NETWORK = "rs-inner"
WEBUI_IMAGE = "rs-webui:latest"
WEBUI_CONTAINER = "rs-webui"
ROUTER_CONTAINER = "rs-router"
ROUTER_NETWORK = "rs-sandbox"
# Local extension-image registry (STAGE_FEATURE_STAGING C1). The in-container
# listen port is FIXED at 5000 — it is baked into the inner daemon's
# insecure-registries entry (agent/Dockerfile.substrate-base) AND into
# extension.EXT_REGISTRY ("rs-registry:5000"), the pull locator. ONLY the HOST
# loopback publish port is configurable (REGISTRY_HOST_PORT in versions.env), used
# for the host push (loopback = insecure-by-default, no host daemon.json change).
REGISTRY_CONTAINER = "rs-registry"
REGISTRY_INNER_PORT = "5000"
DEFAULT_REGISTRY_VERSION = "2.8.3"
DEFAULT_REGISTRY_HOST_PORT = "5000"
REGISTRY_CACHE_DIR = Path.home() / ".research-sandbox" / "registry"
CONTAINER_PREFIX = "rs-project-"
DOCKER_VOLUME_PREFIX = "rs-docker-"
PROJECT_NETWORK_PREFIX = "rs-net-"
PROJECT_LABEL = "research.project"
DIND_MODE_LABEL = "research.dind"
# Project flavor: "research" (default — supervisor agent + workers) or
# "sandbox" (agent-less collection of isolated boxes, STAGE_SANDBOX_PROJECT.md).
# Mirrored into .orchestrator/project.json so the webui (which reads only the
# project volume, no docker socket) and the in-supervisor rs-sandbox CLI can
# branch on it.
PROJECT_TYPE_LABEL = "research.project_type"
PROJECT_TYPE_RESEARCH = "research"
PROJECT_TYPE_SANDBOX_DIND = "sandbox-dind"
# Containment substrate (WORKFLOW_TAXONOMY_S1.md). Mirrored onto the container
# label so start()/recreate read it back; legacy containers without it default
# to dind-sysbox. Also recorded in .orchestrator/project.json for the webui.
SUBSTRATE_LABEL = "research.substrate"
AGENT_LABEL = "research.agents"  # comma-joined agent-dist set a docker box deployed (STAGE_MULTI_AGENT)
MCP_CONTAINER_PREFIX = "rs-mcp-"
MCP_LABEL = "research.mcp"
MCP_NAME_LABEL = "research.mcp_name"
PROBE_IMAGE = "busybox:1.36"

# Image version pins live in a visible root-level manifest, not scattered as
# Dockerfile ARG defaults. `_build_images` threads them as `docker build
# --build-arg`; see versions.env for the workflow + per-pin caveats.
VERSIONS_FILE = SCRIPT_DIR / "versions.env"

# Upstream datasource per pin, consumed by `research images outdated`. Kept here
# rather than annotated into versions.env so the manifest stays a clean
# KEY=VALUE file. Two ecosystems are stdlib-awkward to query honestly — the
# docker-ce static repo (HTML dir listing) and the VS Code Marketplace gallery
# (POST query API) — so they're marked "manual" with a URL instead of a faked
# check. A pin present in versions.env but absent here prints "no source"; a
# source whose key isn't pinned is skipped. Keep in sync when adding a pin.
VERSION_SOURCES: dict[str, dict[str, str]] = {
    "CODE_SERVER_VERSION": {
        "kind": "github-releases",
        "repo": "coder/code-server",
        # The tag is the code-server version; the extension-compat gate is the
        # *bundled* VS Code version, which this check does NOT resolve — open
        # the release to confirm before bumping for an engines.vscode reason.
        "note": "verify bundled VS Code in the release notes before bumping",
    },
    "PLAYWRIGHT_MCP_VERSION": {"kind": "npm", "pkg": "@playwright/mcp"},
    "PYYAML_VERSION": {"kind": "pypi", "pkg": "PyYAML"},
    "AIOHTTP_VERSION": {"kind": "pypi", "pkg": "aiohttp"},
    "DOCKER_VERSION": {
        "kind": "manual",
        "url": "https://download.docker.com/linux/static/stable/x86_64/",
    },
    "DATA_WRANGLER_VERSION": {
        "kind": "manual",
        "url": "https://marketplace.visualstudio.com/items"
               "?itemName=ms-toolsai.datawrangler",
    },
    "CLAUDE_CODE_EXT_VERSION": {
        "kind": "manual",
        # Open VSX item page — eyeball the latest linux-x64 build before bumping.
        "url": "https://open-vsx.org/extension/Anthropic/claude-code",
    },
}

# Per-supervisor service registry. KNOWN_SERVICES lists every kind the webui
# might render; --enable / --disable on `project create|update` flips
# `research.service.<id>` labels and `RS_SERVICE_<ID>` env vars in lockstep.
# ALWAYS_ON_SERVICES can't be disabled — `supervisor` (the SSH + byobu
# substrate, formerly `xterm`) is what `research project ssh` rides on;
# disabling it would brick the project. New service kinds extend both
# lists in the same commit that ships the entrypoint conditional and the
# registry entry.
KNOWN_SERVICES: list[str] = ["supervisor", "code-server"]
ALWAYS_ON_SERVICES: set[str] = {"supervisor"}
SERVICE_LABEL_PREFIX = "research.service."

# In-supervisor ports for code-server's lazy-start stub. The stub listens on
# CODE_SERVER_STUB_PORT (the port the webui reverse-proxy hits via container
# DNS) and spawns code-server on CODE_SERVER_UPSTREAM_PORT, which never
# leaves 127.0.0.1. Ports are constants — supervisors are single-tenant
# inside their own network namespace, no contention possible.
CODE_SERVER_STUB_PORT = 8443
CODE_SERVER_UPSTREAM_PORT = 8444

# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------


def die(msg: str, code: int = 1) -> "NoReturn":  # type: ignore[name-defined]
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(code)


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, text=True, **kw)


def run_check(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    r = run(cmd, capture_output=True, **kw)
    if r.returncode != 0:
        die(f"command failed: {' '.join(cmd)}\n{r.stderr.strip()}")
    return r


def run_quiet(cmd: list[str]) -> bool:
    return run(cmd, capture_output=True).returncode == 0


# ---------------------------------------------------------------------------
# .env state
# ---------------------------------------------------------------------------


class Config:
    def __init__(self) -> None:
        self.projects_dir: str = (
            os.environ.get("PROJECTS_DIR") or str(SCRIPT_DIR / "container_volumes")
        )
        self.sandbox_dns: list[str] = [
            s.strip()
            for s in os.environ.get("SANDBOX_DNS", "9.9.9.9,149.112.112.112").split(",")
            if s.strip()
        ]
        self.default_profile: str = os.environ.get("DEFAULT_PROFILE", "python")
        self.default_memory: str = os.environ.get("DEFAULT_MEMORY", "")
        # Per-role-MCP container memory cap. Blast-radius backstop: if a
        # runaway claude -p / Chromium / DB-MCP child triggers OOM, the
        # killer takes the role container, not the supervisor. Default 2g:
        # at 1g, a single browser-bearing call (Chromium ~400MB resident +
        # daemon ~500MB + renderer/GPU subprocesses) leaves no headroom
        # and risks OOM on real loads. At 4g, value is wasteful for non-
        # browser roles (wrangler peaks <500MB) but harmless. Pair with
        # default_role_mcp_max_concurrent_calls — bumping memory should
        # bump concurrency proportionally. Override per .env or per-role
        # at enable: `--memory 4g`.
        self.default_role_mcp_memory: str = os.environ.get(
            "DEFAULT_ROLE_MCP_MEMORY", "2g")
        # Per-role-MCP daemon-side concurrency cap. send_job calls beyond
        # this return an MCP tool error with structured payload
        # {reason: "concurrency_limit", ...} immediately — no spawn, no
        # Chromium / DB connection wasted on a refused call. Default 3:
        # each browser-bearing concurrent call is ~400MB resident; 3 fits
        # the 2g default_role_mcp_memory comfortably with daemon overhead.
        # Non-browser roles (wrangler, echo-mcp) effectively uncapped in
        # practice — their per-call footprint is tiny. Set to 0 to disable
        # the cap entirely. Override per .env or per-role at enable:
        # `--max-concurrent-calls 6`.
        self.default_role_mcp_max_concurrent_calls: int = int(
            os.environ.get("DEFAULT_ROLE_MCP_MAX_CONCURRENT_CALLS", "3"))
        self.default_dind: str = os.environ.get("DEFAULT_DIND", "auto")
        self.default_egress: str = os.environ.get("DEFAULT_EGRESS", "open")


def load_config() -> Config:
    env = SCRIPT_DIR / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            k, _, v = line.partition("=")
            if k and k not in os.environ:
                os.environ[k] = v
    return Config()


def read_env_value(key: str) -> str:
    """Read a single key from .env (commented lines ignored)."""
    env_file = SCRIPT_DIR / ".env"
    if not env_file.exists():
        return ""
    for line in env_file.read_text().splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or not stripped:
            continue
        if "=" in stripped:
            k, _, v = stripped.partition("=")
            if k.strip() == key:
                return v.strip()
    return ""


def update_env_key(key: str, value: str) -> None:
    """Set or append KEY=VALUE in .env. Replaces a commented `# KEY=` line
    in place if present, so .env stays diffable across edits."""
    env_file = SCRIPT_DIR / ".env"
    if not env_file.exists():
        env_file.write_text(f"{key}={value}\n")
        return
    lines = env_file.read_text().splitlines()
    found = False
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith(f"{key}=") or stripped.startswith(f"# {key}="):
            lines[i] = f"{key}={value}"
            found = True
            break
    if not found:
        lines.append(f"{key}={value}")
    env_file.write_text("\n".join(lines) + "\n")


def docker_compose(*compose_args: str) -> None:
    run_check([
        "docker", "compose",
        "-f", str(SCRIPT_DIR / "docker-compose.yml"),
        *compose_args,
    ])


# ---------------------------------------------------------------------------
# Port / password generation
# ---------------------------------------------------------------------------


def gen_password() -> str:
    return secrets.token_urlsafe(16)


def find_free_port(base: int = 2240) -> int:
    # 2240–3239 avoids ADS's 2222–3221 range so the two can coexist on one host.
    for port in range(base, base + 1000):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.bind(("", port))
                return port
        except OSError:
            continue
    die(f"could not find a free port starting at {base}")


# ---------------------------------------------------------------------------
# Docker helpers
# ---------------------------------------------------------------------------


def container_running(name: str) -> bool:
    r = run(["docker", "inspect", "-f", "{{.State.Running}}", name], capture_output=True)
    return r.stdout.strip() == "true"


def container_exists(name: str) -> bool:
    return run_quiet(["docker", "inspect", name])


def volume_exists(name: str) -> bool:
    return run_quiet(["docker", "volume", "inspect", name])


def network_exists(name: str) -> bool:
    return run_quiet(["docker", "network", "inspect", name])


def sysbox_available() -> bool:
    r = run(["docker", "info", "--format", "{{json .Runtimes}}"], capture_output=True)
    return '"sysbox-runc"' in r.stdout


def select_dind_mode(mode: str) -> str:
    if mode == "auto":
        if sysbox_available():
            return "sysbox"
        print(
            "note: sysbox-runc not found; falling back to --privileged DIND "
            "(weaker isolation; see README).",
            file=sys.stderr,
        )
        return "privileged"
    if mode not in ("sysbox", "privileged"):
        die(f"invalid --dind value: {mode!r} (expected auto|sysbox|privileged)")
    if mode == "sysbox" and not sysbox_available():
        die(
            "sysbox-runc is not available on this host. "
            "Install it or rerun with --dind privileged."
        )
    return mode


def get_supervisor_containers() -> list[dict]:
    fmt = "{{.Names}}\t{{.State}}\t{{.Label \"" + PROJECT_LABEL + "\"}}"
    r = run_check([
        "docker", "ps", "-a",
        "--filter", f"label={PROJECT_LABEL}",
        "--format", fmt,
    ])
    containers = []
    for line in r.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) >= 3 and parts[2]:
            containers.append({"name": parts[0], "state": parts[1], "project": parts[2]})
    return containers


def container_name_for(project: str) -> str:
    return f"{CONTAINER_PREFIX}{project}"


def docker_volume_name_for(project: str) -> str:
    return f"{DOCKER_VOLUME_PREFIX}{project}"


def project_root_for(project: str, cfg: "Config") -> Path:
    return Path(cfg.projects_dir).expanduser().resolve() / project


def workspace_path_for(project: str, cfg: "Config") -> Path:
    return project_root_for(project, cfg) / "workspace"


def project_network_for(project: str) -> str:
    return f"{PROJECT_NETWORK_PREFIX}{project}"


def wait_for_inner_dockerd(container: str, timeout: int = 60) -> None:
    import time

    deadline = time.time() + timeout
    print("waiting for inner dockerd...")
    while time.time() < deadline:
        r = run(["docker", "exec", container, "docker", "info"], capture_output=True)
        if r.returncode == 0:
            return
        time.sleep(1)
    die(f"inner dockerd did not become ready within {timeout}s "
        f"(check `docker logs {container}` and `docker exec {container} sudo cat /tmp/dockerd.log`)")


def stage_worker_image(container: str, image: str, force: bool = False) -> None:
    """Push the host-built image into the supervisor's inner Docker daemon.

    With ``force=True`` the inner daemon's existing copy of the tag (if any)
    is removed first, so the load brings in the rebuilt content rather than
    being a no-op when the tag points at a stale image (the case when
    `research project update --rebuild` re-stages after a host rebuild)."""
    if not run_quiet(["docker", "image", "inspect", image]):
        die(f"host image {image} not found; run `research setup`.")
    # Skip if already present inside, unless --force.
    present = run(["docker", "exec", container, "docker", "image", "inspect", image],
                  capture_output=True).returncode == 0
    if present and not force:
        return
    if present:
        run(["docker", "exec", container, "docker", "image", "rm", "-f", image],
            capture_output=True)
    print(f"staging {image} into the supervisor (this can take a minute)...")
    save = subprocess.Popen(
        ["docker", "save", image],
        stdout=subprocess.PIPE,
    )
    assert save.stdout is not None
    load = subprocess.Popen(
        ["docker", "exec", "-i", container, "docker", "load"],
        stdin=save.stdout,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    save.stdout.close()
    save.wait()
    load_out, _ = load.communicate()
    if load.returncode != 0:
        die(f"failed to stage {image}:\n{load_out.decode(errors='replace')}")


def get_ssh_port(container: str) -> str | None:
    r = run(
        ["docker", "inspect", "-f",
         "{{range $k, $v := .NetworkSettings.Ports}}{{if eq $k \"22/tcp\"}}"
         "{{(index $v 0).HostPort}}{{end}}{{end}}",
         container],
        capture_output=True,
    )
    return r.stdout.strip() or None


# ---------------------------------------------------------------------------
# Networking (router + per-project networks)
# ---------------------------------------------------------------------------


def get_router_ip(network: str) -> str:
    r = run_check([
        "docker", "inspect", ROUTER_CONTAINER,
        "-f", '{{(index .NetworkSettings.Networks "' + network + '").IPAddress}}',
    ])
    ip = r.stdout.strip()
    if not ip:
        die(f"{ROUTER_CONTAINER} is not attached to {network}")
    return ip


def get_network_subnet(network: str) -> str:
    r = run_check([
        "docker", "network", "inspect", network,
        "-f", "{{(index .IPAM.Config 0).Subnet}}",
    ])
    return r.stdout.strip()


def inject_route(container: str, router_ip: str) -> None:
    # `replace` (vs `add`) handles both first-boot (no default route) and
    # non-internal networks (Docker has already set a default route to the
    # bridge gateway; we overwrite it to point at the router for egress
    # enforcement).
    run_check([
        "docker", "run", "--rm", "--privileged",
        "--network", f"container:{container}",
        "alpine:3.20", "ip", "route", "replace", "default", "via", router_ip,
    ])


def apply_firewall_rules(network: str, mode: str) -> None:
    subnet = get_network_subnet(network)
    run_check([
        "docker", "exec", ROUTER_CONTAINER,
        "/scripts/apply-rules.sh", subnet, mode,
    ])


def remove_firewall_rules(network: str) -> None:
    if not network_exists(network):
        return
    subnet = get_network_subnet(network)
    run(["docker", "exec", ROUTER_CONTAINER, "/scripts/remove-rules.sh", subnet],
        capture_output=True)


def ensure_project_network(project: str, mode: str) -> tuple[str, str]:
    """Create per-project bridge network; connect router; apply firewall rules.

    Note: NOT ``--internal``. Docker 29 silently drops ``-p`` port publishing on
    internal networks, which breaks SSH. Egress enforcement is provided by the
    router's iptables FORWARD rules (keyed on source subnet); we inject a
    default route via the router after container start so all egress traverses
    those rules.
    """
    network = project_network_for(project)
    if not network_exists(network):
        run_check(["docker", "network", "create", network])
    run(["docker", "network", "connect", network, ROUTER_CONTAINER],
        capture_output=True)
    router_ip = get_router_ip(network)
    apply_firewall_rules(network, mode)
    return network, router_ip


def remove_project_network(project: str) -> None:
    network = project_network_for(project)
    remove_firewall_rules(network)
    # Disconnect every container research.py knows might be attached. The webui
    # (if running) was wired in by `wire_webui_to_projects()` and the registry
    # (if the project ever enabled an extension) by
    # `_connect_registry_to_project_network()`; without an explicit disconnect,
    # `network rm` fails with "endpoints remain". All calls are idempotent — they
    # exit non-zero silently when the container isn't on this network.
    for svc in (ROUTER_CONTAINER, WEBUI_CONTAINER, REGISTRY_CONTAINER):
        run(["docker", "network", "disconnect", network, svc],
            capture_output=True)
    run(["docker", "network", "rm", network], capture_output=True)


# ---------------------------------------------------------------------------
# docker run argv builder
# ---------------------------------------------------------------------------


def build_supervisor_docker_args(
    *,
    container_name: str,
    project: str,
    network: str,
    workspace_path: Path,
    ssh_port: int,
    ssh_pass: str,
    dns_servers: list[str],
    memory: str,
    cpus: str,
    image: str,
    dind_mode: str,
    inner_firewall: bool = False,
    project_type: str = PROJECT_TYPE_RESEARCH,
    substrate: str = Substrate.DIND_SYSBOX.value,
    service_flags: dict[str, bool] | None = None,
) -> list[str]:
    is_docker = substrate == Substrate.DOCKER.value
    args = [
        "run", "-d",
        "--name", container_name,
        "--hostname", project,
        "--network", network,
        "--add-host", "host.docker.internal:host-gateway",
        "-v", f"{workspace_path}:/workspace",
        "-p", f"{ssh_port}:22",
        "-e", f"PROJECT={project}",
        "-e", f"SSH_PASSWORD={ssh_pass}",
        "-e", f"HOST_GID={os.getgid()}",
        "--label", f"{PROJECT_LABEL}={project}",
        "--label", f"{DIND_MODE_LABEL}={dind_mode}",
        # Flavor marker for the host (_container_project_type / recreate
        # metadata) and the webui. The per-flavor *image* (selected by the
        # caller's `image=`) is what actually differs at runtime; the entrypoints
        # no longer branch on a flavor env.
        "--label", f"{PROJECT_TYPE_LABEL}={project_type}",
        # Containment substrate marker — read back by start()/recreate to pick
        # plain docker start vs the sysbox recreate dance. Legacy containers
        # without it default to dind-sysbox (_container_substrate).
        "--label", f"{SUBSTRATE_LABEL}={substrate}",
    ]
    # The docker substrate has no inner dockerd — no DIND boot, no runtime flag.
    if not is_docker:
        args += ["-e", "DOCKER_DIND=true"]
    if inner_firewall:
        args += ["-e", "RS_INNER_FIREWALL=1"]
    # Per-service flags: webui reads the labels (outside-the-container truth);
    # entrypoint reads the env vars (inside-the-container truth). Both must
    # land on the same container in lockstep. The env var name normalizes '-' to
    # '_' (`code-server` → RS_SERVICE_CODE_SERVER): a hyphen is shell-unreadable
    # (`${RS_SERVICE_CODE-SERVER}` parses as `$RS_SERVICE_CODE` minus `SERVICE`),
    # and every reader (the entrypoints, rs_sandbox's os.environ lookup) uses the
    # underscore form. The LABEL keeps the canonical hyphenated id.
    flags = service_flags if service_flags is not None else {sid: True for sid in KNOWN_SERVICES}
    for sid in sorted(flags):
        ena = "enabled" if flags[sid] else "disabled"
        env_name = "RS_SERVICE_" + sid.upper().replace("-", "_")
        args += ["--label", f"{SERVICE_LABEL_PREFIX}{sid}={ena}"]
        args += ["-e", f"{env_name}={ena}"]
    # code-server lazy-reap idle window. Optional — entrypoint defaults to
    # 1800s (30 min) when unset; .env can override per-host. Survives
    # _recreate_supervisor by being re-passed from the host's env on every
    # create, which is what we want (a host-side tweak should propagate to
    # the next project lifecycle, not require per-project state).
    idle = os.environ.get("CODE_SERVER_IDLE_SECONDS")
    if idle:
        args += ["-e", f"CODE_SERVER_IDLE_SECONDS={idle}"]
    for s in dns_servers:
        args += ["--dns", s]

    if dind_mode == "sysbox":
        args += ["--runtime=sysbox-runc", "--pids-limit=4096"]
    elif dind_mode == "privileged":
        args += ["--privileged", "--pids-limit=4096",
                 "-v", f"{docker_volume_name_for(project)}:/var/lib/docker"]

    if memory:
        args += [f"--memory={memory}"]
    if cpus:
        args += [f"--cpus={cpus}"]
    args.append(image)
    return args


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------


def _preflight() -> None:
    """Sanity checks + idempotent one-time bootstrap (.env, images)."""
    if not shutil.which("docker"):
        die("docker not found on PATH. Install Docker Engine first.")
    r = run(["docker", "info"], capture_output=True)
    if r.returncode != 0:
        die("docker daemon is not reachable. Start it and try again.")

    env_path = SCRIPT_DIR / ".env"
    if not env_path.exists():
        example = SCRIPT_DIR / ".env.example"
        if example.exists():
            env_path.write_text(example.read_text())
            print(f"created {env_path.name} (copied from .env.example)")
        else:
            env_path.write_text("")
            print(f"created empty {env_path.name}")


def load_versions() -> dict[str, str]:
    """Parse the root-level versions.env (KEY=VALUE, `#`-comment lines) into a
    dict of image-version pins. Mirrors load_config()'s .env parsing — stdlib
    only, no quote/inline-comment handling (pins are bare tokens). Missing file
    yields {} so every Dockerfile ARG default still applies."""
    pins: dict[str, str] = {}
    if not VERSIONS_FILE.exists():
        return pins
    for line in VERSIONS_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip()
        if k:
            pins[k] = v
    return pins


# ---------------------------------------------------------------------------
# Agent dist — host-cached, version-pinned agent software copied into a box at
# boot (STAGE_AGENT_DIST_S1). The image stays agent-less; the entrypoint deploys
# the agent via `cp` from a RO mount of this cache. Built INSIDE a throwaway
# rs-minimal-base container (no host ~/.local pollution, no host-tool dependency).
# ---------------------------------------------------------------------------

AGENT_DIST_DIR = Path.home() / ".research-sandbox" / "agent-dist"

# Where the dist is staged (supervisor: real files) / RO-mounted (every container
# that deploys an agent) — the single in-container copy-source path. The fleet
# agent the supervisor + worker homes deploy (no bake; STAGE_AGENT_DIST slice 2).
AGENT_DIST_MOUNT = "/opt/agent-dist"
DEFAULT_AGENT = "claude"
# RO copy-source mount spliced into every inner `docker run` (the source is the
# supervisor's staged dir — same path, RO). The entrypoint cp's its own copy.
# Spliced UNCONDITIONALLY at the rscore spawn sites (role-MCP / PI / pi-isolated):
# the dind floor guarantees the dist is staged, and even on a missing source an
# inner -v auto-creates an inert empty dir (the entrypoint absence-guard no-ops).
# rs_worker.py / rs_sandbox.py guard with os.path.isdir instead — a DELIBERATE
# asymmetry (those modules run in-supervisor and prefer a clear "claude not found"
# over a cryptic mount error), not an oversight; don't "harmonize" it away.
AGENT_DIST_MOUNT_ARGS = ["-v", f"{AGENT_DIST_MOUNT}:{AGENT_DIST_MOUNT}:ro"]

# Per-agent install recipe. version_key names the versions.env pin — the host
# dist pin (the bake is gone, STAGE_AGENT_DIST slice 2; no Dockerfile reads it
# anymore), so `agent refresh` writing it bumps what every container deploys via
# `cp` at boot. `install` runs IN rs-minimal-base as the `research` user. Adding
# codex/goose later is one entry here (the N+M seam — but each needs its own
# cross-user launcher spike before its relink is trusted; see _relativize_launcher).
# Where an agent's companion VS Code extension .vsix is tucked inside the
# captured ~/.local (STAGE_AGENT_EXTENSIONS, "B-tuck"): it rides every existing
# cp into a container's ~/.local for free, so its presence there is exactly the
# signal "this agent was deployed here" — the install gate falls out of file
# presence, no launcher check. code-server-deploy.sh step 3b globs this dir.
AGENT_EXT_SUBDIR = "share/rs-agent-ext"

_AGENT_INSTALL = {
    "claude": {
        "version_key": "CLAUDE_CODE_VERSION",
        "install": "curl -fsSL https://claude.ai/install.sh | bash -s -- {ver}",
        "bin": "claude",
        # Upstream version-resolve endpoint (verified against bootstrap.sh: the
        # installer curls this to turn "latest" into a concrete version; the body
        # is a bare semver string). `agent refresh` fetches just this — no install.
        "latest_url": "https://downloads.claude.ai/claude-code-releases/latest",
        # OPTIONAL companion editor extension (agent-bound; STAGE_AGENT_EXTENSIONS).
        # A future agent with no extension omits this whole key → no .vsix, no-op.
        # version_key is an INDEPENDENT versions.env pin (the CLI + extension move
        # together upstream but are bumped separately). The url is Open VSX,
        # platform-pinned linux-x64 (the extension is platform-specific; our
        # containers are x86_64 linux). file = the saved basename, chosen as the
        # extension id so code-server-deploy.sh's skip-glob (*<base>*) precisely
        # matches the installed folder anthropic.claude-code-<ver>.
        "ext": {
            "id": "Anthropic.claude-code",
            "version_key": "CLAUDE_CODE_EXT_VERSION",
            "url": ("https://open-vsx.org/api/Anthropic/claude-code/linux-x64/"
                    "{ver}/file/Anthropic.claude-code-{ver}@linux-x64.vsix"),
            "file": "anthropic.claude-code.vsix",
        },
    },
}
KNOWN_AGENTS = tuple(_AGENT_INSTALL)   # the --agent enum

# A version token safe to interpolate into the in-container install shell — a
# charset guard (defense-in-depth against a crafted versions.env), not a range.
_AGENT_VERSION_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]*\Z")

# The dist build is a NETWORK download (install.sh fetches the agent binary), so a
# transient failure is possible; each attempt is a fresh --rm container and the
# install is idempotent, so retrying is safe. 3 attempts makes a single transient
# unlikely to fail the build while bounding wasted time at ~3× a failed download.
_AGENT_BUILD_ATTEMPTS = 3
# Tail of the failed-build output surfaced in the die message — enough to see the
# actual install error without dumping the whole (large) download log.
_AGENT_ERR_TAIL = 2000

# Canonical agent settings.json bundled into the dist (STAGE_AGENT_DIST_SETTINGS):
# bypassPermissions + dark theme, deliberately NO `hooks` key. Deployed to
# ~/.claude/settings.json NO-CLOBBER at every dist-deploy site, so any surface that
# already has its own settings keeps it — chiefly the research supervisor, whose
# setup.sh writes the SAME content PLUS the rs-audit-stop Stop hook (that hook must
# never be clobbered; setup.sh is boot-ordered ahead of the staging to win the
# race). Fresh surfaces (docker box, sandbox-dind) with no settings get bypass.
# The no-hooks shape is load-bearing for inner surfaces — a `hooks` key there is a
# `command not found` on every Stop event. Mirror of container/supervisor/setup.sh
# MINUS hooks; keep the two in sync.
_AGENT_SETTINGS_JSON = json.dumps(
    {"permissions": {"defaultMode": "bypassPermissions"}, "theme": "dark"},
    indent=2) + "\n"


# ---------------------------------------------------------------------------
# Editor dist — host-cached, version-pinned code-server copied into an
# INTERACTIVE container at boot (STAGE_EDITOR_DIST slice 1). The PI/worker
# lineage (rs-pi-base / rs-pi-isolated / rs-sandbox-box) has NO baked editor;
# this delivers it the same way the agent dist delivers claude. The minimal
# lineage keeps its bake in slice 1 (the coexistence guard skips the dist cp
# there); slice 2 deletes the bake and flips it to the dist.
# ---------------------------------------------------------------------------

EDITOR_DIST_DIR = Path.home() / ".research-sandbox" / "editor-dist"
# Staged (supervisor: real files) / RO-mounted (interactive inner containers).
EDITOR_DIST_MOUNT = "/opt/editor-dist"
EDITOR_DIST_MOUNT_ARGS = ["-v", f"{EDITOR_DIST_MOUNT}:{EDITOR_DIST_MOUNT}:ro"]
_CODE_SERVER_BIN = "code-server"
_CODE_SERVER_VERSION_KEY = "CODE_SERVER_VERSION"
_DATA_WRANGLER_VERSION_KEY = "DATA_WRANGLER_VERSION"

# Tier-2 extension prune — MUST mirror agent/Dockerfile.minimal-base's strip list
# until slice 2 deletes the bake (the dist and the bake should ship the same
# editor). Keep grammar/themes/markdown/notebook; drop heavy language-servers,
# the git stack, and JS build/debug tooling.
_CODE_SERVER_STRIP_EXTS = (
    "typescript-language-features", "html-language-features",
    "css-language-features", "json-language-features", "php-language-features",
    "git", "git-base", "github", "github-authentication",
    "microsoft-authentication", "merge-conflict", "npm", "grunt", "gulp",
    "jake", "node-debug", "node-debug2", "debug-auto-launch",
    "debug-server-ready", "references-view", "extension-editing",
    "simple-browser",
)
# Datawrangler .vsix (MS-marketplace-only) — downloaded fresh in-container at
# build so the dist is self-contained (doesn't depend on the bake surviving
# slice 2). Version from versions.env (DATA_WRANGLER_VERSION).
_DATA_WRANGLER_VSIX_URL = (
    "https://marketplace.visualstudio.com/_apis/public/gallery/publishers/"
    "ms-toolsai/vsextensions/datawrangler/{ver}/vspackage")
# GitHub "latest" redirects to /releases/tag/v<ver>; reading the effective URL
# resolves the upstream version with curl alone (no in-container JSON parse).
_CODE_SERVER_LATEST_URL = "https://github.com/coder/code-server/releases/latest"


def agent_dist_path(agent: str) -> Path:
    return AGENT_DIST_DIR / agent


def _agent_sidecar(agent: str) -> Path:
    return AGENT_DIST_DIR / f"{agent}.json"


def dist_present(agent: str) -> bool:
    """True iff a usable dist for `agent` is cached (its launcher entry exists).
    lexists, not exists: the launcher is a symlink to an absolute ~/.local/share
    path that is dangling on the host but resolves inside a box."""
    spec = _AGENT_INSTALL.get(agent)
    return bool(spec) and os.path.lexists(
        agent_dist_path(agent) / "local" / "bin" / spec["bin"])


def _relativize_launcher(launcher: Path) -> None:
    """Rewrite the dist's launcher symlink to be $HOME-agnostic (STAGE_AGENT_DIST
    slice 2). The installer writes ~/.local/bin/<agent> as an ABSOLUTE symlink into
    /home/research/.local/share/...; dangling once the tree is cp'd into a worker
    home (/home/worker), so claude won't run there (spike: exit 127). Rewritten to a
    relative ../share/... target it resolves under ANY user's $HOME, so one dist
    serves the research box AND the worker fleet (spike phase B: exit 0). The spike
    also confirmed the launcher is the ONLY /home/research coupling in claude's
    payload — this relink is the whole fix. Defensive: only rewrite the expected
    absolute-into-/.local/ symlink; anything else is left untouched with a warning
    (a layout change / a future agent needs its own cross-user spike before its
    relink is trusted — do not assume the property generalizes)."""
    if not os.path.islink(launcher):
        print(f"warning: {launcher.name} is not a symlink — dist may be "
              f"non-portable across users (boot may fail in worker homes)",
              file=sys.stderr)
        return
    target = os.readlink(launcher)
    if not (target.startswith("/") and "/.local/" in target):
        print(f"warning: {launcher.name} -> {target} is not the expected absolute "
              f"/.local/ symlink — left as-is (dist may be non-portable)",
              file=sys.stderr)
        return
    suffix = target.split("/.local/", 1)[1]   # share/<agent>/versions/<ver>
    launcher.unlink()
    launcher.symlink_to(os.path.join("..", suffix))   # ../share/... from .local/bin/


def _agent_build_dist(agent: str, version: str) -> None:
    """Build agent@version IN a throwaway rs-minimal-base container and swap the
    captured ~/.local tree into the cache, owned by the operator. Host-side
    `docker run` (host network → the one download); never pollutes the host's
    own ~/.local; no host-tool dependency (curl/install run in the image)."""
    spec = _AGENT_INSTALL[agent]
    if not _AGENT_VERSION_RE.match(version):
        die(f"refusing to build {agent!r} with suspicious version {version!r}")
    if not run_quiet(["docker", "image", "inspect", MINIMAL_BASE_IMAGE]):
        die(f"{MINIMAL_BASE_IMAGE} not found — run `research start --rebuild` first")
    # Companion editor extension (STAGE_AGENT_EXTENSIONS, B-tuck). Resolved here —
    # not threaded from the caller — so BOTH `agent pull` AND a CLI-version
    # `agent refresh` re-bundle the .vsix at the CURRENT ext pin (no CLI↔ext skew).
    # die loud on an unpinned ext version rather than ship an extension-less dist
    # (wired-but-absent is the worst failure shape); charset-guard before the URL
    # interpolation reaches an in-container shell (same defense as the CLI version).
    ext = spec.get("ext")
    ext_dl = ""
    if ext:
        ext_ver = load_versions().get(ext["version_key"])
        if not ext_ver:
            die(f"agent {agent!r} declares a companion extension but its pin "
                f"{ext['version_key']} is unset in versions.env")
        if not _AGENT_VERSION_RE.match(ext_ver):
            die(f"refusing to fetch {agent!r} extension with suspicious "
                f"version {ext_ver!r}")
        ext_url = ext["url"].format(ver=ext_ver)
        ext_dl = (f" && mkdir -p ~/.local/{AGENT_EXT_SUBDIR}"
                  f" && curl -fsSL --compressed -o "
                  f"~/.local/{AGENT_EXT_SUBDIR}/{ext['file']} {shlex.quote(ext_url)}")
    AGENT_DIST_DIR.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(dir=str(AGENT_DIST_DIR)))   # same fs ⇒ cheap rename
    try:
        bin_ = spec["bin"]
        # pipefail + an in-container `test -x` so a failed `curl | bash` surfaces
        # the installer's stderr via run_check instead of silently producing an
        # empty tree (curl|bash masks curl's exit without pipefail). The launcher
        # is verified IN the container, where an absolute ~/.local/share symlink
        # resolves. Root container: su to research for the install, then chown the
        # captured tree to the operator (so a non-1000 host owns its cache).
        inner = ("set -e; set -o pipefail; "
                 + spec["install"].format(ver=version)
                 + f" && test -x ~/.local/bin/{bin_}"
                 + ext_dl                       # tuck the .vsix into ~/.local BEFORE the capture
                 + " && cp -a ~/.local /out/.local")
        script = (f"set -e; su - research -c {shlex.quote(inner)}; "
                  f"chown -R {os.getuid()}:{os.getgid()} /out")
        captured = tmp / ".local"
        # lexists, NOT exists: the launcher is a symlink to an absolute
        # ~/.local/share path — dangling on the host, but it resolves once a box
        # copies the whole tree into its own /home/research/.local.
        built, last_err = False, ""
        for _ in range(_AGENT_BUILD_ATTEMPTS):
            r = run(["docker", "run", "--rm", "-v", f"{tmp}:/out",
                     MINIMAL_BASE_IMAGE, "sh", "-lc", script], capture_output=True)
            if (r.returncode == 0 and os.path.lexists(captured / "bin" / bin_)
                    and (not ext
                         or (captured / AGENT_EXT_SUBDIR / ext["file"]).is_file())):
                built = True
                break
            last_err = ((r.stderr or "") + (r.stdout or "")).strip()
            shutil.rmtree(captured, ignore_errors=True)   # clear a partial /out
        if not built:
            die(f"agent {agent} build failed after {_AGENT_BUILD_ATTEMPTS} "
                f"attempts (version {version}):\n{last_err[-_AGENT_ERR_TAIL:] or 'no output'}")
        _relativize_launcher(captured / "bin" / bin_)
        dest = agent_dist_path(agent)
        if dest.exists():
            shutil.rmtree(dest)
        # Fixed tree (STAGE_AGENT_DIST_SETTINGS): local/ = the captured ~/.local,
        # claude/settings.json = the bundled bypass settings (no hooks). Mirrors the
        # editor dist's fixed-tree shape. captured is tmp/.local, same fs as dest.
        dest.mkdir(parents=True)
        os.replace(captured, dest / "local")
        (dest / "claude").mkdir()
        (dest / "claude" / "settings.json").write_text(_AGENT_SETTINGS_JSON)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    _agent_sidecar(agent).write_text(json.dumps(
        {"agent": agent, "version": version,
         "pulled_at": datetime.datetime.now(datetime.timezone.utc).isoformat()},
        indent=2) + "\n")


def agent_pull(agent: str = "claude", version: str | None = None) -> dict:
    """Pull agent@(version or the versions.env pin) into the host cache."""
    if agent not in _AGENT_INSTALL:
        die(f"unknown agent {agent!r} (known: {', '.join(KNOWN_AGENTS)})")
    ver = version or load_versions().get(_AGENT_INSTALL[agent]["version_key"])
    if not ver:
        die(f"no pinned version for {agent!r} in versions.env "
            f"({_AGENT_INSTALL[agent]['version_key']})")
    _agent_build_dist(agent, ver)
    return {"agent": agent, "version": ver, "path": str(agent_dist_path(agent))}


def _stage_agent_dist(supervisor: str, agent: str = DEFAULT_AGENT,
                      *, deploy_local: bool = True) -> None:
    """Stage the host agent dist into a RUNNING supervisor (STAGE_AGENT_DIST slice
    2). Real files at AGENT_DIST_MOUNT — the inner fleet (worker / role-MCP / pi /
    pi-isolated / sandbox-box) RO-mounts that path and cp's its own writable copy
    at boot. `deploy_local` ALSO (re)deploys the supervisor's OWN ~/.local from the
    dist — True for the research flavor (the PI's interactive claude + the
    rs-audit-stop hook live there); False for the rs-sandbox-dind (sandbox-dind) flavor,
    whose supervisor never runs claude itself but DOES stage the dist so its
    rs-sandbox-box boxes (FROM rs-analysis-base — no bake now) can deploy it.

    Two dragons handled here:
      • docker-cp-into-existing-dir NESTS (src copied INTO dest → .../claude/bin),
        silently breaking the mount path on the update-agent / recreate re-stage.
        So rm the dest first, then `docker cp <cache>/.` to land contents directly.
      • The supervisor's ~/.local deploy is UNCONDITIONAL (not absence-guarded) so
        `update-agent` actually refreshes the launcher the PI's interactive claude
        uses; a version bump leaves the old versions/<oldver>/ as harmless dead
        weight (the relinked launcher points at the new one).
    Runs as root (-u 0) to write /opt + /home; chowns to uid:gid 1000:1000
    numerically (the documented both-leaf uid) so it's user-name-agnostic; absolute
    /home/research, not ~ (cross-boundary-path rule)."""
    src = agent_dist_path(agent)
    if not dist_present(agent):
        die(f"no cached {agent} dist to stage — run `research agent pull` first")
    # SYSBOX UID SHIFT: a plain `docker cp` carries the HOST uid/gid of the cache
    # files; inside the sysbox supervisor those land as a foreign (unmapped) owner
    # that container-root can neither chown NOR rm NOR overwrite (EPERM — even a
    # leftover tarball in sticky /tmp can't be unlinked or re-cp'd over), and an
    # inner `cp -a` can't preserve it either. So never let a foreign uid in AND
    # never leave an intermediate file: STREAM a uid/gid-0-normalized tar straight
    # into the container's `tar -x` via stdin. The extracted tree is root-owned
    # (in-range) → chown→1000 works, as does a future re-stage's `rm -rf`. No host
    # temp file, no `docker cp`, no in-container leftover. (No host `tar` either —
    # tarfile is Python stdlib; stdout→DEVNULL so the 150MB stdin write can't
    # deadlock on backpressure, the quiet extract keeps stderr tiny.)
    def _root_owned(ti: tarfile.TarInfo) -> tarfile.TarInfo:
        ti.uid = ti.gid = 0
        ti.uname = ti.gname = ""
        return ti
    extract = (f"rm -rf {AGENT_DIST_MOUNT} && mkdir -p {AGENT_DIST_MOUNT} && "
               f"tar -C {AGENT_DIST_MOUNT} -x && chown -R 1000:1000 {AGENT_DIST_MOUNT}")
    proc = subprocess.Popen(
        ["docker", "exec", "-i", "-u", "0", supervisor, "sh", "-c", extract],
        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        with tarfile.open(fileobj=proc.stdin, mode="w|") as tf:   # streaming; symlinks preserved
            for entry in sorted(os.listdir(src)):
                tf.add(os.path.join(src, entry), arcname=entry, filter=_root_owned)
    finally:
        if proc.stdin:
            proc.stdin.close()
    _, err = proc.communicate()
    if proc.returncode != 0:
        detail = (err.decode(errors="replace") if err else "").strip()
        die(f"staging {agent} dist into {supervisor} failed: "
            f"{detail or 'tar extract returned non-zero'}")
    if deploy_local:
        # /opt/agent-dist is now research-owned, so cp -a preserves cleanly. -f
        # (force) is load-bearing for the update-agent RE-deploy: the existing
        # ~/.local/bin/claude is a symlink, and plain `cp -a` can't overwrite it
        # ("File exists") — -f unlinks + recreates. chown is belt-and-suspenders.
        # The ~/.local cp is forced (update-agent re-deploys the launcher symlink);
        # the settings.json install is NO-CLOBBER so a baked/propagated settings —
        # chiefly the research supervisor's hook-bearing one from setup.sh, which is
        # boot-ordered to land first — is preserved (STAGE_AGENT_DIST_SETTINGS).
        run_check(["docker", "exec", "-u", "0", supervisor, "sh", "-c",
                   f"mkdir -p /home/research/.local /home/research/.claude && "
                   f"cp -af {AGENT_DIST_MOUNT}/local/. /home/research/.local/ && "
                   f"( [ -e /home/research/.claude/settings.json ] || "
                   f"cp {AGENT_DIST_MOUNT}/claude/settings.json "
                   f"/home/research/.claude/settings.json ) && "
                   f"chown -R 1000:1000 /home/research/.local /home/research/.claude"])


def _stage_rs_sandbox(container: str) -> None:
    """Stage the rs-sandbox box-management CLI into a RUNNING dind supervisor
    at /usr/local/bin/rs-sandbox (STAGE_SANDBOX_DIND_AGENT). It is no longer baked
    into the image — the box harness is a standing dind utility (STAGE_DIND_UNIFY):
    delivered eagerly at create/recreate for sandbox-dind, and lazily on first
    box_add for research (the image carries no copy). A single stdlib file: stream it
    over `docker exec -i` stdin into `cat`, owned root + chmod 755
    (it lives in /usr/local/bin and is run by the research user) — exactly what the
    old `COPY … /usr/local/bin/rs-sandbox` bake produced, just at runtime."""
    src = Path(__file__).resolve().parent / "rs_sandbox.py"
    if not src.is_file():
        die(f"rs_sandbox.py not found at {src} — cannot stage the box harness")
    install = ("cat > /usr/local/bin/rs-sandbox && chmod 755 /usr/local/bin/rs-sandbox")
    proc = subprocess.Popen(
        ["docker", "exec", "-i", "-u", "0", container, "sh", "-c", install],
        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    _, err = proc.communicate(input=src.read_bytes())
    if proc.returncode != 0:
        detail = (err.decode(errors="replace") if err else "").strip()
        die(f"staging rs-sandbox into {container} failed: "
            f"{detail or 'cat returned non-zero'}")


def _agent_resolve_latest(agent: str) -> str:
    """LIGHT in-container fetch of upstream `latest` → a concrete version string
    (no install). Honors the no-host-tool rule (curl runs in rs-minimal-base)."""
    spec = _AGENT_INSTALL[agent]
    if not run_quiet(["docker", "image", "inspect", MINIMAL_BASE_IMAGE]):
        die(f"{MINIMAL_BASE_IMAGE} not found — run `research start --rebuild` first")
    r = run(["docker", "run", "--rm", MINIMAL_BASE_IMAGE, "sh", "-lc",
             f"curl -fsSL {shlex.quote(spec['latest_url'])}"], capture_output=True)
    if r.returncode != 0:
        die(f"could not resolve upstream {agent} version: "
            f"{(r.stderr or '').strip() or 'fetch failed'}")
    ver = (r.stdout or "").strip()
    if not _AGENT_VERSION_RE.match(ver):
        die(f"upstream returned an unexpected version string: {ver!r}")
    return ver


def agent_refresh_check(agent: str = "claude") -> tuple[str, str]:
    """Side-effect-free: return (current pin from versions.env, upstream latest)."""
    if agent not in _AGENT_INSTALL:
        die(f"unknown agent {agent!r} (known: {', '.join(KNOWN_AGENTS)})")
    current = load_versions().get(_AGENT_INSTALL[agent]["version_key"], "")
    return current, _agent_resolve_latest(agent)


def _set_version_pin(key: str, value: str) -> None:
    """Rewrite KEY=… in versions.env in place — the SANCTIONED `agent refresh`
    write (the file's header sanctions this one bespoke writer)."""
    lines = VERSIONS_FILE.read_text().splitlines()
    out, found = [], False
    for ln in lines:
        s = ln.strip()
        if s and not s.startswith("#") and s.split("=", 1)[0].strip() == key:
            out.append(f"{key}={value}")
            found = True
        else:
            out.append(ln)
    if not found:
        out.append(f"{key}={value}")
    VERSIONS_FILE.write_text("\n".join(out) + "\n")


def agent_apply_refresh(agent: str, version: str) -> None:
    """Bump versions.env's pin to `version` AND (re)build the dist at it. The
    prompt/confirm is the front-end's job (this just applies)."""
    _set_version_pin(_AGENT_INSTALL[agent]["version_key"], version)
    _agent_build_dist(agent, version)


def agent_list() -> list[dict]:
    """Cached agents (read sidecars); empty if none pulled yet."""
    out: list[dict] = []
    for agent in KNOWN_AGENTS:
        if dist_present(agent) and _agent_sidecar(agent).exists():
            try:
                out.append(json.loads(_agent_sidecar(agent).read_text()))
            except Exception:
                out.append({"agent": agent, "version": "?", "pulled_at": "?"})
    return out


# ---- editor dist (code-server) — the service twin of the agent dist --------

def _editor_sidecar() -> Path:
    return EDITOR_DIST_DIR.parent / "editor-dist.json"


def editor_dist_present() -> bool:
    """True iff a usable editor dist is cached. lexists, not exists: the launcher
    is a relativized symlink that dangles on the host but resolves in a box."""
    return os.path.lexists(EDITOR_DIST_DIR / ".local" / "bin" / _CODE_SERVER_BIN)


def _editor_build_dist(cs_version: str, dw_version: str) -> None:
    """Build the code-server editor dist IN a throwaway rs-minimal-base container
    and swap it into the host cache (STAGE_EDITOR_DIST). Unlike the agent dist (a
    bare ~/.local capture), the editor dist is a fixed tree:
        .local/                              -- `--method standalone` install (Tier-2 stripped)
        tools/code-server-stub.py            -- the lazy-start stub
        templates/User/settings.json         -- code-server user settings
        templates/extensions/datawrangler.vsix
    The install + the .vsix download run in-container (network); the stub +
    settings are repo files copied in host-side (so the build is self-contained
    and survives slice 2's bake deletion). `--method standalone` (NOT the deb
    method the bake uses — that lands in /usr/bin, leaving ~/.local empty) roots
    the tree in ~/.local so it cp-deploys like the agent dist; the launcher is
    relativized for cross-user portability (the spike proved it suffices)."""
    for v in (cs_version, dw_version):
        if not _AGENT_VERSION_RE.match(v):
            die(f"refusing to build editor dist with suspicious version {v!r}")
    if not run_quiet(["docker", "image", "inspect", MINIMAL_BASE_IMAGE]):
        die(f"{MINIMAL_BASE_IMAGE} not found — run `research start --rebuild` first")
    sup_dir = SCRIPT_DIR / "container" / "supervisor"
    stub_src = sup_dir / "code-server-stub.py"
    deploy_src = sup_dir / "code-server-deploy.sh"
    settings_src = sup_dir / "code-server-settings.json"
    for p in (stub_src, deploy_src, settings_src):
        if not p.is_file():
            die(f"editor dist build: missing repo file {p}")
    EDITOR_DIST_DIR.parent.mkdir(parents=True, exist_ok=True)
    tmp: Path | None = Path(tempfile.mkdtemp(dir=str(EDITOR_DIST_DIR.parent)))
    try:
        # Strip the Tier-2 extensions in the standalone install's extensions dir
        # (|| true so a missing extension can't abort under set -e).
        strip = " ".join(f'rm -rf "$EXT_DIR/{e}" || true;' for e in _CODE_SERVER_STRIP_EXTS)
        vsix_url = _DATA_WRANGLER_VSIX_URL.format(ver=dw_version)
        # set -e + pipefail (mirror _agent_build_dist) so a `curl | sh` failure or a
        # missing extensions dir aborts with a NAMED error instead of silently
        # producing an empty capture that fails opaquely after the retries.
        inner = (
            "set -e; set -o pipefail; "
            "curl -fsSL https://code-server.dev/install.sh "
            f"| sh -s -- --method standalone --version {cs_version}; "
            f"test -x ~/.local/bin/{_CODE_SERVER_BIN}; "
            'EXT_DIR="$(find ~/.local/lib -maxdepth 6 -type d -name extensions | head -n1)"; '
            '[ -n "$EXT_DIR" ] || { echo "code-server extensions dir not found" >&2; exit 1; }; '
            f"{strip} "
            "cp -a ~/.local /out/.local; "
            f"curl -fsSL --compressed -o /out/datawrangler.vsix {shlex.quote(vsix_url)}")
        script = (f"set -e; set -o pipefail; su - research -c {shlex.quote(inner)}; "
                  f"chown -R {os.getuid()}:{os.getgid()} /out")
        captured_local = tmp / ".local"
        vsix_tmp = tmp / "datawrangler.vsix"
        built, last_err = False, ""
        for _ in range(_AGENT_BUILD_ATTEMPTS):
            r = run(["docker", "run", "--rm", "-v", f"{tmp}:/out",
                     MINIMAL_BASE_IMAGE, "sh", "-lc", script], capture_output=True)
            if (r.returncode == 0
                    and os.path.lexists(captured_local / "bin" / _CODE_SERVER_BIN)
                    and vsix_tmp.is_file()):
                built = True
                break
            last_err = ((r.stderr or "") + (r.stdout or "")).strip()
            shutil.rmtree(captured_local, ignore_errors=True)
            vsix_tmp.unlink(missing_ok=True)
        if not built:
            die(f"editor dist build failed after {_AGENT_BUILD_ATTEMPTS} attempts:\n"
                f"{last_err[-_AGENT_ERR_TAIL:] or 'no output'}")
        # Assemble the rest of the tree host-side: the stub + settings are repo
        # files; the .vsix moves out of the build-output root into templates/.
        (tmp / "tools").mkdir()
        for src in (stub_src, deploy_src):
            dst = tmp / "tools" / src.name
            shutil.copy2(src, dst)
            os.chmod(dst, 0o755)   # entrypoint runs the stub + sources the deploy
        (tmp / "templates" / "User").mkdir(parents=True)
        shutil.copy2(settings_src, tmp / "templates" / "User" / "settings.json")
        (tmp / "templates" / "extensions").mkdir(parents=True)
        os.replace(vsix_tmp, tmp / "templates" / "extensions" / "datawrangler.vsix")
        _relativize_launcher(captured_local / "bin" / _CODE_SERVER_BIN)
        if EDITOR_DIST_DIR.exists():
            shutil.rmtree(EDITOR_DIST_DIR)
        os.replace(tmp, EDITOR_DIST_DIR)   # same fs (mkdtemp under the parent)
        tmp = None                         # moved into place; skip the finally rmtree
    finally:
        if tmp is not None:
            shutil.rmtree(tmp, ignore_errors=True)
    _editor_sidecar().write_text(json.dumps(
        {"code_server_version": cs_version, "data_wrangler_version": dw_version,
         "pulled_at": datetime.datetime.now(datetime.timezone.utc).isoformat()},
        indent=2) + "\n")


def editor_pull(cs_version: str | None = None,
                dw_version: str | None = None) -> dict:
    """Pull the editor dist at (versions or the versions.env pins) into the cache."""
    v = load_versions()
    cs = cs_version or v.get(_CODE_SERVER_VERSION_KEY)
    dw = dw_version or v.get(_DATA_WRANGLER_VERSION_KEY)
    if not cs:
        die(f"no pinned {_CODE_SERVER_VERSION_KEY} in versions.env")
    if not dw:
        die(f"no pinned {_DATA_WRANGLER_VERSION_KEY} in versions.env")
    _editor_build_dist(cs, dw)
    return {"code_server_version": cs, "data_wrangler_version": dw,
            "path": str(EDITOR_DIST_DIR)}


def editor_show() -> dict:
    """The cached editor dist's sidecar, or {} if none pulled yet."""
    if not editor_dist_present():
        return {}
    try:
        return json.loads(_editor_sidecar().read_text())
    except Exception:
        return {"code_server_version": "?", "data_wrangler_version": "?"}


def _editor_resolve_latest() -> str:
    """LIGHT resolve of code-server's upstream `latest` → a concrete version (no
    install). GitHub's /releases/latest redirects to /releases/tag/v<ver>; read
    the effective URL with curl alone (no in-container JSON parse). Honors the
    no-host-tool rule (curl runs in rs-minimal-base)."""
    if not run_quiet(["docker", "image", "inspect", MINIMAL_BASE_IMAGE]):
        die(f"{MINIMAL_BASE_IMAGE} not found — run `research start --rebuild` first")
    r = run(["docker", "run", "--rm", MINIMAL_BASE_IMAGE, "sh", "-lc",
             "curl -fsS -o /dev/null -w '%{url_effective}' "
             + shlex.quote(_CODE_SERVER_LATEST_URL)], capture_output=True)
    if r.returncode != 0:
        die(f"could not resolve upstream code-server version: "
            f"{(r.stderr or '').strip() or 'fetch failed'}")
    eff = (r.stdout or "").strip()                 # …/releases/tag/v4.123.0
    ver = eff.rsplit("/", 1)[-1].lstrip("v")
    if not _AGENT_VERSION_RE.match(ver):
        die(f"upstream returned an unexpected version string: {ver!r} (from {eff!r})")
    return ver


def editor_refresh_check() -> tuple[str, str]:
    """Side-effect-free: (current CODE_SERVER_VERSION pin, upstream latest)."""
    current = load_versions().get(_CODE_SERVER_VERSION_KEY, "")
    return current, _editor_resolve_latest()


def editor_apply_refresh(cs_version: str) -> None:
    """Bump versions.env's CODE_SERVER_VERSION pin AND rebuild the dist at it
    (datawrangler stays at its pin). The prompt/confirm is the front-end's job."""
    _set_version_pin(_CODE_SERVER_VERSION_KEY, cs_version)
    dw = load_versions().get(_DATA_WRANGLER_VERSION_KEY)
    if not dw:
        die(f"no pinned {_DATA_WRANGLER_VERSION_KEY} in versions.env")
    _editor_build_dist(cs_version, dw)


def _stage_editor_dist(supervisor: str, *, deploy_local: bool = False) -> None:
    """Stage the host editor dist into a RUNNING supervisor (STAGE_EDITOR_DIST).
    Real files at EDITOR_DIST_MOUNT — interactive inner containers (PI roles,
    sandbox boxes) RO-mount that path and cp their own writable copy at boot.
    Mirrors `_stage_agent_dist`'s uid-0 tar-stream (the sysbox-uid-shift +
    docker-cp-nesting dragons; see that helper) but stages the FLAT editor tree
    (.local/ + tools/ + templates/).

    `deploy_local` ALSO deploys the editor into the supervisor's OWN ~/.local and
    launches its stub (slice 2 — the minimal-lineage bake is gone, so the
    research supervisor's editor must come from the dist). The supervisor
    entrypoint runs at container-START, BEFORE this post-start staging, so its
    dist block saw an empty /opt/editor-dist and no-op'd — this exec is what
    actually brings the supervisor's own editor up (mirrors `_stage_agent_dist`'s
    deploy_local). The research supervisor passes the RESOLVED code-server flag
    (NOT an unconditional True): the deploy script does not re-check
    RS_SERVICE_CODE_SERVER, so a `--disable code-server` supervisor must not cp the
    binary or launch the stub. The sandbox-dind management supervisor passes
    deploy_local=False — it deploys NO editor of its own (its boxes RO-mount the
    staged dist and deploy theirs); staging still runs so that mount is populated."""
    src = EDITOR_DIST_DIR
    if not editor_dist_present():
        die("no cached editor dist to stage — run `research editor pull` first")

    def _root_owned(ti: tarfile.TarInfo) -> tarfile.TarInfo:
        ti.uid = ti.gid = 0
        ti.uname = ti.gname = ""
        return ti
    extract = (f"rm -rf {EDITOR_DIST_MOUNT} && mkdir -p {EDITOR_DIST_MOUNT} && "
               f"tar -C {EDITOR_DIST_MOUNT} -x && chown -R 1000:1000 {EDITOR_DIST_MOUNT}")
    proc = subprocess.Popen(
        ["docker", "exec", "-i", "-u", "0", supervisor, "sh", "-c", extract],
        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        with tarfile.open(fileobj=proc.stdin, mode="w|") as tf:   # streaming; symlinks preserved
            for entry in sorted(os.listdir(src)):
                tf.add(os.path.join(src, entry), arcname=entry, filter=_root_owned)
    finally:
        if proc.stdin:
            proc.stdin.close()
    _, err = proc.communicate()
    if proc.returncode != 0:
        detail = (err.decode(errors="replace") if err else "").strip()
        die(f"staging editor dist into {supervisor} failed: "
            f"{detail or 'tar extract returned non-zero'}")
    if deploy_local:
        # Deploy + launch via the shared dist script (single source of truth for
        # the deploy logic — the same script the interactive leaves' entrypoints
        # run). Runs as the research user (the supervisor/management container
        # default USER, uid 1000) with HOME pinned absolute to /home/research (the
        # cross-boundary-path rule) so the script's $HOME-based cp lands there. The
        # nohup'd stub reparents under tini when this exec session closes.
        run_check(["docker", "exec", "-e", "HOME=/home/research", supervisor,
                   "bash", f"{EDITOR_DIST_MOUNT}/tools/code-server-deploy.sh"])


def _build_images(force: bool) -> None:
    """Build supervisor + worker + mcp-proxy + role-mcp images. Skip
    existing ones unless --rebuild. Build order matters: rs-role-mcp-base
    FROMs rs-analysis-base, per-role images (rs-echo-mcp etc.) FROM
    rs-role-mcp-base — keep the list bottom-up so each FROM resolves to
    the just-built layer rather than a stale cached copy."""
    specs = [
        # No-DIND base MUST build first: rs-substrate-base AND rs-minimal both
        # FROM it (WORKFLOW_TAXONOMY_S1.md carve).
        (MINIMAL_BASE_IMAGE, SCRIPT_DIR / "agent" / "Dockerfile.minimal-base"),
        # Shared substrate base = rs-minimal-base + sysbox DIND. MUST build
        # before its two leaf images (rs-supervisor, rs-sandbox-dind).
        (SUBSTRATE_BASE_IMAGE, SCRIPT_DIR / "agent" / "Dockerfile.substrate-base"),
        (SUPERVISOR_IMAGE, SCRIPT_DIR / "agent" / "Dockerfile.supervisor"),
        (SANDBOX_DIND_IMAGE, SCRIPT_DIR / "agent" / "Dockerfile.sandbox-dind"),
        # rs-minimal — the runnable `docker`-substrate leaf (FROM rs-minimal-base,
        # which is already built above). No agent baked in.
        (MINIMAL_IMAGE, SCRIPT_DIR / "agent" / "Dockerfile.minimal"),
        (ANALYSIS_IMAGE, SCRIPT_DIR / "agent" / "Dockerfile.analysis-base"),
        (MCP_PROXY_IMAGE, SCRIPT_DIR / "agent" / "Dockerfile.mcp-proxy"),
        (ROLE_MCP_BASE_IMAGE, SCRIPT_DIR / "agent" / "Dockerfile.role-mcp-base"),
        (PI_BASE_IMAGE, SCRIPT_DIR / "agent" / "Dockerfile.pi-base"),
        # Clean extension base (STAGE_FEATURE_STAGING C1). FROM miniconda3, so it
        # has no in-tree FROM dependency; the ext leaves (dynamic loop below) AND
        # the lane-3 generic images (pi-isolated + sandbox-box) FROM it, so it MUST
        # precede them.
        (EXT_BASE_IMAGE, SCRIPT_DIR / "agent" / "Dockerfile.ext-base"),
        # Generic PI-isolated image — FROM rs-ext-base (lane-3): research-decoupled
        # + registry-delivered, adds git + the clone/setup entrypoint. One image
        # for every isolated type (type behavior comes from the cloned repo).
        (PI_ISOLATED_IMAGE, SCRIPT_DIR / "agent" / "Dockerfile.pi-isolated"),
        # Disposable sandbox-project box image — FROM rs-ext-base (lane-3): lean
        # (no data-science stack) + clean of the PI artifact-contract. The browser
        # variant FROMs it, so it must build first (bottom-up).
        (SANDBOX_BOX_IMAGE, SCRIPT_DIR / "agent" / "Dockerfile.sandbox-box"),
        (SANDBOX_BOX_BROWSER_IMAGE,
         SCRIPT_DIR / "agent" / "Dockerfile.sandbox-box-browser"),
    ]
    for role, image in sorted(role_mcp.ROLE_IMAGES.items()):
        dockerfile = SCRIPT_DIR / "agent" / f"Dockerfile.{role}"
        if not dockerfile.is_file():
            print(f"warning: role-mcp image {image} has no Dockerfile at "
                  f"{dockerfile.name}; skipping (add it in the per-role stage)",
                  file=sys.stderr)
            continue
        specs.append((image, dockerfile))
    # PI per-role images (rs-pi-echo, rs-pi-wrangler, …) FROM rs-pi-base.
    # Build order is bottom-up so each FROM resolves to the freshly-built
    # layer, same discipline as role-mcp-base.
    for role, image in sorted(extension.BAKED_IMAGES.items()):
        dockerfile = SCRIPT_DIR / "agent" / f"Dockerfile.pi-{role}"
        if not dockerfile.is_file():
            print(f"warning: sandbox image {image} has no Dockerfile at "
                  f"{dockerfile.name}; skipping (add it in the per-role stage)",
                  file=sys.stderr)
            continue
        specs.append((image, dockerfile))
    pins = load_versions()
    # MIGRATED extension-lane roles (STAGE_FEATURE_STAGING C1): build a clean
    # rs-ext-<name> image (FROM rs-ext-base, no rs-analysis-base lineage), tagged
    # with its versions.env pin so the push/pull ref carries the snapshot pin. The
    # PUSH to the local registry happens lazily at `enable` (_push_extension_image),
    # not here — `_build_images` only produces the host image.
    for name, (repo, vkey) in sorted(extension.EXT_REGISTRY_REFS.items()):
        dockerfile = SCRIPT_DIR / "agent" / f"Dockerfile.ext-{name}"
        if not dockerfile.is_file():
            print(f"warning: extension {name!r} has no Dockerfile at "
                  f"{dockerfile.name}; skipping", file=sys.stderr)
            continue
        pin = pins.get(vkey)
        if not pin:
            print(f"warning: extension {name!r} missing pin {vkey} in "
                  f"versions.env; skipping", file=sys.stderr)
            continue
        specs.append((f"rs-ext-{name}:{pin}", dockerfile))
    for tag, dockerfile in specs:
        if not force and run_quiet(["docker", "image", "inspect", tag]):
            print(f"image {tag} already present (use --rebuild to force)")
            continue
        # Pass only the pins this Dockerfile declares an `ARG` for, so docker
        # doesn't warn about unconsumed build-args. The ARG default in the
        # Dockerfile stays the fallback for standalone `docker build` outside
        # this CLI; here the manifest value wins.
        text = dockerfile.read_text()
        build_args: list[str] = []
        for key, value in pins.items():
            if f"ARG {key}" in text:
                build_args += ["--build-arg", f"{key}={value}"]
        print(f"building {tag}...")
        run_check([
            "docker", "build",
            "-f", str(dockerfile),
            "-t", tag,
            *build_args,
            str(SCRIPT_DIR),
        ])

    # Lane-3 generic images (STAGE_FEATURE_STAGING): retag each :latest with its
    # content-snapshot pin so the registry PUSH ref carries it. :latest stays for
    # the FROM chains (sandbox-box-browser FROM rs-sandbox-box:latest) + the
    # SANDBOX_BOX_IMAGE / rs-sandbox BOX_IMAGE constants + the inner retag target.
    for host_base, (_repo, vkey) in sorted(extension.GENERIC_REGISTRY_IMAGES.items()):
        pin = pins.get(vkey)
        if not pin:
            print(f"warning: {host_base} missing pin {vkey} in versions.env; "
                  f"skipping snapshot retag", file=sys.stderr)
            continue
        if run_quiet(["docker", "image", "inspect", f"{host_base}:latest"]):
            run_check(["docker", "tag", f"{host_base}:latest", f"{host_base}:{pin}"])


def _http_json(url: str, timeout: float = 10.0) -> dict:
    """GET a JSON document with the stdlib. A User-Agent is required by the
    GitHub API (it 403s anonymous requests without one) and harmless elsewhere."""
    req = urllib.request.Request(
        url, headers={"User-Agent": "research-sandbox/images-outdated"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def _latest_version(source: dict[str, str]) -> str:
    """Resolve the latest published version for a non-manual datasource. Raises
    on network/parse failure; the caller renders that as 'unreachable'."""
    kind = source["kind"]
    if kind == "github-releases":
        data = _http_json(
            f"https://api.github.com/repos/{source['repo']}/releases/latest")
        return str(data["tag_name"]).lstrip("v")
    if kind == "npm":
        # %2F-encode the scope so the scoped-package GET resolves cleanly.
        pkg = source["pkg"].replace("/", "%2F")
        data = _http_json(f"https://registry.npmjs.org/{pkg}")
        return str(data["dist-tags"]["latest"])
    if kind == "pypi":
        data = _http_json(f"https://pypi.org/pypi/{source['pkg']}/json")
        return str(data["info"]["version"])
    raise ValueError(f"unknown datasource kind: {kind}")


def _start_enabled_mcps() -> None:
    targets = _shared_mcps(only_enabled=True)
    for name, entry in targets:
        try:
            _spawn_shared_mcp(name, entry)
        except SystemExit:
            print(f"warning: failed to start MCP {name!r}; continuing",
                  file=sys.stderr)


def _resolve_create_mcp_arg(value: str | None) -> list[str]:
    """Resolve ``project create --mcp`` into a list of registry names. The
    helper validates membership only — enabled-state and reachability are
    checked per-MCP by ``_allow_mcp_for_project`` so a single misbehaving
    entry can be skipped without aborting the create."""
    v = (value or "all-enabled").strip()
    try:
        data = mcp_registry.load(expand=False)
    except mcp_registry.RegistryError as e:
        die(str(e))
    if v == "all-enabled":
        return sorted(n for n, e in data["mcps"].items()
                      if e.get("enabled", False))
    if v == "none":
        return []
    names = [n.strip() for n in v.split(",") if n.strip()]
    unknown = [n for n in names if n not in data["mcps"]]
    if unknown:
        die(f"--mcp: unknown MCP name(s): {', '.join(unknown)}")
    return names


def _for_containers(op: str, target: str | None) -> None:
    if target == "__ALL__":
        containers = [c["name"] for c in get_supervisor_containers()]
    elif target:
        containers = [container_name_for(target)]
    else:
        die("must specify a project name or --all")
    if not containers:
        print("no projects to act on")
        return
    for name in containers:
        if not container_exists(name):
            print(f"skip: {name} does not exist")
            continue
        run_check(["docker", op, name])
        print(f"{op}: {name}")


# ---------------------------------------------------------------------------
# project update — push edited code into a running project
# ---------------------------------------------------------------------------


# Files baked into the supervisor image at build time. `update` (no --rebuild)
# `docker cp`s each into the stopped supervisor; the next start runs the new
# code. Pairs are (host-relative source, container target, executable bit).
_SUPERVISOR_FILE_MAP: list[tuple[str, str, bool]] = [
    ("cli/rs_worker.py",                                "/usr/local/bin/rs-worker",                          True),
    ("cli/rs_audit_stop.py",                            "/usr/local/bin/rs-audit-stop",                      True),
    ("container/supervisor/mcp_render_config.py",       "/opt/mcp-proxy-tools/mcp_render_config.py",         False),
    ("container/supervisor/mcp-reload.sh",              "/usr/local/bin/mcp-reload",                         True),
    ("container/supervisor/inner-firewall.sh",          "/usr/local/bin/rs-inner-firewall",                  True),
    ("container/supervisor/CLAUDE.md",                  "/opt/claude-templates/CLAUDE.md",                   False),
    ("container/supervisor/setup.sh",                   "/opt/claude-templates/setup.sh",                    True),
    ("container/supervisor/logbook_supervisor_template.md", "/opt/claude-templates/logbook_supervisor_template.md", False),
    ("container/supervisor/logbook_pi_template.md",     "/opt/claude-templates/logbook_pi_template.md",      False),
    # NOTE: code-server stub/settings are NOT file-copied — slice 2 deleted them
    # from the image; the editor now lives in the host dist (code-server-deploy.sh
    # reads them from /opt/editor-dist), and its update path is `research editor
    # refresh/pull` + recreate (re-stages the dist), never a file-only cp.
    ("container/analysis/CLAUDE.md.template",           "/opt/claude-templates/worker.CLAUDE.md.template",   False),
    ("agent/entrypoint.supervisor.sh",                  "/entrypoint.sh",                                    True),
]

_SUPERVISOR_DIR_MAP: list[tuple[str, str]] = [
    ("container/supervisor/commands", "/opt/claude-templates/commands"),
]

# The rs-sandbox-dind leaf has none of the supervisor's agent files — its
# file-only-update surface is just rs-sandbox + its own entrypoint. (code-server
# / byobu live in the shared base, so changes there are a base rebuild, not a
# file-copy.)
_SANDBOX_DIND_FILE_MAP: list[tuple[str, str, bool]] = [
    ("cli/rs_sandbox.py",              "/usr/local/bin/rs-sandbox", True),
    ("agent/entrypoint.sandbox-dind.sh", "/entrypoint.sh",            True),
]


def _docker_cp_with_mode(src: Path, container: str, dst: str, mode: int) -> None:
    """`docker cp` a file with an explicit mode bit, via a tempdir staging
    step (the source files in the working tree may be 0664; baked scripts
    need 0755). Tempdir is cleaned up on return; never persists."""
    with tempfile.TemporaryDirectory() as tmpdir:
        staged = Path(tmpdir) / src.name
        shutil.copy2(src, staged)
        os.chmod(staged, mode)
        run_check(["docker", "cp", str(staged), f"{container}:{dst}"])


def _docker_cp_supervisor_files(container: str) -> list[str]:
    """Copy the edited per-project-substrate files into the container (file-only
    `project update`). Flavor-aware: a sandbox-dind project's rs-sandbox-dind container
    has none of the supervisor's agent paths, so it gets the management map.
    Returns the list of host-relative paths that were actually copied."""
    is_sandbox = _container_project_type(container) == PROJECT_TYPE_SANDBOX_DIND
    file_map = _SANDBOX_DIND_FILE_MAP if is_sandbox else _SUPERVISOR_FILE_MAP
    dir_map: list[tuple[str, str]] = [] if is_sandbox else _SUPERVISOR_DIR_MAP
    copied: list[str] = []
    for rel, dst, exe in file_map:
        src = SCRIPT_DIR / rel
        if not src.is_file():
            continue
        if exe:
            _docker_cp_with_mode(src, container, dst, 0o755)
        else:
            run_check(["docker", "cp", str(src), f"{container}:{dst}"])
        copied.append(rel)
    for rel, dst in dir_map:
        src = SCRIPT_DIR / rel
        if not src.is_dir():
            continue
        # Trailing /. on src copies the contents into the existing target dir
        # (instead of nesting). Doesn't delete files removed from the source.
        run_check(["docker", "cp", f"{src}/.", f"{container}:{dst}/"])
        copied.append(rel + "/")
    return copied


def _read_supervisor_metadata(container: str) -> dict:
    """Inspect the existing supervisor; return the params needed to recreate
    it with `build_supervisor_docker_args` (used by `update --rebuild`)."""
    r = run_check(["docker", "inspect", container])
    data = json.loads(r.stdout)[0]

    # Published SSH host port.
    port_bindings = (data.get("HostConfig") or {}).get("PortBindings") or {}
    ssh_b = port_bindings.get("22/tcp") or []
    ssh_port = int(ssh_b[0]["HostPort"]) if ssh_b else 0

    # Env: SSH_PASSWORD, RS_INNER_FIREWALL.
    env: dict[str, str] = {}
    for entry in (data.get("Config") or {}).get("Env") or []:
        if "=" in entry:
            k, v = entry.split("=", 1)
            env[k] = v

    labels = (data.get("Config") or {}).get("Labels") or {}

    # Bind-mounts other than /workspace and the privileged-DIND volume —
    # i.e. user-supplied --data paths under /workspace/shared/data/<basename>/.
    # /external/* (PI-isolated external folders) are deliberately EXCLUDED:
    # they're recomputed fresh from the host registry on every recreate
    # (_extension_external_mounts), so recovering them here would both
    # double-add and pin stale roots after a registry edit.
    extra_mounts: list[str] = []
    for m in data.get("Mounts") or []:
        dst_in = m.get("Destination", "")
        if dst_in in ("/workspace", "/var/lib/docker"):
            continue
        if dst_in.startswith("/external/"):
            continue
        if m.get("Type") != "bind":
            continue
        ro = ":ro" if m.get("RW") is False else ""
        extra_mounts += ["-v", f"{m['Source']}:{dst_in}{ro}"]

    hc = data.get("HostConfig") or {}
    mem_bytes = hc.get("Memory") or 0
    if mem_bytes and mem_bytes % (1024 ** 3) == 0:
        memory = f"{mem_bytes // (1024 ** 3)}g"
    elif mem_bytes:
        memory = str(mem_bytes)
    else:
        memory = ""
    nano_cpus = hc.get("NanoCpus") or 0
    cpus = f"{nano_cpus / 1e9:g}" if nano_cpus else ""

    service_flags: dict[str, bool] = {}
    for sid in KNOWN_SERVICES:
        v = labels.get(f"{SERVICE_LABEL_PREFIX}{sid}")
        # Missing label (legacy projects) defaults to enabled, which matches
        # the on-create default. ALWAYS_ON_SERVICES are forced True regardless.
        service_flags[sid] = (v != "disabled")
    for sid in ALWAYS_ON_SERVICES:
        service_flags[sid] = True

    return {
        "ssh_port": ssh_port,
        "ssh_pass": env.get("SSH_PASSWORD", ""),
        "dind_mode": labels.get(DIND_MODE_LABEL, "privileged"),
        "inner_firewall": env.get("RS_INNER_FIREWALL") == "1",
        "project_type": labels.get(PROJECT_TYPE_LABEL, PROJECT_TYPE_RESEARCH),
        "substrate": labels.get(SUBSTRATE_LABEL, Substrate.DIND_SYSBOX.value),
        "memory": memory,
        "cpus": cpus,
        "extra_mounts": extra_mounts,
        "service_flags": service_flags,
    }


def _stash_creds_for_rebuild(
    container: str, was_running: bool, workspace_path: Path
) -> None:
    """Move Claude auth state into the workspace bind-mount so it survives
    container destruction. Two pieces:
      - ~research/.claude/        → /workspace/.creds-stash/
      - ~research/.claude.json    → /workspace/.creds-stash-home.json

    The second piece is what makes interactive `claude` skip the /login
    prompt after the recreate (it carries `oauthAccount`); without it,
    the operator re-OAuths every `project update --rebuild`.

    Two restore paths inside this function depending on container state:
    - Running supervisor: `docker exec mv` — atomic, never leaves the
      container's filesystem until the bind-mount writeback hits the
      host workspace dir.
    - Stopped supervisor: `docker cp` — the container is about to be
      destroyed so this is functionally a move; creds never touch /tmp.

    Idempotent: skips any stash points that already exist from a prior
    failed update. The entrypoint will move them back at next start."""
    host_stash = workspace_path / ".creds-stash"
    host_home_stash = workspace_path / ".creds-stash-home.json"
    if was_running:
        if not host_stash.exists():
            run(["docker", "exec", container, "sh", "-c",
                 "if [ -d /home/research/.claude ] && "
                 "[ ! -d /workspace/.creds-stash ]; then "
                 "mv /home/research/.claude /workspace/.creds-stash; "
                 "fi"],
                capture_output=True)
        if not host_home_stash.exists():
            run(["docker", "exec", container, "sh", "-c",
                 "if [ -f /home/research/.claude.json ] && "
                 "[ ! -f /workspace/.creds-stash-home.json ]; then "
                 "mv /home/research/.claude.json "
                 "/workspace/.creds-stash-home.json; "
                 "fi"],
                capture_output=True)
    else:
        # docker cp on a stopped container works for files in its filesystem.
        if not host_stash.exists():
            run(["docker", "cp",
                 f"{container}:/home/research/.claude",
                 str(host_stash)],
                capture_output=True)
        if not host_home_stash.exists():
            run(["docker", "cp",
                 f"{container}:/home/research/.claude.json",
                 str(host_home_stash)],
                capture_output=True)


def _recreate_supervisor(
    project: str,
    cfg: "Config",
    *,
    force_restage: bool = False,
    post_create_hook=None,
    service_flags: dict[str, bool] | None = None,
) -> None:
    """Stash creds → stop+rm → create new container from SUPERVISOR_IMAGE
    → optional post-create hook → start → re-inject route → re-stage inner
    images → respawn mcp-proxy. The only safe shape on sysbox: stop+start
    of the same container ID hits sysbox-mgr's `volume dir for container
    <id> already exists` bug. A fresh container ID gets fresh bindings.

    Workspace, network, SSH port, env, mounts, memory/CPU limits all
    survive because they're recovered from the existing container's
    metadata before rm. Creds move through /workspace/.creds-stash, never
    via /tmp.

    The post-create hook runs against the not-yet-started container —
    used by `cmd_project_update` to docker-cp edited files in before the
    entrypoint reads them."""
    container = container_name_for(project)
    was_running = container_running(container)
    workspace_path = workspace_path_for(project, cfg)

    _stash_creds_for_rebuild(container, was_running, workspace_path)
    md = _read_supervisor_metadata(container)

    # The sysbox recreate dance (rm + create + re-stage) exists because sysbox
    # loses its volume bindings on stop. A docker-substrate project is a plain
    # runc container with no inner store to re-stage — it uses docker start/stop
    # (start() routes it to _start_docker_substrate; update() refuses it). If one
    # ever reaches here it's a routing bug, not a recoverable state.
    if md.get("substrate", Substrate.DIND_SYSBOX.value) == Substrate.DOCKER.value:
        die(f"_recreate_supervisor called for docker-substrate project "
            f"{project!r}; docker projects use plain start/stop")

    if was_running:
        print(f"stopping {container}...")
        run_check(["docker", "stop", container])
    print(f"removing old container {container}...")
    run_check(["docker", "rm", container])

    network = project_network_for(project)
    flags = service_flags if service_flags is not None else md["service_flags"]
    md_ptype = md.get("project_type", PROJECT_TYPE_RESEARCH)
    substrate_image = (SANDBOX_DIND_IMAGE if md_ptype == PROJECT_TYPE_SANDBOX_DIND
                       else SUPERVISOR_IMAGE)
    docker_args = build_supervisor_docker_args(
        container_name=container,
        project=project,
        network=network,
        workspace_path=workspace_path,
        ssh_port=md["ssh_port"],
        ssh_pass=md["ssh_pass"],
        dns_servers=cfg.sandbox_dns,
        memory=md["memory"],
        cpus=md["cpus"],
        image=substrate_image,
        dind_mode=md["dind_mode"],
        inner_firewall=md["inner_firewall"],
        project_type=md_ptype,
        substrate=md["substrate"],
        service_flags=flags,
    )
    if md["extra_mounts"]:
        docker_args = docker_args[:-1] + md["extra_mounts"] + [docker_args[-1]]
    # PI-isolated external folders are recomputed fresh from the host
    # registry (excluded from md["extra_mounts"]) so the recreate tracks
    # the current registry — newly-added types appear, removed ones drop.
    ext_mounts = _extension_external_mounts(project)
    if ext_mounts:
        docker_args = docker_args[:-1] + ext_mounts + [docker_args[-1]]
    # build_supervisor_docker_args emits ["run", "-d", ...]; convert to create.
    assert docker_args[0] == "run" and docker_args[1] == "-d"
    create_args = ["create"] + docker_args[2:]

    print(f"creating new container from {substrate_image}...")
    run_check(["docker", *create_args])

    if post_create_hook is not None:
        post_create_hook(container)

    print(f"starting {container}...")
    run_check(["docker", "start", container])

    router_ip = get_router_ip(network)
    inject_route(container, router_ip)

    # Inner-dockerd state. In sysbox mode /var/lib/docker is fresh, so
    # worker + proxy + role-mcp images need staging. Privileged DIND has a
    # named volume that survives rm; stage_worker_image is a no-op there
    # unless `force_restage` (i.e. images were rebuilt on the host).
    wait_for_inner_dockerd(container)
    run(["docker", "exec", container, "docker", "rm", "-f", "mcp-proxy"],
        capture_output=True)
    # Sandbox flavor stages the blank box image (no analysis workers); see
    # cmd_project_create's matching branch.
    if md_ptype == PROJECT_TYPE_SANDBOX_DIND:
        # Sandbox-dind supervisor RUNS an agent (STAGE_SANDBOX_DIND_AGENT): re-stage
        # the dist into its OWN ~/.local (deploy_local=True; that staging also
        # populates /opt/agent-dist for any boxes), editor gated like research. No
        # bake, so a recreate must redeploy. Before the role-MCP relaunch below.
        _stage_agent_dist(container)
        if editor_dist_present():
            _stage_editor_dist(container, deploy_local=flags.get("code-server", True))
        # Box harness is a standing dind utility (STAGE_DIND_UNIFY — no --with-boxes
        # gate): re-stage rs-sandbox (no bake) + re-deliver the box images for every
        # sandbox-dind recreate.
        _stage_rs_sandbox(container)
        # Box images: read FROZEN pins + PULL (RF1 — never re-push at recreate:
        # the registry already holds the frozen pin from create, and re-pushing
        # would die() if a versions.env bump left the host without the old tag).
        # A pre-pins project has no pins → greenfield backfill (adopt current pins,
        # push, pull, backfill project.json).
        _box_pins = _read_box_pins(workspace_path)
        if _box_pins:
            _deliver_box_images(container, network, _box_pins, push=False)
        else:
            _box_pins = _box_image_pins(load_versions())
            _deliver_box_images(container, network, _box_pins, push=True)
            _write_box_pins(workspace_path, _box_pins)
        # Re-stage the box-preset catalog (best-effort) so a directly-invoked
        # rs-sandbox resolves presets after a recreate (STAGE_BOX_EXT_UX).
        _stage_box_catalog(workspace_path, strict=False)
    else:
        stage_worker_image(container, ANALYSIS_IMAGE, force=force_restage)
        # Re-stage the agent dist into the fresh container (its own ~/.local + the
        # /opt/agent-dist the fleet mounts) — no bake, so a recreate must redeploy
        # it. Before the role-MCP relaunch loop below, which mounts that path.
        _stage_agent_dist(container)
        # Editor dist re-staged the same way (STAGE_EDITOR_DIST) so a recreated
        # supervisor's interactive PI/extension containers find a populated mount.
        # deploy_local re-deploys the supervisor's OWN editor (no bake now), gated
        # on its resolved code-server flag.
        if editor_dist_present():
            _stage_editor_dist(container,
                               deploy_local=flags.get("code-server", True))
    stage_worker_image(container, MCP_PROXY_IMAGE, force=force_restage)

    run(["docker", "exec", container, "/usr/local/bin/mcp-reload"],
        capture_output=True)

    # Bring previously-enabled role-MCPs back up. The inner dockerd is
    # fresh under sysbox; each role-MCP container is gone and must be
    # re-created from the role-mcps.json snapshot. _role_mcp_start lazy-
    # stages each per-role image into the inner dockerd before running
    # it, with force_restage threaded through so a host-side rebuild
    # propagates inward.
    workspace_path = workspace_path_for(project, cfg)
    role_entries = role_mcp.load_role_mcps(workspace_path)
    for role in sorted(role_entries):
        if role_entries[role].get("stopped"):
            # Deliberately parked via `project worker stop` — do NOT
            # auto-restart on recreate. Park survives the supervisor swap
            # (mirrors the worker `down` model); explicit `worker start`
            # brings it back.
            continue
        try:
            _role_mcp_start(container, project, cfg, role,
                            force_restage=force_restage)
        except SystemExit:
            print(f"warning: failed to restart role-mcp {role!r}; "
                  f"the entry in role-mcps.json is intact, retry with "
                  f"`research project role-mcp enable {project} {role}`",
                  file=sys.stderr)

    # Same idea for extension containers (baked PI roles + BYO isolated agents,
    # STAGE_CLI_TAXONOMY). The extensions.json snapshot is the source of truth;
    # _extension_start re-stages the image into the (potentially fresh) inner
    # dockerd and restarts the container. For BYO entries the supervisor's
    # /external/<type> mounts were just recomputed from the registry above, so
    # every agent's external folder is wired before its container restarts.
    # Workspace state survives because it's on the project volume.
    extension_entries = extension.load(workspace_path)
    for name in sorted(extension_entries):
        # kind="sandbox" boxes (the agent-less flavor) are owned by the
        # in-supervisor rs-sandbox CLI, not the host baked/byo start path —
        # _extension_start only knows baked/byo and would wrongly treat a box as
        # byo (external mount + repo env). Delegate the restart to rs-sandbox
        # so the docker-run logic lives in one place.
        if extension_entries[name].get("kind") == extension.SANDBOX_KIND:
            # Boxes are a first-class standing dind utility (STAGE_DIND_UNIFY): make
            # the harness available before restarting. On research the box harness is
            # NOT re-staged by the create/recreate branches above (frozen lane), so
            # without this a research box's container is gone + rs-sandbox/image
            # absent → the restart exec fails + dead tab. Inert on sandbox-dind
            # (eager-staged) and for a box-less project (no kind="sandbox" entries).
            _ensure_box_harness(container, network, workspace_path,
                                bool(extension_entries[name].get("browser")))
            r = run(["docker", "exec", container, "rs-sandbox", "restart", name],
                    capture_output=True)
            if r.returncode != 0:
                print(f"warning: failed to restart sandbox box {name!r}: "
                      f"{(r.stderr or r.stdout).strip()}", file=sys.stderr)
            continue
        try:
            _extension_start(container, project, cfg, name,
                           force_restage=force_restage)
        except SystemExit:
            print(f"warning: failed to restart sandbox {name!r}; "
                  f"the entry in extensions.json is intact, retry with "
                  f"`research project extension enable {project} {name}`",
                  file=sys.stderr)


def _container_project_type(container: str) -> str:
    """Project flavor from the supervisor's research.project_type label
    (defaults to "research" for legacy containers without it)."""
    r = run(["docker", "inspect", "-f",
             f'{{{{index .Config.Labels "{PROJECT_TYPE_LABEL}"}}}}', container],
            capture_output=True)
    t = r.stdout.strip() if r.returncode == 0 else ""
    return t or PROJECT_TYPE_RESEARCH


def _container_substrate(container: str) -> str:
    """Containment substrate from the research.substrate label (defaults to
    dind-sysbox for legacy containers without it). Drives start()'s plain
    docker-start vs sysbox-recreate fork."""
    r = run(["docker", "inspect", "-f",
             f'{{{{index .Config.Labels "{SUBSTRATE_LABEL}"}}}}', container],
            capture_output=True)
    s = r.stdout.strip() if r.returncode == 0 else ""
    return s or Substrate.DIND_SYSBOX.value


def _start_docker_substrate(project: str, cfg: "Config") -> None:  # type: ignore[name-defined]
    """Start a stopped `docker`-substrate project. Unlike a sysbox supervisor
    (which loses its implicit volume bindings on stop and must be recreated), a
    plain runc container starts cleanly with `docker start`. Only the router
    default route — dropped with the container's netns on stop — is re-injected.
    This is the deliberate exception to the 'sysbox can't stop/start' rule: it is
    sysbox-specific, and the docker substrate has no sysbox bindings to lose."""
    container = container_name_for(project)
    run_check(["docker", "start", container])
    network = project_network_for(project)
    inject_route(container, get_router_ip(network))


def _refresh_workspace_claude_templates(container: str) -> None:
    """Overwrite /workspace/.claude/{CLAUDE.md, logbook_*_template.md,
    commands/} from /opt/claude-templates/. The entrypoint's first-boot
    `if-not-present` guard means existing projects never see template
    edits otherwise — this closes that loop. Slash-commands dir is
    rebuilt from scratch (so removed slash commands actually disappear),
    not merged."""
    run_check(["docker", "exec", container, "sh", "-eu", "-c", r"""
        cp -f /opt/claude-templates/CLAUDE.md /workspace/.claude/CLAUDE.md
        cp -f /opt/claude-templates/logbook_supervisor_template.md \
              /workspace/.claude/logbook_supervisor_template.md
        cp -f /opt/claude-templates/logbook_pi_template.md \
              /workspace/.claude/logbook_pi_template.md
        rm -rf /workspace/.claude/commands
        cp -a /opt/claude-templates/commands /workspace/.claude/commands
    """])


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# MCP registry CLI (Stage 2.1)
# ---------------------------------------------------------------------------


def mcp_container_name_for(name: str) -> str:
    return f"{MCP_CONTAINER_PREFIX}{name}"


def projects_using_mcp(mcp_name: str) -> list[str]:
    """Scan per-project allowlists for projects that allow this MCP. The
    allowlist lives at ``<workspace>/.orchestrator/mcp-allow.json`` (see
    project_allowlist_path) — looking at the project root directly would
    silently miss every project."""
    cfg = load_config()
    root = Path(cfg.projects_dir).expanduser().resolve()
    if not root.is_dir():
        return []
    out: list[str] = []
    for p in sorted(root.iterdir()):
        if not p.is_dir():
            continue
        allow_file = project_allowlist_path(p.name, cfg)
        if not allow_file.is_file():
            continue
        try:
            data = json.loads(allow_file.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(data, list):
            continue
        for e in data:
            if isinstance(e, dict) and e.get("name") == mcp_name:
                out.append(p.name)
                break
    return out


def project_allowlist_path(project: str, cfg: "Config") -> Path:
    """Per-project MCP allowlist. Lives INSIDE the workspace (which is the
    only thing the supervisor bind-mounts) so atomic-rename writes by
    research.py are visible to the supervisor immediately. A single-file
    bind-mount would pin the original inode and silently make replacements
    invisible to the container."""
    return workspace_path_for(project, cfg) / ".orchestrator" / "mcp-allow.json"


def ensure_mcp_files(project: str, cfg: "Config") -> None:
    """Initialize the host-side mcp registry (if missing) and an empty
    per-project allowlist. The registry is host-only state; the supervisor
    never reads it directly — every datum it needs lives in the allowlist
    entry written at `project mcp allow` time."""
    if not mcp_registry.REGISTRY_PATH.is_file():
        mcp_registry.REGISTRY_DIR.mkdir(parents=True, exist_ok=True)
        mcp_registry.save_atomic(mcp_registry.empty())
    allow = project_allowlist_path(project, cfg)
    if not allow.is_file():
        allow.parent.mkdir(parents=True, exist_ok=True)
        allow.write_text("[]\n")


def resolve_host_gateway() -> str:
    """Numeric IP for `host.docker.internal` from a container on the host's
    docker daemon. Used to translate `external` MCP destinations to numeric
    IPs in per-project allowlists, so the supervisor's inner-daemon proxy
    can reach them via the existing rs-router path."""
    r = run_check([
        "docker", "run", "--rm",
        "--add-host=host.docker.internal:host-gateway",
        "alpine:3.20", "getent", "hosts", "host.docker.internal",
    ])
    out = r.stdout.strip()
    if not out:
        die("could not resolve host.docker.internal via host-gateway")
    return out.split()[0]


def mcp_container_ip(name: str) -> str:
    cname = mcp_container_name_for(name)
    r = run_check([
        "docker", "inspect", cname, "-f",
        '{{(index .NetworkSettings.Networks "' + ROUTER_NETWORK + '").IPAddress}}',
    ])
    ip = r.stdout.strip()
    # A crash-looping container reports "running" between restarts but has an
    # empty IPAddress; docker's template renders that as the literal string
    # "invalid IP" rather than empty — non-empty would silently propagate to
    # iptables. Parse with ipaddress to catch both.
    try:
        ipaddress.ip_address(ip)
    except ValueError:
        die(f"could not resolve {cname}'s IP on {ROUTER_NETWORK} "
            f"(docker inspect returned {ip!r}); the container may be "
            f"crash-looping. Check `docker logs {cname}`.")
    return ip


def load_project_allowlist(project: str, cfg: "Config") -> list[dict]:
    p = project_allowlist_path(project, cfg)
    if not p.is_file():
        return []
    try:
        data = json.loads(p.read_text())
    except json.JSONDecodeError as e:
        die(f"allowlist {p} is invalid JSON: {e}")
    if not isinstance(data, list):
        die(f"allowlist {p} must be a JSON array")
    return [e for e in data if isinstance(e, dict)]


def save_project_allowlist(project: str, cfg: "Config", entries: list[dict]) -> None:
    p = project_allowlist_path(project, cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(entries, indent=2, sort_keys=True) + "\n")
    tmp.replace(p)


def _parse_kv(items: list[str], flag: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in items:
        if "=" not in item:
            die(f"{flag} entry must be K=V, got {item!r}")
        k, _, v = item.partition("=")
        if not k:
            die(f"{flag} entry has empty key: {item!r}")
        out[k] = v
    return out


def _parse_host_arg(s: str) -> tuple[str, int]:
    host, sep, port_s = s.rpartition(":")
    if not sep or not host or not port_s:
        die(f"--host must be HOST:PORT, got {s!r}")
    try:
        port = int(port_s)
    except ValueError:
        die(f"--host port must be an integer, got {port_s!r}")
    return host, port


def _build_mcp_entry(args: argparse.Namespace) -> dict:
    entry: dict = {"kind": args.kind, "transport": args.transport}
    if args.path and args.path != mcp_registry.DEFAULT_PATH:
        entry["path"] = args.path
    if args.kind == "external":
        if args.host is None:
            die("--host is required for --kind external")
        host_addr, host_port = _parse_host_arg(args.host)
        entry["host_address"] = host_addr
        entry["host_port"] = host_port
        if args.header:
            entry["headers"] = _parse_kv(args.header, "--header")
    else:  # shared
        if not args.image:
            die("--image is required for --kind shared")
        if args.port is None:
            die("--port is required for --kind shared")
        entry["image"] = args.image
        entry["port"] = args.port
        if args.env:
            entry["env"] = _parse_kv(args.env, "--env")
    desc = (getattr(args, "description", None) or "").strip()
    if desc:
        entry["description"] = desc
    roles = _parse_csv_list(getattr(args, "roles", None))
    if roles:
        entry["roles"] = roles
    return entry


def _ensure_router_running() -> None:
    if not container_running(ROUTER_CONTAINER):
        die(f"{ROUTER_CONTAINER} is not running. Run `research start` first.")


def _ensure_registry_running() -> None:
    """Idempotent local extension-image registry on rs-sandbox — the artifact
    store for the extension lane (STAGE_FEATURE_STAGING C1). Triad:
    running -> no-op; exists-stopped -> docker start; absent -> docker run.

    Published on 127.0.0.1:<REGISTRY_HOST_PORT> for the HOST push (loopback =
    insecure-by-default), and reachable on rs-sandbox / per-project networks as
    rs-registry:5000 for the inner-dockerd pull. Stood up lazily (first extension
    enable), not at `research start`."""
    if container_running(REGISTRY_CONTAINER):
        return
    if container_exists(REGISTRY_CONTAINER):
        run_check(["docker", "start", REGISTRY_CONTAINER])
        return
    pins = load_versions()
    image = "registry:" + pins.get("REGISTRY_VERSION", DEFAULT_REGISTRY_VERSION)
    host_port = pins.get("REGISTRY_HOST_PORT", DEFAULT_REGISTRY_HOST_PORT)
    if not run_quiet(["docker", "image", "inspect", image]):
        print(f"pulling {image}...")
        run_check(["docker", "pull", image])
    REGISTRY_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    r = run([
        "docker", "run", "-d",
        "--name", REGISTRY_CONTAINER,
        "--network", ROUTER_NETWORK,
        "--restart", "unless-stopped",
        "-p", f"127.0.0.1:{host_port}:{REGISTRY_INNER_PORT}",
        "-v", f"{REGISTRY_CACHE_DIR}:/var/lib/registry",
        image,
    ], capture_output=True)
    if r.returncode != 0:
        err = (r.stderr or r.stdout or "").strip()
        if "already allocated" in err or "address already in use" in err:
            die(f"host port {host_port} is already in use; set REGISTRY_HOST_PORT "
                f"in versions.env to a free port and retry. (docker: "
                f"{err[-200:]})")
        die(f"failed to start {REGISTRY_CONTAINER}: {err[-300:]}")
    print(f"started {REGISTRY_CONTAINER} (local extension registry on "
          f"{ROUTER_NETWORK})")


def _connect_registry_to_project_network(network: str) -> None:
    """Attach the local registry to a per-project network so the project's inner
    dockerd pulls rs-registry:5000 by container DNS over its OWN subnet (L2) —
    never crossing the router's RFC1918 DROP. Ensures the registry exists first
    (lazy stand-up). Idempotent (a re-connect exits non-zero silently)."""
    _ensure_registry_running()
    run(["docker", "network", "connect", network, REGISTRY_CONTAINER],
        capture_output=True)


def _push_extension_image(name: str) -> None:
    """Lazily publish a migrated extension's host-built image into the local
    registry (STAGE_FEATURE_STAGING C1 lazy-push). Pushes via the loopback port
    (insecure-by-default, no host daemon.json change). Idempotent — re-pushing an
    existing tag re-sends only the manifest (layers are skipped)."""
    pins = load_versions()
    repo, vkey = extension.EXT_REGISTRY_REFS[name]
    pin = pins.get(vkey)
    if not pin:
        die(f"missing version pin {vkey} for extension {name!r}; add it to "
            f"versions.env and run `research start --rebuild`.")
    host_tag = f"rs-ext-{name}:{pin}"
    if not run_quiet(["docker", "image", "inspect", host_tag]):
        die(f"extension image {host_tag} not built; run `research start "
            f"--rebuild` first.")
    _ensure_registry_running()
    host_port = pins.get("REGISTRY_HOST_PORT", DEFAULT_REGISTRY_HOST_PORT)
    push_ref = f"127.0.0.1:{host_port}/{repo}:{pin}"
    run_check(["docker", "tag", host_tag, push_ref])
    run_check(["docker", "push", push_ref])
    print(f"published extension {name!r} -> {extension.EXT_REGISTRY}/{repo}:{pin}")


def _push_generic_image(host_base: str) -> None:
    """Lazily publish a GENERIC lane-3 image (rs-pi-isolated / rs-sandbox-box[
    -browser]) into the local registry. Mirrors _push_extension_image but keys on
    extension.GENERIC_REGISTRY_IMAGES and the rs-<base>:<pin> host tag the build
    retag produced. Called at the MINT site only (create for boxes, enable for
    pi-isolated) — a recreate PULLS the frozen pin, never re-pushes."""
    pins = load_versions()
    repo, vkey = extension.GENERIC_REGISTRY_IMAGES[host_base]
    pin = pins.get(vkey)
    if not pin:
        die(f"missing version pin {vkey} for {host_base!r}; add it to "
            f"versions.env and run `research start --rebuild`.")
    host_tag = f"{host_base}:{pin}"
    if not run_quiet(["docker", "image", "inspect", host_tag]):
        die(f"image {host_tag} not built; run `research start --rebuild` first.")
    _ensure_registry_running()
    host_port = pins.get("REGISTRY_HOST_PORT", DEFAULT_REGISTRY_HOST_PORT)
    push_ref = f"127.0.0.1:{host_port}/{repo}:{pin}"
    run_check(["docker", "tag", host_tag, push_ref])
    run_check(["docker", "push", push_ref])
    print(f"published {host_base} -> {extension.EXT_REGISTRY}/{repo}:{pin}")


# The two blank-box images, in build order (browser FROMs the plain box). Each:
# (host image base, registry repo key in project.json, the :latest tag the
# in-supervisor rs-sandbox `docker run`s — see cli/rs_sandbox.py BOX_IMAGE).
_BOX_IMAGES = [
    ("rs-sandbox-box", "sandbox-box", SANDBOX_BOX_IMAGE),
    ("rs-sandbox-box-browser", "sandbox-box-browser", SANDBOX_BOX_BROWSER_IMAGE),
]


def _box_image_pins(pins: dict[str, str]) -> dict[str, str]:
    """{registry-repo: pin} for the two box images, from versions.env. die()s on a
    missing pin (the mint site — create/greenfield-backfill — needs both)."""
    out: dict[str, str] = {}
    for host_base, repo, _latest in _BOX_IMAGES:
        _, vkey = extension.GENERIC_REGISTRY_IMAGES[host_base]
        pin = pins.get(vkey)
        if not pin:
            die(f"missing version pin {vkey} for {host_base!r}; add it to "
                f"versions.env and run `research start --rebuild`.")
        out[repo] = pin
    return out


def _deliver_box_images(supervisor: str, network: str, box_pins: dict[str, str],
                        *, push: bool, only: "set[str] | None" = None) -> None:
    """Make the pinned box images available in a dind supervisor's inner dockerd,
    registry-delivered. ``push`` (the MINT path — create, or the greenfield
    recreate-backfill) first publishes the host images; a normal recreate is
    PULL-ONLY (push=False) — the registry already holds the frozen pin from create,
    and re-pushing it would die() if a versions.env bump left the host without the
    old tag (RF1). ``only`` restricts delivery to a subset of host-base names
    (default None = both) — the lazy box-harness path delivers just the image a
    box actually needs, so a non-browser box never pulls Chromium. Connects the
    registry to the project network (idempotent — survives recreate), pulls each
    pinned ref into the inner store, and retags to the :latest the in-supervisor
    rs-sandbox runs (so rs-sandbox is unchanged)."""
    images = [t for t in _BOX_IMAGES if only is None or t[0] in only]
    if push:
        for host_base, _repo, _latest in images:
            _push_generic_image(host_base)
    _connect_registry_to_project_network(network)
    for _host_base, repo, latest_tag in images:
        ref = f"{extension.EXT_REGISTRY}/{repo}:{box_pins[repo]}"
        run_check(["docker", "exec", supervisor, "docker", "pull", ref])
        run_check(["docker", "exec", supervisor, "docker", "tag", ref, latest_tag])


def _ensure_box_harness(container: str, network: str, workspace_path: "Path",
                        want_browser: bool) -> None:
    """Idempotently make the rs-sandbox box harness usable in a RUNNING dind
    supervisor: stage the rs-sandbox CLI if absent, and deliver the ONE box image
    this request needs (base, or browser when ``want_browser``) into the inner
    store if absent. No-op where both are already present — every sandbox-dind
    project eager-stages the harness at create/recreate, so this only does work on
    the frozen research lane, which never touches boxes at create/recreate. This is
    the SOLE box-delivery path for research (box_add + the recreate relaunch loop);
    research create/recreate are untouched. Uses the project's frozen pins when
    present (sandbox-dind) else the current versions.env pins (research has no
    frozen box_image_pins — a versions.env bump can drift a research box's image,
    acceptable)."""
    if not run_quiet(["docker", "exec", container, "test", "-e",
                      "/usr/local/bin/rs-sandbox"]):
        _stage_rs_sandbox(container)
    host_base, _repo, latest_tag = _BOX_IMAGES[1] if want_browser else _BOX_IMAGES[0]
    if not run_quiet(["docker", "exec", container, "docker", "image", "inspect",
                      latest_tag]):
        pins = _read_box_pins(workspace_path) or _box_image_pins(load_versions())
        _deliver_box_images(container, network, pins, push=True, only={host_base})


def _read_box_pins(workspace_path: "Path") -> dict[str, str]:
    """The frozen box-image pins from .orchestrator/project.json, or {} for a
    pre-migration project (greenfield → recreate backfills)."""
    f = workspace_path / ".orchestrator" / "project.json"
    try:
        data = json.loads(f.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    pins = data.get("box_image_pins")
    return pins if isinstance(pins, dict) else {}


def _write_box_pins(workspace_path: "Path", box_pins: dict[str, str]) -> None:
    """Backfill the box-image pins into project.json (greenfield recreate), keeping
    every other marker key intact."""
    f = workspace_path / ".orchestrator" / "project.json"
    try:
        data = json.loads(f.read_text())
    except (OSError, json.JSONDecodeError):
        data = {}
    data["box_image_pins"] = box_pins
    f.write_text(json.dumps(data, indent=2) + "\n")


def _stage_box_catalog(workspace_path: "Path", *, strict: bool) -> list[dict]:
    """Stage the resolved box-preset catalog into .orchestrator/box-catalog.json
    (STAGE_BOX_EXT_UX) so the in-supervisor rs-sandbox resolves presets offline,
    and RETURN it so a caller can gate against the same bytes without a second
    load (closing the unguarded-second-load-site gap). Host-side write into the
    bind-mounted workspace dir (atomic-rename → visible to the supervisor
    immediately; a parent-dir mount, not a single-file mount). Q1→live: box_add
    refreshes it (strict=True → BoxCatalogError surfaces, the caller maps it to
    ValidationError); create/recreate stage it best-effort (strict=False → a
    malformed box-registry.json falls back to built-ins + warns, never blocks the
    lifecycle verb)."""
    try:
        catalog = box_catalog.load_catalog()
    except box_catalog.BoxCatalogError:
        if strict:
            raise
        print("warning: box-registry.json is malformed; staging built-in box "
              "presets only until it is fixed", file=sys.stderr)
        catalog = [{**m, "source": "builtin"}
                   for m in box_catalog.load_builtins().values()]
    p = workspace_path / ".orchestrator" / "box-catalog.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(catalog, indent=2, sort_keys=True) + "\n")
    tmp.replace(p)
    return catalog


def _spawn_shared_mcp(name: str, entry: dict) -> None:
    """Run the shared MCP container on rs-sandbox. Idempotent: skips if running."""
    cname = mcp_container_name_for(name)
    if container_running(cname):
        return
    if container_exists(cname):
        run(["docker", "rm", "-f", cname], capture_output=True)
    if not run_quiet(["docker", "image", "inspect", entry["image"]]):
        print(f"pulling {entry['image']}...")
        run_check(["docker", "pull", entry["image"]])
    cmd = [
        "docker", "run", "-d",
        "--name", cname,
        "--network", ROUTER_NETWORK,
        "--restart", "unless-stopped",
        "--label", f"{MCP_LABEL}=1",
        "--label", f"{MCP_NAME_LABEL}={name}",
    ]
    for k, v in entry.get("env", {}).items():
        cmd += ["-e", f"{k}={v}"]
    cmd += [entry["image"]]
    run_check(cmd)
    print(f"started {cname}")


def _set_enabled(name: str, value: bool) -> dict:
    with mcp_registry.lock():
        try:
            data = mcp_registry.load(expand=False)
        except mcp_registry.RegistryError as e:
            die(str(e))
        entry = data["mcps"].get(name)
        if entry is None:
            die(f"no MCP named {name!r}")
        entry["enabled"] = value
        try:
            mcp_registry.save_atomic(data)
        except mcp_registry.RegistryError as e:
            die(str(e))
    return entry


# ---------------------------------------------------------------------------
# PI-isolated type registry CLI (STAGE_PI_ISOLATED)
# ---------------------------------------------------------------------------
# Host-side registry of reusable PI-isolated agent *types* (repo + root
# folder + setup). Mirrors the general MCP registry's host+project split:
# types are defined once here and referenced by name at
# `project create|update --enable <type>`. The registry ships empty — RS
# pre-bakes no types.


def projects_using_extension_type(name: str) -> list[str]:
    """Scan per-project extensions.json snapshots for projects that enable this
    BYO type — the gate for a safe `extension remove`. A BYO extension entry is
    keyed by its type name, so membership is the test."""
    cfg = load_config()
    root = Path(cfg.projects_dir).expanduser().resolve()
    if not root.is_dir():
        return []
    out: list[str] = []
    for p in sorted(root.iterdir()):
        if not p.is_dir():
            continue
        entries = extension.load(workspace_path_for(p.name, cfg))
        e = entries.get(name)
        if isinstance(e, dict) and e.get("kind") == "byo":
            out.append(p.name)
    return out


def _verify_pi_isolated_repo(repo: str, ref: str) -> None:
    """Best-effort `git ls-remote` check that repo+ref resolve, so a typo
    surfaces at `add` time rather than inside the supervisor at first
    enable (bad failure-distance — STAGE_PI_ISOLATED Q6). Skipped with a
    warning if git isn't on the host PATH; the operator can pass
    --no-verify to skip deliberately."""
    if not shutil.which("git"):
        print("warning: git not on PATH; skipping repo/ref verification "
              "(pass --no-verify to silence)", file=sys.stderr)
        return
    r = run(["git", "ls-remote", "--exit-code", repo, ref],
            capture_output=True)
    if r.returncode != 0:
        die(f"could not resolve ref {ref!r} in {repo!r} via git ls-remote "
            f"(pass --no-verify to skip this check):\n"
            f"  {(r.stderr or r.stdout).strip()}")


def _resolve_pi_isolated_ref(repo: str) -> str:
    """Resolve the repo's default-branch HEAD to a concrete commit SHA so a
    `--ref`-less `add` still pins (no silent upstream drift — the invariant
    holds, the operator just doesn't have to look the SHA up). Requires git
    on the host: pinning is non-negotiable, so if we can't resolve we fail
    rather than store an unpinned entry."""
    if not shutil.which("git"):
        die("git not on PATH: cannot resolve the latest commit to pin "
            "(--ref omitted). Install git, or pass --ref <sha> explicitly.")
    r = run(["git", "ls-remote", repo, "HEAD"], capture_output=True)
    if r.returncode != 0 or not r.stdout.split():
        die(f"could not resolve default-branch HEAD of {repo!r} via "
            f"git ls-remote:\n  {(r.stderr or r.stdout).strip()}")
    return r.stdout.split()[0]


def _extension_registry_edit(name: str, mutate) -> None:
    with pi_isolated_registry.lock():
        try:
            data = pi_isolated_registry.load(expand=False)
        except pi_isolated_registry.RegistryError as e:
            die(str(e))
        entry = data["types"].get(name)
        if entry is None:
            die(f"no extension type named {name!r}")
        mutate(entry)
        try:
            pi_isolated_registry.save_atomic(data)
        except pi_isolated_registry.RegistryError as e:
            die(str(e))


def _shared_mcps(only_enabled: bool = False) -> list[tuple[str, dict]]:
    try:
        data = mcp_registry.load()
    except mcp_registry.RegistryError as e:
        die(str(e))
    out = []
    for name, entry in sorted(data["mcps"].items()):
        if entry["kind"] != "shared":
            continue
        if only_enabled and not entry.get("enabled", False):
            continue
        out.append((name, entry))
    return out


def _probe_mcp(name: str, entry: dict) -> tuple[bool, str]:
    if entry["kind"] == "external":
        host = entry.get("host_address", "host.docker.internal")
        port = entry["host_port"]
        cmd = [
            "docker", "run", "--rm",
            "--network", ROUTER_NETWORK,
            "--add-host", "host.docker.internal:host-gateway",
            PROBE_IMAGE,
            "nc", "-z", "-w", "5", host, str(port),
        ]
    else:
        cname = mcp_container_name_for(name)
        if not container_running(cname):
            return False, (f"shared MCP container {cname} not running "
                           f"(try: research mcp start {name})")
        cmd = [
            "docker", "run", "--rm",
            "--network", ROUTER_NETWORK,
            PROBE_IMAGE,
            "nc", "-z", "-w", "5", cname, str(entry["port"]),
        ]
    r = run(cmd, capture_output=True)
    if r.returncode == 0:
        return True, ""
    return False, (r.stderr or r.stdout).strip()


def _supervisor_mcp_reload(container_name: str) -> None:
    """Re-render the supervisor's proxy config and SIGHUP the proxy."""
    if not container_running(container_name):
        return
    r = run(["docker", "exec", container_name, "/usr/local/bin/mcp-reload"],
            capture_output=True)
    if r.returncode != 0:
        msg = (r.stderr or r.stdout).strip()
        print(f"warning: mcp-reload in {container_name} failed: {msg}",
              file=sys.stderr)


def _allow_mcp_for_project(project: str, cfg: "Config", mcp_name: str,
                           *, do_reload: bool = True) -> tuple[bool, str]:
    """Open the router hole, append (or replace) the per-project allowlist
    entry, and optionally reload the supervisor's mcp-proxy. Returns
    ``(ok, message)`` so callers can iterate batches without aborting.
    The caller owns project-existence checks; this helper validates
    router + registry + (shared) container state."""
    container_name = container_name_for(project)

    if not container_running(ROUTER_CONTAINER):
        return False, f"{ROUTER_CONTAINER} is not running"

    try:
        entry = mcp_registry.entry_for(mcp_name)
    except mcp_registry.RegistryError as e:
        return False, str(e)
    if entry is None:
        return False, f"no MCP named {mcp_name!r}"
    if not entry.get("enabled", False):
        return False, f"MCP {mcp_name!r} is not enabled"

    if entry["kind"] == "external":
        host_addr = entry.get("host_address", "host.docker.internal")
        ip = resolve_host_gateway() if host_addr == "host.docker.internal" else host_addr
        port = entry["host_port"]
    else:  # shared
        cname = mcp_container_name_for(mcp_name)
        if not container_running(cname):
            return False, f"shared MCP container {cname} not running"
        ip = mcp_container_ip(mcp_name)
        port = entry["port"]

    network = project_network_for(project)
    subnet = get_network_subnet(network)
    r = run(["docker", "exec", ROUTER_CONTAINER,
             "/scripts/mcp-allow.sh", subnet, ip, str(port)],
            capture_output=True)
    if r.returncode != 0:
        return False, (r.stderr or r.stdout).strip() or "mcp-allow.sh failed"

    allowlist = load_project_allowlist(project, cfg)
    allowlist = [e for e in allowlist if e.get("name") != mcp_name]
    new_entry = {
        "name": mcp_name,
        "kind": entry["kind"],
        "transport": entry.get("transport", "http"),
        "ip": ip,
        "port": port,
        "path": entry.get("path", mcp_registry.DEFAULT_PATH),
    }
    if entry.get("headers"):
        new_entry["headers"] = entry["headers"]
    if entry.get("description"):
        new_entry["description"] = entry["description"]
    allowlist.append(new_entry)
    save_project_allowlist(project, cfg, allowlist)

    if do_reload:
        _supervisor_mcp_reload(container_name)
    return True, f"-> {ip}:{port}"


def _deny_mcp_for_project(project: str, cfg: "Config", mcp_name: str,
                          *, do_reload: bool = True) -> tuple[bool, str]:
    """Close the router hole, drop the entry from the per-project
    allowlist, optionally reload the supervisor's mcp-proxy. Returns
    ``(ok, message)``. Tolerates a torn-down project network / stopped
    router (skips the firewall step in that case)."""
    container_name = container_name_for(project)

    allowlist = load_project_allowlist(project, cfg)
    target = next((e for e in allowlist if e.get("name") == mcp_name), None)
    if target is None:
        return False, f"{mcp_name!r} is not currently allowed"

    network = project_network_for(project)
    if network_exists(network) and container_running(ROUTER_CONTAINER):
        subnet = get_network_subnet(network)
        run(["docker", "exec", ROUTER_CONTAINER,
             "/scripts/mcp-deny.sh", subnet,
             str(target.get("ip", "")), str(target.get("port", ""))],
            capture_output=True)

    allowlist = [e for e in allowlist if e.get("name") != mcp_name]
    save_project_allowlist(project, cfg, allowlist)

    if do_reload:
        _supervisor_mcp_reload(container_name)
    return True, ""


def _require_project(project: str) -> str:
    container_name = container_name_for(project)
    if not container_exists(container_name):
        die(f"project {project!r} does not exist")
    return container_name


def _batch_apply(project: str, cfg: "Config", names: list[str],
                 helper, action: str) -> tuple[list[str], list[str]]:
    """Run ``helper`` (allow or deny) for each name with reload deferred.
    Returns ``(succeeded, failed)`` lists. Caller is responsible for the
    one-shot reload at the end."""
    succeeded: list[str] = []
    failed: list[str] = []
    for name in names:
        ok, msg = helper(project, cfg, name, do_reload=False)
        if ok:
            succeeded.append(name)
            print(f"{action} {name!r}{(' ' + msg) if msg else ''}")
        else:
            failed.append(name)
            print(f"warning: {action} {name!r} skipped: {msg}",
                  file=sys.stderr)
    return succeeded, failed


# ---------------------------------------------------------------------------
# Per-project role-MCP lifecycle (B.0)
# ---------------------------------------------------------------------------


def _role_mcp_stage_creds(supervisor: str, role: str) -> None:
    """Snapshot the supervisor's current Claude credentials into the
    per-role daemon-state dir so the role-MCP container can stage them at
    boot. Idempotent: overwrites any previous snapshot.

    Tolerant of an un-authed supervisor: if the supervisor has no
    `.credentials.json` yet, this stages nothing and returns cleanly — it
    does NOT fail. Enablement is independent of auth (the worker-side twin
    of the PI auth-ownership model): the daemon boots idle and the
    supervisor's claude copies its own creds in later via
    `rs-role-mcp sync-creds`, prompted by the `needs_credentials` send_job
    envelope. The `mkdir -p` below is kept unconditionally — it is the
    load-bearing root-owned-bind-source guard, needed whether or not creds
    exist.

    Path note: creds land under .role-mcps/<role>/.creds/ (the daemon-state
    location), NOT under shared/<role>/ which is reserved for the role's
    public publish surface. Mixing them would expose creds to any future
    cross-role RO consumer of shared/<role>/."""
    # mkdir -p does double duty: stages creds, AND pre-creates the publish
    # source dir (/workspace/shared/{role}) and daemon-state dir
    # (/workspace/.role-mcps/{role}) with the supervisor user's ownership
    # (uid 1000) BEFORE docker tries to bind-mount them. If we don't,
    # docker auto-creates missing bind-mount sources as root-owned 755,
    # and the role-MCP container's worker user (also uid 1000 but
    # different namespace) can't write to them. Failure mode is
    # silent until the daemon tries its first write.
    #
    # Role-MCPs only spawn headless `claude -p`, which works from
    # `.credentials.json` alone — no `~/.claude.json` propagation here.
    script = f"""
        set -e
        mkdir -p /workspace/.role-mcps/{role}/.creds
        mkdir -p /workspace/shared/{role}
        if [ ! -f /home/research/.claude/.credentials.json ]; then
            echo "supervisor not yet authenticated; role-mcp {role} will boot idle (run rs-role-mcp sync-creds after /login)" >&2
            exit 0
        fi
        cp /home/research/.claude/.credentials.json \
           /workspace/.role-mcps/{role}/.creds/.credentials.json
        chmod 600 /workspace/.role-mcps/{role}/.creds/.credentials.json
        if [ -f /home/research/.claude/settings.json ]; then
            # Strip the `hooks` key — the supervisor's Stop hook calls
            # /usr/local/bin/rs-audit-stop, which is baked into the
            # supervisor image only. Propagating it would break every
            # claude session in the role-MCP container with a "command
            # not found" error on every Stop event.
            jq 'del(.hooks)' /home/research/.claude/settings.json \
               > /workspace/.role-mcps/{role}/.creds/settings.json
            chmod 600 /workspace/.role-mcps/{role}/.creds/settings.json
        fi
    """
    r = run(["docker", "exec", supervisor, "bash", "-eu", "-c", script],
            capture_output=True)
    if r.returncode != 0:
        die((r.stderr or r.stdout).strip()
            or f"failed to stage creds for role-mcp {role!r}")


def _role_mcp_migrate_state(supervisor: str, role: str) -> None:
    """One-shot move of daemon-state subdirs from the B.0 layout
    (/workspace/shared/<role>/{jobs,memories,...}) to the B.3 layout
    (/workspace/.role-mcps/<role>/{jobs,memories,...}).

    Idempotent: only moves entries when the source exists and the
    destination doesn't. Safe to call on every role-mcp start; on
    already-migrated workspaces every check short-circuits.

    The publish surface at /workspace/shared/<role>/ is preserved (the
    non-daemon-state files there, if any, stay put — they're the role's
    public artifact dir going forward). Daemon-state names are explicit
    (no glob) so we don't accidentally sweep a future publish artifact
    a user dropped in there."""
    daemon_state_names = [
        "jobs", "memories", ".calls", ".creds",
        "global.md", ".summarize-watermark",
    ]
    moves = " ".join(daemon_state_names)
    script = f"""
        set -e
        src=/workspace/shared/{role}
        dst=/workspace/.role-mcps/{role}
        mkdir -p "$dst"
        for name in {moves}; do
            if [ -e "$src/$name" ] && [ ! -e "$dst/$name" ]; then
                mv "$src/$name" "$dst/$name"
                echo "migrated $src/$name -> $dst/$name" >&2
            fi
        done
    """
    run(["docker", "exec", supervisor, "bash", "-eu", "-c", script],
        capture_output=True)


def _role_mcp_inner_exists(supervisor: str, role: str) -> bool:
    cname = role_mcp.role_container_name(role)
    r = run(["docker", "exec", supervisor,
             "docker", "inspect", cname], capture_output=True)
    return r.returncode == 0


def _role_mcp_inner_running(supervisor: str, role: str) -> bool:
    cname = role_mcp.role_container_name(role)
    r = run(["docker", "exec", supervisor,
             "docker", "inspect", "-f", "{{.State.Running}}", cname],
            capture_output=True)
    return r.returncode == 0 and r.stdout.strip() == "true"


def _data_mount_args_from_supervisor(supervisor: str) -> list[str]:
    """Harvest `--data` bind-mounts from the supervisor and return docker
    `-v` args that propagate them RO into an inner container at the same
    paths. Role-MCPs and PI containers gain visibility into the project's
    `/workspace/shared/data/<basename>/` dirs that workers already see via
    their RO mount of `<workspace>/shared/`.

    Symmetric exposure: every inner container sees every `--data` path
    the operator passed at `project create`. Per-role narrower visibility
    would need a new flag (deferred — `--data` stays project-level).

    The destination path inside the supervisor is itself a valid path on
    the supervisor's filesystem (it's a bind-mount from the host); the
    inner dockerd can bind that same path into a child container with
    no further translation. We RO-pin it regardless of the supervisor's
    own mount mode (operator may have writable `--data` in the future;
    inner containers stay RO for the security posture)."""
    r = run(["docker", "inspect", supervisor, "--format", "{{json .Mounts}}"],
            capture_output=True)
    if r.returncode != 0:
        return []
    try:
        mounts = json.loads(r.stdout)
    except json.JSONDecodeError:
        return []
    args: list[str] = []
    for m in mounts:
        if m.get("Type") != "bind":
            continue
        dst = m.get("Destination") or ""
        if dst.startswith("/workspace/shared/data/"):
            args += ["-v", f"{dst}:{dst}:ro"]
    return args


def _role_mcp_start(supervisor: str, project: str, cfg: "Config",
                    role: str, *, force_restage: bool = False) -> None:
    """Run the role-MCP container in the supervisor's inner dockerd.
    Idempotent: tears down any prior instance with the same name first so
    a stale crashed container doesn't block start. Lazy-stages the
    per-role image into the inner dockerd on first use; project create
    doesn't pre-stage role-MCP images (most projects won't use them).
    Pass force_restage=True after a host-side image rebuild to push the
    new content through."""
    workspace_path = workspace_path_for(project, cfg)
    entries = role_mcp.load_role_mcps(workspace_path)
    entry = entries.get(role)
    if entry is None:
        die(f"no role-mcps.json entry for role {role!r}; call enable first")

    role_mcp.validate_role(role)

    cname = role_mcp.role_container_name(role)
    # rm any prior container BEFORE migrating state, so the move can't race
    # a running daemon that's still writing into /workspace/shared/<role>/.
    run(["docker", "exec", supervisor, "docker", "rm", "-f", cname],
        capture_output=True)
    _role_mcp_migrate_state(supervisor, role)
    _role_mcp_stage_creds(supervisor, role)

    image = entry.get("image") or role_mcp.ROLE_IMAGES[role]
    stage_worker_image(supervisor, image, force=force_restage)

    # Bind-mount layout:
    #   /workspace                  ← <supervisor>/workspace/.role-mcps/<role>
    #     RW. Daemon-private state: jobs/, memories/, global.md, .calls/,
    #     .creds/, .summarize-watermark, .tools-inventory.md. Hidden under
    #     a leading-dot dir on the project volume so casual `ls /shared/`
    #     doesn't surface internals.
    #   /workspace/published        ← <supervisor>/workspace/shared/<role>
    #     RW from this role-MCP. The role's PUBLIC artifact surface —
    #     intended to be cross-role-RO-consumable later. Wrangler writes
    #     extracts/<topic>/<slug>.{parquet,sql,metadata.json} here;
    #     librarian (B.2) will write refs/<topic>/; echo and (likely)
    #     websearcher leave it empty.
    #   /etc/orchestrator           ← <supervisor>/workspace/.orchestrator (RO)
    #     Parent-dir bind-mount so atomic-rename writes by the host stay
    #     visible (single-file-bind-mount rule); entrypoint reads
    #     role-mcps.json and mcp-allow.json from here.
    ip = entry["ip"]
    # Substrate (B.1-substrate) resource flags + concurrency env. Persisted
    # in role-mcps.json so they survive _recreate_supervisor without
    # re-consulting Config (operator's enable-time intent is captured).
    memory = entry.get("memory") or cfg.default_role_mcp_memory
    mcc = entry.get("max_concurrent_calls")
    if mcc is None:
        mcc = cfg.default_role_mcp_max_concurrent_calls
    docker_args = [
        "docker", "exec", supervisor,
        "docker", "run", "-d",
        "--name", cname,
        "--network", INNER_NETWORK,
        "--ip", ip,
        "--restart", "unless-stopped",
        # tini at PID 1 reaps zombies. Belt-and-suspenders with per-MCP
        # `dumb-init` wrappers in image-baked extras (B.1) — if a wrapped
        # stdio MCP dies uncleanly, grandchildren reparent to container
        # PID 1 and tini reaps them.
        "--init",
        # Blast-radius backstop. OOM-killer takes the role container,
        # not the supervisor. See Config.default_role_mcp_memory comment
        # for the size reasoning.
        f"--memory={memory}",
        "-v", f"/workspace/.role-mcps/{role}:/workspace",
        "-v", f"/workspace/shared/{role}:/workspace/published",
        "-v", "/workspace/.orchestrator:/etc/orchestrator:ro",
        *AGENT_DIST_MOUNT_ARGS,   # claude copy-source (no bake; slice 2)
        "-e", f"RS_ROLE_NAME={role}",
        "-e", f"RS_ROLE_MCP_PORT={role_mcp.ROLE_MCP_PORT}",
        # Daemon reads this to enforce the cap on send_job. 0 = uncapped.
        "-e", f"RS_ROLE_MAX_CONCURRENT_CALLS={int(mcc)}",
        "--label", f"research.role_mcp={role}",
        "--label", f"research.project={project}",
        # Project --data paths, propagated RO at the same mount points
        # the supervisor + workers see them at.
        *_data_mount_args_from_supervisor(supervisor),
        image,
    ]
    run_check(docker_args)
    print(f"role-mcp {role!r}: running at {ip}:{role_mcp.ROLE_MCP_PORT}")


def _role_mcp_stop(supervisor: str, role: str) -> None:
    """Hard stop + remove the role-MCP container (`docker rm -f` = SIGKILL,
    no grace, no drain). This is `disable`'s teardown — it pairs with
    dropping the role-mcps.json entry, so abruptness is acceptable: the role
    is leaving the project. For a *graceful park that keeps the entry*, use
    `_role_mcp_park` instead. Tolerates absence — caller may have already
    removed it via _recreate_supervisor."""
    cname = role_mcp.role_container_name(role)
    run(["docker", "exec", supervisor, "docker", "rm", "-f", cname],
        capture_output=True)


def _role_mcp_in_flight(workspace_path: Path, role: str) -> list[dict]:
    """Read-only, host-side count of in-flight send_job calls, straight off
    the project volume (`<workspace>/.role-mcps/<role>/jobs/*.json` — the same
    files the daemon writes; see daemon.py JobStore). A job still in status
    `running` is a live `claude -p` call (or a daemon-restart orphan the
    daemon would reap on its next boot; from the host the file state is the
    conservative signal — we'd rather refuse a stop than kill a real call).
    Returns the running entries so the caller can name them in a refusal.
    Pure read — no docker, no mutation, safe to call as a gate."""
    jobs_dir = workspace_path / ".role-mcps" / role / "jobs"
    if not jobs_dir.is_dir():
        return []
    running: list[dict] = []
    for p in sorted(jobs_dir.glob("*.json")):
        try:
            entry = json.loads(p.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if isinstance(entry, dict) and entry.get("status") == "running":
            running.append(entry)
    return running


def _role_mcp_park(supervisor: str, role: str) -> None:
    """Graceful stop of the role-MCP container WITHOUT removing it and
    WITHOUT touching role-mcps.json — the role stays registered, just parked.
    Uses `docker stop` (SIGTERM + grace), not the `rm -f` (SIGKILL) that
    `disable` uses. The daemon has no SIGTERM drain handler, so gracefulness
    here comes from *quiescence*, not from the signal: callers gate on
    `_role_mcp_in_flight` == 0 first, so the daemon is idle (no `claude -p`
    child, no memory write mid-flight) when the signal lands and a
    handler-less SIGTERM terminates a process with nothing to tear. The
    container is left `exited` (name preserved, visible in `docker ps -a`);
    `_role_mcp_start` rm's and replaces it on unpark, and
    `_recreate_supervisor` skips parked entries entirely. The
    `--restart unless-stopped` policy honors a manual stop, so the parked
    container won't auto-restart within the supervisor's lifetime."""
    cname = role_mcp.role_container_name(role)
    run(["docker", "exec", supervisor, "docker", "stop", cname],
        capture_output=True)


def _derive_auto_upstreams(role: str, project: str, cfg: "Config") -> list[str]:
    """Auto-wired upstream set for ``role``: every registered MCP whose
    ``roles`` field lists ``role`` AND that is currently allowed for the
    project. Sorted alphabetically so role-mcps.json diffs across sync
    runs are minimal."""
    try:
        registry = mcp_registry.load(expand=False)
    except mcp_registry.RegistryError as e:
        die(str(e))
    allowed = {e.get("name") for e in load_project_allowlist(project, cfg)
               if e.get("name")}
    return sorted(
        name for name, entry in registry["mcps"].items()
        if role in (entry.get("roles") or [])
        and name in allowed
    )


def _derive_extension_auto_upstreams(project: str, cfg: "Config") -> list[str]:
    """Auto-wired upstream set for a baked extension: EVERY MCP currently
    allowed for the project. Deliberately wider than the worker's
    ``_derive_auto_upstreams`` (which filters by the registry ``roles`` claim) —
    an extension is a PI-facing surface with no role-claim concept, so "auto"
    means "everything the project can reach." Sorted for minimal diffs."""
    return sorted(
        e["name"] for e in load_project_allowlist(project, cfg) if e.get("name")
    )


def _role_mcp_enable(project: str, cfg: "Config", role: str,
                     upstreams: list[str] | None,
                     *, force_auto: bool = False,
                     memory: str | None = None,
                     max_concurrent_calls: int | None = None) -> None:
    """Validate + write the per-project role-mcps.json entry + start the
    container + reload the supervisor's mcp-proxy so its config includes
    the role-MCP route. Idempotent. The worker surface is fully independent
    of the same-named extension (no auto-mirror) — enable an extension via
    ``research project extension enable``.

    Upstream-source state machine:
      - ``upstreams=list, force_auto=False``: explicit pin. Survives sync.
      - ``upstreams=None, force_auto=True``: re-derive from registry × allow,
        write ``upstream_source=auto``. The re-mark path.
      - ``upstreams=None, force_auto=False``:
          - if no existing entry: first-time enable — auto-derive, write
            ``upstream_source=auto``. Empty result emits the M8 warning.
          - if an entry exists: idempotent re-run — preserve current
            ``upstream_source`` and ``upstream_mcps``. No silent flips."""
    role_mcp.validate_role(role)
    supervisor = container_name_for(project)
    if not container_running(supervisor):
        die(f"project {project!r} is not running; bring it up first")

    workspace_path = workspace_path_for(project, cfg)
    entries = role_mcp.load_role_mcps(workspace_path)
    existing = entries.get(role)

    if upstreams is not None:
        chosen_upstreams = list(upstreams)
        chosen_source = "explicit"
    elif force_auto or existing is None:
        chosen_upstreams = _derive_auto_upstreams(role, project, cfg)
        chosen_source = "auto"
        if not chosen_upstreams:
            print(
                f"warning: no registered MCPs claim role {role!r}; "
                f"role-mcp {role!r} starting with empty inventory. "
                f"Add a registry entry with `research mcp add ... "
                f"--roles {role}` (then `research project mcp sync "
                f"{project}`), or pin explicit upstreams with "
                f"`research project role-mcp enable {project} {role} "
                f"--upstream <csv>`.",
                file=sys.stderr,
            )
    else:
        # Preserve-on-reenable: idempotent re-run, no silent flips.
        chosen_upstreams = list(existing.get("upstream_mcps") or [])
        chosen_source = existing.get("upstream_source") or "explicit"

    allow_entries = load_project_allowlist(project, cfg)
    try:
        role_mcp.validate_upstreams(chosen_upstreams, allow_entries)
    except ValueError as e:
        die(str(e))

    # Resource caps: explicit flag > existing entry > cfg default. The
    # entry always carries a concrete value so _recreate_supervisor and
    # `role-mcp status` reads don't need access to Config — the persisted
    # state is the source of truth. A bump to DEFAULT_ROLE_MCP_* only
    # affects NEW enables; existing entries keep their captured values
    # until disable+enable (predictable across recreates).
    if memory is not None:
        chosen_memory = memory
    elif existing is not None and existing.get("memory"):
        chosen_memory = str(existing["memory"])
    else:
        chosen_memory = cfg.default_role_mcp_memory

    if max_concurrent_calls is not None:
        chosen_mcc = max_concurrent_calls
    elif existing is not None and existing.get("max_concurrent_calls") is not None:
        chosen_mcc = int(existing["max_concurrent_calls"])
    else:
        chosen_mcc = cfg.default_role_mcp_max_concurrent_calls

    new_entry = role_mcp.build_entry(
        role, chosen_upstreams, upstream_source=chosen_source,
        memory=chosen_memory, max_concurrent_calls=chosen_mcc,
    )
    # Preserve parked state across re-enable / sync re-derive: a worker the
    # operator deliberately stopped (`project worker stop`) stays parked
    # until an explicit `project worker start`. build_entry always renders a
    # running entry, so without this carry-forward a `project mcp sync`
    # re-derive would silently resurrect a stopped worker.
    parked = bool(existing is not None and existing.get("stopped"))
    if parked:
        new_entry["stopped"] = True
    entries[role] = new_entry
    role_mcp.save_role_mcps(workspace_path, entries)

    if parked:
        print(
            f"worker {role!r}: entry updated but left parked — start it with "
            f"`research project worker start {project} {role}`",
            file=sys.stderr,
        )
    else:
        _role_mcp_start(supervisor, project, cfg, role)
        _supervisor_mcp_reload(supervisor)


def _role_mcp_disable(project: str, cfg: "Config", role: str) -> None:
    """Stop the container, drop the role-mcps.json entry, reload the
    proxy. Workspace state under /workspace/.role-mcps/<role>/ (daemon
    state: jobs, memories, global.md, creds) and under
    /workspace/shared/<role>/ (publish surface) both survive — the
    bind-mounts are on the project volume and unaffected by docker rm."""
    supervisor = container_name_for(project)
    workspace_path = workspace_path_for(project, cfg)
    entries = role_mcp.load_role_mcps(workspace_path)
    if role not in entries:
        die(f"role-mcp {role!r} is not enabled for project {project!r}")
    if container_running(supervisor):
        _role_mcp_stop(supervisor, role)
    del entries[role]
    role_mcp.save_role_mcps(workspace_path, entries)
    if container_running(supervisor):
        _supervisor_mcp_reload(supervisor)


def _parse_csv_list(value: str | None) -> list[str]:
    if not value:
        return []
    return [t.strip() for t in value.split(",") if t.strip()]


# ---------------------------------------------------------------------------
# Per-project PI-role lifecycle (STAGE_BACKEND_PI P.0)
# ---------------------------------------------------------------------------


def _extension_ensure_workspace(supervisor: str, name: str, kind: str) -> None:
    """Pre-create the extension's workspace bind-mount source dir as the
    uid-1000 supervisor user, so dockerd's auto-create on ``docker run -v``
    doesn't land it root-owned and lock out the container's worker user.

    No credentials are staged — extensions are PI-owned: they boot un-authed
    and the PI authenticates in-tab (``/login``) or pulls the supervisor's
    creds via the manual ``rs-pi sync-creds`` bridge. bypassPermissions
    config is baked into rs-pi-base. Source path differs by kind (baked:
    ``pi/<name>``; byo: ``pi-isolated/<name>``) — see extension.workspace_subdir."""
    sub = extension.workspace_subdir(name, kind)
    r = run(["docker", "exec", supervisor, "bash", "-eu", "-c",
             f"mkdir -p /workspace/{sub}"], capture_output=True)
    if r.returncode != 0:
        die((r.stderr or r.stdout).strip()
            or f"failed to ensure workspace dir for sandbox {name!r}")


def _extension_inner_exists(supervisor: str, name: str, kind: str) -> bool:
    # `docker container inspect` (not bare inspect, which falls through to the
    # same-named image — the inner dockerd tags per-role images with the
    # container's unqualified name).
    cname = extension.container_name(name, kind)
    r = run(["docker", "exec", supervisor,
             "docker", "container", "inspect", cname], capture_output=True)
    return r.returncode == 0


def _extension_inner_running(supervisor: str, name: str, kind: str) -> bool:
    cname = extension.container_name(name, kind)
    r = run(["docker", "exec", supervisor,
             "docker", "inspect", "-f", "{{.State.Running}}", cname],
            capture_output=True)
    return r.returncode == 0 and r.stdout.strip() == "true"


def _inner_container_states(supervisor: str) -> dict[str, str]:
    """Map inner-container name → docker state (running/exited/created/dead/…)
    via a single ``docker ps -a``. Empty dict when the supervisor is down, so
    callers fall back to config-only listings (the comprehensive-list rule:
    every subcontainer visible, supervisor up *or* down)."""
    if not container_running(supervisor):
        return {}
    r = run(["docker", "exec", supervisor, "docker", "ps", "-a",
             "--format", "{{.Names}}\t{{.State}}"], capture_output=True)
    if r.returncode != 0:
        return {}
    out: dict[str, str] = {}
    for line in r.stdout.splitlines():
        if "\t" in line:
            n, _, s = line.partition("\t")
            out[n.strip()] = s.strip()
    return out


def _extension_start(supervisor: str, project: str, cfg: "Config",
                   name: str, *, force_restage: bool = False) -> None:
    """Run an extension container in the supervisor's inner dockerd. Idempotent:
    tears down any prior same-named container first. Branches on the entry's
    ``kind`` — baked roles stage a per-role image + mirror MCP wiring; BYO
    agents stage the generic image + clone an external repo. Lazy-stages the
    image; pass force_restage after a host-side rebuild."""
    workspace_path = workspace_path_for(project, cfg)
    entries = extension.load(workspace_path)
    entry = entries.get(name)
    if entry is None:
        die(f"no extensions.json entry for {name!r}; call enable first")
    kind = entry.get("kind")

    cname = extension.container_name(name, kind)
    run(["docker", "exec", supervisor, "docker", "rm", "-f", cname],
        capture_output=True)
    _extension_ensure_workspace(supervisor, name, kind)
    ip = entry["ip"]

    # Editor dist (STAGE_EDITOR_DIST): interactive PI/extension containers RO-mount
    # the supervisor's staged /opt/editor-dist + carry RS_SERVICE_CODE_SERVER from
    # the PROJECT's code-server service flag (read off the supervisor's label, the
    # outside-the-container truth). The entrypoint deploys the editor only when the
    # flag is enabled AND the mount is populated (a project without the editor
    # pulled stages none → an empty/absent mount → the entrypoint no-ops). The mount
    # is spliced unconditionally (an inner -v auto-creates an inert empty dir if the
    # source is absent, exactly like the agent mount); the flag gates the deploy.
    _editor_on = _read_service_flags(supervisor).get("code-server", True)
    editor_args = [
        *EDITOR_DIST_MOUNT_ARGS,
        "-e", f"RS_SERVICE_CODE_SERVER={'enabled' if _editor_on else 'disabled'}",
    ]

    if kind == "baked":
        image = entry.get("image") or extension.image_ref(name, load_versions())
        if extension.is_ext(name):
            # MIGRATED extension (STAGE_FEATURE_STAGING C1): the inner dockerd
            # PULLS the snapshot ref from the local registry (offline — the
            # registry is attached to this project's own network) instead of a
            # host save/load stage. force_restage is irrelevant: the pin is
            # immutable, so a recreate re-pulls the same tag (layer-cached). The
            # same ref is used for the `docker run` below.
            run_check(["docker", "exec", supervisor, "docker", "pull", image])
        else:
            stage_worker_image(supervisor, image, force=force_restage)
        # Single RW workspace mount + RO orchestrator mount. The latter lets a
        # mirror role render .mcp.json + .tools-inventory.md at entrypoint time
        # from the same role-mcps.json the worker service uses (pi-echo-style
        # roles with no worker twin simply ignore it). No /creds mount —
        # PI-owned; bypassPermissions baked into rs-pi-base.
        docker_args = [
            "docker", "exec", supervisor,
            "docker", "run", "-d",
            "--name", cname,
            "--network", INNER_NETWORK,
            "--ip", ip,
            "--restart", "unless-stopped",
            "-v", f"/workspace/pi/{name}:/workspace",
            "-v", "/workspace/.orchestrator:/etc/orchestrator:ro",
            *AGENT_DIST_MOUNT_ARGS,   # claude copy-source (no bake; slice 2)
            *editor_args,             # code-server copy-source + flag (STAGE_EDITOR_DIST)
            "-e", f"RS_PI_ROLE={name}",
            "--label", f"research.pi_role={extension.pi_role_label(name, kind)}",
            "--label", f"research.project={project}",
            # Project --data paths, propagated RO at the same mount points the
            # supervisor + workers see them at.
            *_data_mount_args_from_supervisor(supervisor),
            image,
        ]
        run_check(docker_args)
        print(f"sandbox {name!r} (baked): running at {ip}")
    else:  # byo
        # Registry-delivered (lane-3): PULL the snapshot rs-pi-isolated ref the
        # inner dockerd already has access to (the registry was connected to this
        # project's network at enable, and survives recreate). entry.get("image")
        # is the frozen pin; the generic_image_ref fallback covers a pre-migration
        # byo entry with no "image" key — converting a would-be KeyError into a
        # caught pull-failure (the restart loop's except SystemExit skips it
        # gracefully; the operator re-enables to push/connect/snapshot) — RF3. The
        # fallback's missing-pin is mapped to die() (SystemExit) too, so it stays a
        # graceful per-extension skip in the recreate loop rather than a ValueError
        # that would escape and abort the whole recreate.
        image = entry.get("image")
        if not image:
            try:
                image = extension.generic_image_ref("rs-pi-isolated", load_versions())
            except ValueError as e:
                die(str(e))
        run_check(["docker", "exec", supervisor, "docker", "pull", image])
        mount = entry.get("mount") or pi_isolated_registry.DEFAULT_MOUNT
        # RW workspace (repo cloned to /workspace/<repo> by the entrypoint) +
        # the external host folder at the configured mount. No /creds mount.
        docker_args = [
            "docker", "exec", supervisor,
            "docker", "run", "-d",
            "--name", cname,
            "--network", INNER_NETWORK,
            "--ip", ip,
            "--restart", "unless-stopped",
            "-v", f"/workspace/pi-isolated/{name}:/workspace",
            "-v", f"/external/{name}:{mount}",
            *AGENT_DIST_MOUNT_ARGS,   # claude copy-source (no bake; slice 2)
            *editor_args,             # code-server copy-source + flag (STAGE_EDITOR_DIST)
            "-e", f"RS_PI_ISO_NAME={name}",
            "-e", f"RS_PI_ISO_REPO={entry.get('repo') or ''}",
            "-e", f"RS_PI_ISO_REF={entry.get('ref') or ''}",
            "-e", f"RS_PI_ISO_SETUP={entry.get('setup') or ''}",
            "-e", f"RS_PI_ISO_MOUNT={mount}",
            "--label", f"research.pi_role={extension.pi_role_label(name, kind)}",
            "--label", f"research.pi_isolated={name}",
            "--label", f"research.project={project}",
            *_data_mount_args_from_supervisor(supervisor),
            image,
        ]
        run_check(docker_args)
        print(f"sandbox {name!r} (byo): running at {ip}")


def _extension_stop(supervisor: str, name: str, kind: str) -> None:
    """Stop + remove the extension container in the inner dockerd. Tolerates
    absence. Verifies removal with `docker container inspect` (not bare
    inspect, which falls through to the same-named image)."""
    cname = extension.container_name(name, kind)
    rm = run(["docker", "exec", supervisor, "docker", "rm", "-f", cname],
             capture_output=True)
    check = run(["docker", "exec", supervisor,
                 "docker", "container", "inspect", cname],
                capture_output=True)
    if check.returncode == 0:
        rm_tail = (rm.stderr or rm.stdout or "").strip()[-200:]
        die(f"extension container {cname!r} still present after disable; "
            f"docker rm -f tail: {rm_tail!r}. Inspect "
            f"`docker exec {supervisor} docker container inspect {cname}` "
            f"manually and retry `research project extension disable`.")


def _extension_enable(project: str, cfg: "Config", name: str,
                      upstreams: list[str] | None = None,
                      *, force_auto: bool = False) -> None:
    """Resolve the extension kind (baked role vs BYO registry type), write the
    per-project extensions.json entry, and start the container. Idempotent.

    A baked extension owns its own proxy-routed MCP upstream set, independent of
    any worker service of the same name (the entrypoint renders .mcp.json from
    this extension's own entry). Upstream-source state machine (mirrors
    ``_role_mcp_enable``):
      - ``upstreams=list``: explicit pin (survives sync).
      - ``upstreams=None, force_auto=True`` OR first enable: auto-derive = every
        MCP allowed for the project; write ``upstream_source=auto``.
      - ``upstreams=None, force_auto=False`` with an existing entry: idempotent
        re-run — preserve the current source + set.
    BYO agents are isolated (no proxy .mcp.json) so they take no upstreams; they
    recreate the supervisor first if it doesn't yet mount /external/<name>."""
    supervisor = container_name_for(project)
    if not container_running(supervisor):
        die(f"project {project!r} is not running; bring it up first")
    workspace_path = workspace_path_for(project, cfg)
    entries = extension.load(workspace_path)

    if extension.is_baked(name):
        existing = entries.get(name)
        if upstreams is not None:
            chosen_upstreams = list(upstreams)
            chosen_source = "explicit"
        elif force_auto or existing is None:
            chosen_upstreams = _derive_extension_auto_upstreams(project, cfg)
            chosen_source = "auto"
        else:
            chosen_upstreams = list(existing.get("upstream_mcps") or [])
            chosen_source = existing.get("upstream_source") or "explicit"
        try:
            role_mcp.validate_upstreams(
                chosen_upstreams, load_project_allowlist(project, cfg))
        except ValueError as e:
            die(str(e))
        entries[name] = extension.build_baked_entry(
            name, load_versions(),
            upstreams=chosen_upstreams, upstream_source=chosen_source)
        extension.save(workspace_path, entries)
        if extension.is_ext(name):
            # MIGRATED extension (STAGE_FEATURE_STAGING C1): publish the host
            # image to the local registry (lazy push) and attach the registry to
            # this project's network so the inner dockerd's pull in _extension_start
            # resolves rs-registry:5000 over the project's own subnet. Both are
            # idempotent.
            _push_extension_image(name)
            _connect_registry_to_project_network(project_network_for(project))
        _extension_start(supervisor, project, cfg, name)
        return

    # BYO: look up the host registry type, allocate an IP, snapshot the entry.
    type_entry = pi_isolated_registry.entry_for(name, expand=True)
    if type_entry is None:
        die(f"no sandbox named {name!r} (not a baked role, not in the host "
            f"BYO registry). Register a BYO type first: `research extension add "
            f"{name} --root <host-dir> [--repo <url> --ref <sha>]`")
    try:
        ip = extension.allocate_byo_ip(entries, name)
    except ValueError as e:
        die(str(e))
    entries[name] = extension.build_byo_entry(name, type_entry, ip, load_versions())
    extension.save(workspace_path, entries)

    # Registry-deliver the generic rs-pi-isolated image (lane-3): push (lazy) +
    # connect the registry to this project's network, BEFORE the external-mount
    # branch — because the first byo enable commonly takes the recreate-and-return
    # path, where the container is started by the recreate restart loop's
    # _extension_start (byo branch), which PULLS rs-registry:5000/pi-isolated:<pin>.
    # If push/connect sat only after this branch, that pull would hit an unconnected
    # registry / unpushed blob and fail (RF2).
    _push_generic_image("rs-pi-isolated")
    _connect_registry_to_project_network(project_network_for(project))

    if not _supervisor_has_external_mount(supervisor, name):
        print(f"sandbox {name!r}: supervisor not yet mounting /external/{name}; "
              f"recreating supervisor to wire the external folder (creds + "
              f"workspace survive)...")
        _recreate_supervisor(project, cfg)
        return  # recreate's restart loop starts the container
    _extension_start(supervisor, project, cfg, name)


def _extension_disable(project: str, cfg: "Config", name: str) -> None:
    """Stop the container, drop the extensions.json entry. Workspace state (and,
    for BYO, the external host folder with the cloned repo) survives — they're
    on the project volume / host root, unaffected by docker rm."""
    supervisor = container_name_for(project)
    workspace_path = workspace_path_for(project, cfg)
    entries = extension.load(workspace_path)
    if name not in entries:
        die(f"sandbox {name!r} is not enabled for project {project!r}")
    kind = entries[name].get("kind")
    if container_running(supervisor):
        _extension_stop(supervisor, name, kind)
    del entries[name]
    extension.save(workspace_path, entries)


# ---------------------------------------------------------------------------
# Sandbox external-folder mounts (BYO sandboxes only — baked roles have none)
# ---------------------------------------------------------------------------


def _extension_external_mounts(project: str) -> list[str]:
    """``-v`` args mounting each registered BYO type's ``<root>/<project>/``
    host folder at the supervisor's ``/external/<type>``. Computed fresh from
    the host BYO registry at every supervisor create/recreate, so the mount
    set always tracks the current registry (a removed type drops out; a newly-
    added type appears on the next recreate). The per-project subdir is created
    host-side so docker doesn't auto-create it root-owned. ``~`` is expanded
    here; ``${VAR}`` was expanded by the registry loader."""
    try:
        data = pi_isolated_registry.load(expand=True)
    except pi_isolated_registry.RegistryError as e:
        die(str(e))
    mounts: list[str] = []
    for name, entry in sorted(data["types"].items()):
        root = Path(entry["root"]).expanduser()
        host_dir = root / project
        host_dir.mkdir(parents=True, exist_ok=True)
        mounts += ["-v", f"{host_dir}:/external/{name}"]
    return mounts


def _supervisor_has_external_mount(supervisor: str, name: str) -> bool:
    """True if the supervisor container currently bind-mounts
    ``/external/<name>``. Drives the enable path's decide-to-recreate: a
    type registered after the supervisor's last create/recreate isn't
    mounted yet, so enable must recreate (re-enumerating the registry)
    before it can start the inner container against that source path."""
    r = run(["docker", "inspect", "-f",
             "{{range .Mounts}}{{.Destination}}\n{{end}}", supervisor],
            capture_output=True)
    if r.returncode != 0:
        return False
    return f"/external/{name}" in r.stdout.split("\n")


def _registered_sandbox_byo_types() -> set[str]:
    """BYO extension type names in the host registry, or empty on load failure
    (a malformed registry shouldn't break `project create`; the dedicated
    `sandbox` subcommands surface the error). Baked role names are constants
    (extension.baked_names()), not in this set."""
    try:
        return set(pi_isolated_registry.load(expand=False)["types"])
    except pi_isolated_registry.RegistryError:
        return set()


def _split_enable_tokens(
    enable_arg: str | None,
) -> tuple[str | None, list[str], list[str]]:
    """Split ``--enable`` value into (service_csv, worker_roles, sandboxes).

    Tokens are matched against the registries in order, by canonical name:
      - a key in ``role_mcp.ROLE_IMAGES`` (e.g. ``wrangler``, ``websearcher``,
        ``echo-mcp``) peels into the worker list,
      - a name resolving to an extension type (baked ``echo`` / ``wrangler`` /
        ``websearcher`` or a BYO registry type) that is NOT also a worker peels
        into the extension list,
      - anything else stays for ``_compute_service_flags`` as a service id.

    Worker-first ordering resolves the twin overlap (``wrangler`` is both a
    worker and a baked extension): a bare twin name enables the WORKER only —
    the worker and the same-named extension are independent surfaces (no
    auto-mirror), so the extension is enabled separately via
    ``project extension enable``.

    Empty service set returns None to keep the default intact."""
    if not enable_arg:
        return None, [], []
    sandbox_types = extension.known_type_names()
    services: list[str] = []
    workers: list[str] = []
    sandboxes: list[str] = []
    for tok in enable_arg.split(","):
        tok = tok.strip()
        if not tok:
            continue
        if tok in role_mcp.ROLE_IMAGES:
            workers.append(tok)
        elif tok in sandbox_types:
            sandboxes.append(tok)
        else:
            services.append(tok)
    svc_csv = ",".join(services) if services else None
    return svc_csv, workers, sandboxes


def _parse_role_mcp_upstream(
    raw: list[str], *, valid_roles: set[str],
) -> dict[str, list[str]]:
    """Parse repeated ``--role-mcp-upstream <role>=<csv>`` flags into
    ``{role: [mcp_name, ...]}``. Each role must appear in the same
    ``--enable`` set as a role-MCP token, so a typo or stray role-mcp
    surfaces here rather than as an orphan upstream override.

    An entry with empty CSV (``--role-mcp-upstream wrangler=``) means
    'explicit empty' — daemon comes up with no upstreams — distinct from
    the absence of the flag entirely (which falls through to auto-derive)."""
    out: dict[str, list[str]] = {}
    for item in raw:
        if "=" not in item:
            die(f"--role-mcp-upstream value must be 'role=csv', got {item!r}")
        role, _, csv = item.partition("=")
        role = role.strip()
        if not role:
            die(f"--role-mcp-upstream missing role name in {item!r}")
        if role not in valid_roles:
            die(f"--role-mcp-upstream {role!r} not in --enable role-mcp "
                f"set {sorted(valid_roles)}")
        out[role] = _parse_csv_list(csv)
    return out


def _ordered_union(*lists: list[str]) -> list[str]:
    """Concatenate lists preserving first-seen order, dropping duplicates."""
    seen: set[str] = set()
    out: list[str] = []
    for lst in lists:
        for x in lst:
            if x not in seen:
                seen.add(x)
                out.append(x)
    return out


def _split_disable_tokens(
    disable_arg: str | None,
) -> tuple[str | None, list[str], list[str]]:
    """Mirror of `_split_enable_tokens` for the disable side: (service_csv,
    workers, sandboxes). Same worker-first resolution order so a token that
    names both a worker service and a baked extension (wrangler / websearcher)
    disables the worker (whose mirror then isn't enabled either). `--disable`
    overrules the default-enable set at `project create`, and disables an
    enabled worker/extension at `project update`."""
    if not disable_arg:
        return None, [], []
    sandbox_types = extension.known_type_names()
    services: list[str] = []
    workers: list[str] = []
    sandboxes: list[str] = []
    for tok in disable_arg.split(","):
        tok = tok.strip()
        if not tok:
            continue
        if tok in role_mcp.ROLE_IMAGES:
            workers.append(tok)
        elif tok in sandbox_types:
            sandboxes.append(tok)
        else:
            services.append(tok)
    svc_csv = ",".join(services) if services else None
    return svc_csv, workers, sandboxes


# ---------------------------------------------------------------------------
# Per-supervisor service flags
# ---------------------------------------------------------------------------


def _parse_service_list(s: str | None) -> set[str]:
    if not s:
        return set()
    return {tok for tok in (t.strip() for t in s.split(",")) if tok}


def _compute_service_flags(
    enable_arg: str | None,
    disable_arg: str | None,
    base: dict[str, bool] | None = None,
) -> dict[str, bool]:
    """Resolve per-service enabled flags from --enable / --disable args.

    Default = every known service enabled (or `base` if updating, so missing
    flags inherit the supervisor's prior choices). `supervisor` (the
    SSH + byobu substrate) is always-on and cannot be disabled. Unknown
    service ids are a hard error."""
    enable = _parse_service_list(enable_arg)
    disable = _parse_service_list(disable_arg)
    unknown = (enable | disable) - set(KNOWN_SERVICES)
    if unknown:
        die(f"unknown service id(s): {sorted(unknown)} "
            f"(known: {KNOWN_SERVICES})")
    bad = disable & ALWAYS_ON_SERVICES
    if bad:
        die(f"cannot disable always-on service(s): {sorted(bad)}")

    flags: dict[str, bool] = dict(base) if base else {}
    for sid in KNOWN_SERVICES:
        flags.setdefault(sid, True)
    for sid in disable:
        flags[sid] = False
    for sid in enable:
        flags[sid] = True
    for sid in ALWAYS_ON_SERVICES:
        flags[sid] = True
    return flags


def _read_service_flags(container: str) -> dict[str, bool]:
    """Recover per-service flags from a supervisor's existing labels.
    Missing labels (legacy projects) default to enabled. Used by
    `_recreate_supervisor` so a bare `project update` preserves prior
    --enable/--disable choices."""
    if not container_exists(container):
        return {sid: True for sid in KNOWN_SERVICES}
    r = run(["docker", "inspect", container, "-f",
             "{{json .Config.Labels}}"], capture_output=True)
    try:
        labels = json.loads(r.stdout) or {}
    except json.JSONDecodeError:
        labels = {}
    out: dict[str, bool] = {}
    for sid in KNOWN_SERVICES:
        v = labels.get(f"{SERVICE_LABEL_PREFIX}{sid}")
        out[sid] = (v != "disabled")  # missing or "enabled" => True
    for sid in ALWAYS_ON_SERVICES:
        out[sid] = True
    return out


# ---------------------------------------------------------------------------
# webui (browser SSH multiplexer + service-aware proxy host)
# ---------------------------------------------------------------------------


def _supervisor_ssh_pass(container: str) -> str | None:
    """Read SSH_PASSWORD from a supervisor container's env. Returns None
    when the container is missing or the password isn't published."""
    if not container_exists(container):
        return None
    r = run(["docker", "inspect", container, "-f",
             "{{range .Config.Env}}{{println .}}{{end}}"],
            capture_output=True)
    if r.returncode != 0:
        return None
    for line in r.stdout.splitlines():
        if line.startswith("SSH_PASSWORD="):
            return line.split("=", 1)[1]
    return None


def _webui_import_string(project: str, ssh_pass: str) -> str:
    """Build the base64 import string for the webui SPA. Webui reaches the
    supervisor via container DNS on the per-project network (rs-webui is
    `docker network connect`'d to every rs-net-<proj>), not via the
    published host SSH port."""
    return base64.b64encode(json.dumps({
        "name": project,
        "host": container_name_for(project),
        "port": 22,
        "username": "research",
        "password": ssh_pass,
    }).encode()).decode()


def webui_attach_info(req: "AttachRequest", cfg: "Config" | None = None) -> AttachInfo:  # type: ignore[name-defined]
    """The JIT keyring: resolve a project's SSH coordinates so the webui can
    attach without a host-side `research webui import`. Same target shape as
    `_webui_import_string` (container DNS + internal sshd port 22 — the webui
    reaches supervisors over the per-project bridge, not the published host
    port), but structured rather than base64'd because this is a broker→webui
    machine path, not a human paste.

    Returns the password in-memory; NEVER prints/logs it (the broker serialises
    the result over the socket, the audit line records principal/verb/outcome
    only). die()s if the project is absent or not running — a stopped
    supervisor has no bridge endpoint to attach to."""
    if cfg is None:
        cfg = load_config()
    container = container_name_for(req.name)
    if not container_exists(container):
        die(f"project {req.name!r} does not exist")
    if not container_running(container):
        die(f"project {req.name!r} is not running; start it before attaching")
    ssh_pass = _supervisor_ssh_pass(container)
    if not ssh_pass:
        die(f"could not read SSH password for {req.name!r}")
    # port 22 + user 'research' mirror _webui_import_string above; keep the two
    # in lockstep (both describe the same per-project-bridge endpoint).
    return AttachInfo(name=req.name, host=container, port=22,
                      username="research", password=ssh_pass)


def _running_dind_supervisor(project: str) -> str:
    """Resolve + validate a RUNNING dind supervisor (research OR sandbox-dind) for
    the box_* AND ext_* verbs, returning its container name. die()s (→ the broker's
    `failed` envelope) if the project is absent, stopped, or the docker containment
    substrate (no inner dockerd → no boxes/extensions). The box harness and
    extensions are a standing dind utility on BOTH flavors now (STAGE_DIND_UNIFY),
    so flavor is no longer rejected here — only the substrate is. The substrate
    die() makes the webui's Boxes/Extensions surfaces self-hide on a docker box,
    mirroring how those sections gate."""
    container = container_name_for(project)
    if not container_exists(container):
        die(f"project {project!r} does not exist")
    if not container_running(container):
        die(f"project {project!r} is not running; start it before managing "
            f"boxes or extensions")
    if _container_substrate(container) == Substrate.DOCKER.value:
        die(f"project {project!r} uses the docker containment substrate (no inner "
            f"dockerd); boxes and extensions are unavailable")
    return container


def box_add(req: "BoxAddRequest", progress=None) -> BoxAddResult:  # type: ignore[name-defined]
    """Create a sandbox box in a running sandbox-dind supervisor by driving its
    in-box `rs-sandbox create`. Validated fields only reach the list-form argv
    (no shell); the box itself is the security boundary (locked egress, no
    creds). Returns the box's recorded coordinates."""
    progress = progress or _NULL_PROGRESS
    progress.step("validate", "checking the project")
    cfg = load_config()
    container = _running_dind_supervisor(req.project)
    workspace_path = workspace_path_for(req.project, cfg)
    # Refresh the staged box-preset catalog (Q1→live: an operator-registered type in
    # box-registry.json becomes usable on this already-created project). strict=True
    # → a malformed registry surfaces as ValidationError, not a half-built box.
    try:
        staged = _stage_box_catalog(workspace_path, strict=True)
    except box_catalog.BoxCatalogError as e:
        raise ValidationError(str(e))
    # Semantic gating against the JUST-STAGED catalog (reuse the returned bytes —
    # no second load_catalog(), so no unguarded BoxCatalogError escape past
    # dispatch) + the project allowlist. The in-box rs-sandbox re-gates too; this
    # yields a clean pre-exec ValidationError.
    catalog = {e["name"]: e for e in staged}
    if req.preset != "empty" and req.preset not in catalog:
        raise ValidationError(
            f"unknown box preset {req.preset!r} "
            f"(available: {sorted({'empty', *catalog})})")
    allowed = {e["name"] for e in load_project_allowlist(req.project, cfg)
               if e.get("name")}
    bad = [m for m in req.mcps if m not in allowed]
    if bad:
        raise ValidationError(
            f"MCP(s) {bad} are not allowed for project {req.project!r}; allow them "
            f"first (`research project mcp allow ...`) or omit them")
    # Lazily stand up the box harness. On research this stages rs-sandbox + delivers
    # the needed box image on first use (research create/recreate never touch boxes
    # — the frozen lane); on sandbox-dind (eager-staged) it no-ops. The image a box
    # needs is the browser image iff its preset selects it.
    want_browser = catalog.get(req.preset, {}).get("image") == "browser"
    progress.step("harness", "ensuring the box harness")
    _ensure_box_harness(container, project_network_for(req.project),
                        workspace_path, want_browser)
    argv = ["docker", "exec", container, "rs-sandbox", "create"]
    if req.name:
        argv.append(req.name)
    argv += ["--preset", req.preset]
    if req.agent is not None:
        argv += ["--agent", req.agent]
    if req.editor:
        argv.append("--editor")
    if req.mcps:
        argv += ["--mcps", ",".join(req.mcps)]
    if req.repo:
        argv += ["--repo", req.repo]
    if req.ref:
        argv += ["--ref", req.ref]
    if req.setup:
        argv += ["--setup", req.setup]
    progress.step("create-box", "creating the box")
    r = run(argv, capture_output=True)
    if r.returncode != 0:
        die(f"failed to add box: {(r.stderr or r.stdout).strip()}")
    try:
        info = json.loads(r.stdout)            # rs-sandbox create prints the entry
    except (json.JSONDecodeError, TypeError):
        die(f"could not parse rs-sandbox output: {r.stdout.strip()!r}")
    # Shape-guard the indexing so a returncode-0-but-wrong-shape output die()s
    # (→ SystemExit, caught by the broker) rather than raising a KeyError/TypeError
    # that would escape dispatch's invariant; box_list is already .get()-defensive.
    if not isinstance(info, dict) or not all(k in info for k in ("name", "ip", "container")):
        die(f"unexpected rs-sandbox output shape: {r.stdout.strip()!r}")
    progress.step("ready", "box ready")
    return BoxAddResult(project=req.project, name=info["name"], ip=info["ip"],
                        preset=info.get("preset", req.preset),
                        browser=bool(info.get("browser")),
                        agent=info.get("agent", "none"),
                        editor=bool(info.get("editor", req.editor)),
                        container=info["container"])


def box_remove(req: "BoxRemoveRequest", progress=None) -> BoxRemoveResult:  # type: ignore[name-defined]
    """Discard a sandbox box (container + its workspace) via the in-box
    `rs-sandbox discard`. Step-up re-auth was already enforced by the broker."""
    progress = progress or _NULL_PROGRESS
    progress.step("validate", "checking the project")
    container = _running_dind_supervisor(req.project)
    progress.step("discard", "discarding the box"
                  + (" (keeping artifacts)" if req.keep_workspace else ""))
    cmd = ["docker", "exec", container, "rs-sandbox", "discard", req.name]
    if req.keep_workspace:
        cmd.append("--keep-workspace")
    r = run(cmd, capture_output=True)
    if r.returncode != 0:
        die(f"failed to remove box {req.name!r}: {(r.stderr or r.stdout).strip()}")
    return BoxRemoveResult(project=req.project, name=req.name)


def box_list(req: "BoxListRequest", _progress=None) -> BoxListResult:  # type: ignore[name-defined]
    """List a project's sandbox boxes with live container state, via
    `rs-sandbox list --json`. Filters to kind=="sandbox" (the rs-sandbox-owned
    boxes); baked/byo extensions are managed via `research project extension`."""
    container = _running_dind_supervisor(req.project)
    r = run(["docker", "exec", container, "rs-sandbox", "list", "--json"],
            capture_output=True)
    if r.returncode != 0:
        die(f"failed to list boxes: {(r.stderr or r.stdout).strip()}")
    try:
        rows = json.loads(r.stdout)
    except (json.JSONDecodeError, TypeError):
        die(f"could not parse rs-sandbox output: {r.stdout.strip()!r}")
    boxes = [{"name": e.get("name"), "ip": e.get("ip"),
              "agent": e.get("agent", "none"), "browser": bool(e.get("browser")),
              "state": e.get("state")}
             for e in rows
             if isinstance(e, dict) and e.get("kind") == extension.SANDBOX_KIND]
    return BoxListResult(project=req.project, boxes=boxes)


def ext_enable(req: "ExtEnableRequest", progress=None) -> ExtEnableResult:  # type: ignore[name-defined]
    """Enable a baked/BYO extension on a running dind project — research OR
    sandbox-dind now (STAGE_DIND_UNIFY); webui-driven.
    Coarse progress wrapper over the shared `_extension_enable` (which owns the
    upstream state-machine + the BYO-recreate path). For a BYO first-enable the
    'enable' milestone stays pending through the supervisor recreate — acceptable
    (the op tails like a slow create; terminals reconnect)."""
    progress = progress or _NULL_PROGRESS
    progress.step("validate", "checking the project")
    _running_dind_supervisor(req.project)
    cfg = load_config()
    progress.step("enable", "enabling the extension")
    _extension_enable(req.project, cfg, req.name, req.upstreams,
                      force_auto=req.force_auto)
    progress.step("ready", "extension ready")
    entry = extension.load(workspace_path_for(req.project, cfg)).get(req.name, {})
    return ExtEnableResult(
        project=req.project, name=req.name, kind=entry.get("kind", "?"),
        upstream_source=entry.get("upstream_source"),
        upstream_mcps=list(entry.get("upstream_mcps") or []))


def ext_disable(req: "ExtDisableRequest", progress=None) -> ExtDisableResult:  # type: ignore[name-defined]
    """Disable an extension on a running dind project (research or sandbox-dind).
    Workspace (and, for BYO, the cloned external folder) survive — no step-up
    needed."""
    progress = progress or _NULL_PROGRESS
    progress.step("validate", "checking the project")
    _running_dind_supervisor(req.project)
    cfg = load_config()
    progress.step("disable", "disabling the extension")
    _extension_disable(req.project, cfg, req.name)
    return ExtDisableResult(project=req.project, name=req.name)


def ext_list(req: "ExtListRequest", _progress=None) -> ExtListResult:  # type: ignore[name-defined]
    """The project's extension catalog + enabled set (live container state) + the
    project's allowed MCPs (to drive the enable dialog's upstream picker)."""
    supervisor = _running_dind_supervisor(req.project)
    cfg = load_config()
    workspace_path = workspace_path_for(req.project, cfg)
    entries = extension.load(workspace_path)
    states = _inner_container_states(supervisor)
    enabled = []
    for nm, e in sorted(entries.items()):
        cname = e.get("container") or extension.container_name(nm, e.get("kind"))
        enabled.append({
            "name": nm, "kind": e.get("kind"), "ip": e.get("ip"),
            "state": states.get(cname, "absent"),
            "upstream_source": e.get("upstream_source"),
            "upstream_mcps": list(e.get("upstream_mcps") or []),
        })
    allowed = sorted(x["name"] for x in load_project_allowlist(req.project, cfg)
                     if x.get("name"))
    return ExtListResult(project=req.project,
                         catalog=extension.catalog(load_versions()),
                         enabled=enabled, allowed_mcps=allowed)


def wire_webui_to_projects() -> None:
    """Connect rs-webui to every existing per-project network. Idempotent;
    no-op when the webui isn't running. Called at webui start and after
    every `project create` so the webui sees fresh projects without a
    restart."""
    if not container_exists(WEBUI_CONTAINER):
        return
    r = run(["docker", "network", "ls",
             "--filter", f"name=^{PROJECT_NETWORK_PREFIX}",
             "--format", "{{.Name}}"],
            capture_output=True)
    for net in r.stdout.strip().splitlines():
        if net:
            run(["docker", "network", "connect", net, WEBUI_CONTAINER],
                capture_output=True)


def wire_router_to_projects() -> None:
    """Connect rs-router to every existing per-project network. Idempotent;
    no-op when the router isn't running.

    `cmd_project_create` is the original wire-er (via ensure_project_network).
    This re-wirer exists for the case where the router container was rebuilt
    or recreated — compose's `up -d --build router` does `rm` + `run`, which
    drops every `docker network connect` to rs-net-<project> that prior
    creates set up. Without this, the next `project update` against an
    existing project dies at `get_router_ip` because the recreated rs-router
    isn't attached to that project's network.

    iptables state on the router IS recovered on its own: the router's
    entrypoint replays `/etc/sandbox/rules/*` on startup, and that directory
    lives on the named volume `rs-router-rules` which survives `docker rm`.
    So this helper only handles the network-attachment side of the recreate;
    the firewall side is self-healing."""
    if not container_running(ROUTER_CONTAINER):
        return
    r = run(["docker", "network", "ls",
             "--filter", f"name=^{PROJECT_NETWORK_PREFIX}",
             "--format", "{{.Name}}"],
            capture_output=True)
    for net in r.stdout.strip().splitlines():
        if net:
            run(["docker", "network", "connect", net, ROUTER_CONTAINER],
                capture_output=True)


def wire_registry_to_projects() -> None:
    """Re-attach rs-registry to every per-project network after a host
    `start` / registry recreate (mirror of wire_router_to_projects). LAZY: if the
    registry has never been created (no extension enabled yet) do nothing — it is
    stood up on first `enable`, not at `start`. If it exists but is stopped, start
    it, then re-attach. Without this, a `start` that recreated the registry would
    leave existing projects' inner dockerds unable to pull their extensions."""
    if not container_exists(REGISTRY_CONTAINER):
        return
    if not container_running(REGISTRY_CONTAINER):
        run_check(["docker", "start", REGISTRY_CONTAINER])
    r = run(["docker", "network", "ls",
             "--filter", f"name=^{PROJECT_NETWORK_PREFIX}",
             "--format", "{{.Name}}"],
            capture_output=True)
    for net in r.stdout.strip().splitlines():
        if net:
            run(["docker", "network", "connect", net, REGISTRY_CONTAINER],
                capture_output=True)


def _detect_tailscale_fqdn() -> str | None:
    """Read the host's tailnet FQDN from `tailscale status --json`. Returns
    None if tailscale isn't installed, the daemon isn't running, or the
    host hasn't joined a tailnet."""
    if shutil.which("tailscale") is None:
        return None
    r = run(["tailscale", "status", "--json"], capture_output=True)
    if r.returncode != 0:
        return None
    try:
        data = json.loads(r.stdout)
    except json.JSONDecodeError:
        return None
    fqdn = (data.get("Self") or {}).get("DNSName", "")
    return fqdn.rstrip(".") or None


def _webui_tls_volume() -> str:
    """Resolve the docker volume name actually mounted at /app/tls in
    rs-webui. Falls back to the literal name pinned in docker-compose.yml
    when rs-webui isn't running yet. This avoids a previous bug where the
    helper wrote to a bare `rs-webui-tls` volume while compose mounted a
    project-prefixed one (`research-sandbox_rs-webui-tls`)."""
    if container_exists(WEBUI_CONTAINER):
        r = run(["docker", "inspect", WEBUI_CONTAINER, "-f",
                 "{{range .Mounts}}{{if eq .Destination \"/app/tls\"}}"
                 "{{.Name}}{{end}}{{end}}"], capture_output=True)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    return "rs-webui-tls"


def _stage_webui_cert(cert_pem: bytes, key_pem: bytes,
                      provider: str) -> None:
    """Write cert+key+`.custom` marker into the webui's TLS volume. The
    marker tells the in-container ensure_tls() to skip its auto-regenerate-
    self-signed path so the user-provided cert sticks even when WEBUI_BIND
    doesn't appear in the cert's SAN."""
    volume = _webui_tls_volume()
    if not run_quiet(["docker", "volume", "inspect", volume]):
        run_check(["docker", "volume", "create", volume])
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        (td_path / "cert.pem").write_bytes(cert_pem)
        (td_path / "key.pem").write_bytes(key_pem)
        (td_path / ".custom").write_text(f"{provider}\n")
        # chown to UID 1000 — the in-container webui user. Without this
        # the busybox-written files are root-owned, and any regen attempt
        # (e.g. server.py falling back to self-signed when the marker is
        # absent) hits Permission denied on the cert.pem write and the
        # container restart-loops. chmod 644/600 then matches what the
        # auto-generated cert flow uses.
        run_check([
            "docker", "run", "--rm",
            "-v", f"{volume}:/tls",
            "-v", f"{td_path}:/src:ro",
            "busybox", "sh", "-c",
            "cp /src/cert.pem /tls/cert.pem && "
            "cp /src/key.pem  /tls/key.pem && "
            "cp /src/.custom  /tls/.custom && "
            "chown 1000:1000 /tls/cert.pem /tls/key.pem /tls/.custom && "
            "chmod 644 /tls/cert.pem /tls/.custom && "
            "chmod 600 /tls/key.pem"
        ])


def _webui_recreate_in_place() -> None:
    """Tear down + bring up the webui container so it re-reads the TLS
    volume on next start. No image rebuild; preserves WEBUI_BIND/PORT."""
    bind = read_env_value("WEBUI_BIND") or "127.0.0.1"
    port = read_env_value("WEBUI_PORT") or "7777"
    os.environ["WEBUI_BIND"] = bind
    os.environ["WEBUI_PORT"] = port
    if container_exists(WEBUI_CONTAINER):
        run(["docker", "rm", "-f", WEBUI_CONTAINER], capture_output=True)
    docker_compose("--profile", "webui", "up", "-d", "webui")
    wire_webui_to_projects()


# Re-export every module-level name (incl. _underscore helpers) so
# research.py's existing cmd_* call sites resolve via `from rscore import *`.
__all__ = [_n for _n in list(globals()) if not _n.startswith('__')]
