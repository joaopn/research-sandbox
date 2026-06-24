"""workflow — the workflow manifest schema + store catalog.

Stdlib only (the host CLI imports it; it must not drag docker/yaml deps). A
*workflow* is what the user ultimately launches: a declaration of `substrate +
payload (+ surfacing)`. This module is the data layer — the JSON schema, its
validator, and `load_catalog()` that merges the in-repo built-ins with a
host-side BYO registry. Nothing here touches container lifecycle; `create()`
wiring + the `--type` replacement + the rs-management rename are a LATER slice
(WORKFLOW_TAXONOMY — the breaking create-wiring pass), and the harness
(`repo+setup` / `image_overlay` execution), tab/mcp-export surfacing, and the
BYO `workflow add` writer are later still. So `tabs`/`mcp_exports`/`resources`
are *shape-validated but unconsumed* here, and there is intentionally no `lock()`
(it lands with the writer).

Two on-disk shapes, ONE in-memory validator:
  • built-in file  workflows/<name>.json  — a bare, name-BEARING manifest object
  • BYO registry   ~/.research-sandbox/workflow-registry.json — a {version,
    workflows} envelope keyed by name; entries do NOT repeat the name
`load_catalog()` normalizes both to one `{name: manifest}` map (injecting the
registry key as `name` for BYO entries), so `_validate_entry` only ever sees a
name-bearing dict — mirrors cli/pi_isolated_registry.py's keyed shape.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

# Built-in manifests are tracked product data at the repo root; the BYO registry
# is host-side, mirroring the MCP / sandbox registries.
BUILTIN_DIR = Path(__file__).resolve().parent.parent / "workflows"
REGISTRY_DIR = Path.home() / ".research-sandbox"
REGISTRY_PATH = REGISTRY_DIR / "workflow-registry.json"

VERSION = 1

# Substrate values mirror cli/rscore.py's Substrate enum. Duplicated as bare
# strings (not imported) to keep this module stdlib-light and independently
# importable; keep in lockstep with rscore.Substrate if a substrate is added.
SUBSTRATES = ("docker", "dind-sysbox")

# tabs[].kind mirrors the webui services.py contract; mcp_exports[].transport
# mirrors cli/mcp_registry.py's TRANSPORTS. Keep both in lockstep with those.
TAB_KINDS = ("http", "ssh")
EXPORT_TRANSPORTS = ("http", "sse")

# Per-flavor service defaults (STAGE_EDITOR_DIST slice 2): a manifest may declare
# `services: {<id>: <bool>}` to override the create-time default for a service —
# e.g. the `sandbox` workflow sets {"code-server": false} for a lean box. Mirror
# rscore.KNOWN_SERVICES / ALWAYS_ON_SERVICES (kept here as bare strings to keep
# this module stdlib-light + independently importable; lockstep with rscore).
SERVICE_IDS = ("supervisor", "code-server")
ALWAYS_ON_SERVICE_IDS = ("supervisor",)

NAME_RE = re.compile(r"^[a-z][a-z0-9-]*$")
# A surfacing path (tab path, proxy mount): absolute, no '..' segments — the
# same traversal guard pi_isolated_registry uses for container mount paths.
PATH_RE = re.compile(r"^/[A-Za-z0-9/_.-]*$")
# A TCP port: the kernel's 1–65535 range. Not a tuning knob — the protocol bound.
PORT_MIN, PORT_MAX = 1, 65535

_ALLOWED_KEYS = {"name", "substrate", "image_overlay", "repo", "ref", "setup",
                 "tabs", "mcp_exports", "resources", "services", "description"}
_TAB_KEYS = {"name", "port", "kind", "path"}
_EXPORT_KEYS = {"name", "port", "transport"}
_RESOURCE_KEYS = {"memory", "cpus"}


class WorkflowError(Exception):
    pass


# ---------------------------------------------------------------------------
# Validation — one entry-validator over a normalized, name-bearing manifest.
# ---------------------------------------------------------------------------


def _is_str(v: Any) -> bool:
    return isinstance(v, str) and bool(v.strip())


def _valid_port(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and PORT_MIN <= v <= PORT_MAX


def _validate_path(label: str, p: Any) -> list[str]:
    if not isinstance(p, str) or not PATH_RE.match(p) \
            or any(seg == ".." for seg in p.split("/")):
        return [f"{label}: must match {PATH_RE.pattern!r} with no '..' segments, got {p!r}"]
    return []


def _validate_tabs(name: str, tabs: Any) -> list[str]:
    out: list[str] = []
    if not isinstance(tabs, list):
        return [f"{name!r}: 'tabs' must be a list"]
    for i, t in enumerate(tabs):
        p = lambda m: f"{name!r}: tabs[{i}]: {m}"
        if not isinstance(t, dict):
            out.append(p("must be an object"))
            continue
        if not _is_str(t.get("name")):
            out.append(p("name must be a non-empty string"))
        if not _valid_port(t.get("port")):
            out.append(p(f"port must be an int in {PORT_MIN}–{PORT_MAX}"))
        if t.get("kind") not in TAB_KINDS:
            out.append(p(f"kind must be one of {TAB_KINDS}, got {t.get('kind')!r}"))
        out += [p(m) for m in _validate_path("path", t.get("path"))]
        extras = set(t) - _TAB_KEYS
        if extras:
            out.append(p(f"unknown keys: {sorted(extras)}"))
    return out


def _validate_mcp_exports(name: str, exports: Any) -> list[str]:
    out: list[str] = []
    if not isinstance(exports, list):
        return [f"{name!r}: 'mcp_exports' must be a list"]
    for i, x in enumerate(exports):
        p = lambda m: f"{name!r}: mcp_exports[{i}]: {m}"
        if not isinstance(x, dict):
            out.append(p("must be an object"))
            continue
        if not _is_str(x.get("name")):
            out.append(p("name must be a non-empty string"))
        if not _valid_port(x.get("port")):
            out.append(p(f"port must be an int in {PORT_MIN}–{PORT_MAX}"))
        if x.get("transport") not in EXPORT_TRANSPORTS:
            out.append(p(f"transport must be one of {EXPORT_TRANSPORTS}, got {x.get('transport')!r}"))
        extras = set(x) - _EXPORT_KEYS
        if extras:
            out.append(p(f"unknown keys: {sorted(extras)}"))
    return out


def _validate_resources(name: str, res: Any) -> list[str]:
    out: list[str] = []
    if not isinstance(res, dict):
        return [f"{name!r}: 'resources' must be an object"]
    for k in ("memory", "cpus"):
        if k in res and not _is_str(res[k]):
            out.append(f"{name!r}: resources.{k} must be a non-empty string")
    extras = set(res) - _RESOURCE_KEYS
    if extras:
        out.append(f"{name!r}: resources unknown keys: {sorted(extras)}")
    return out


def _validate_services(name: str, services: Any) -> list[str]:
    """A `services` override map: {<known service id>: <bool>}. Unknown ids and
    always-on services (which rscore forces True regardless) are config errors."""
    if not isinstance(services, dict):
        return [f"{name!r}: 'services' must be an object"]
    out: list[str] = []
    for k, v in services.items():
        if k not in SERVICE_IDS:
            out.append(f"{name!r}: services unknown id {k!r} (known: {SERVICE_IDS})")
        elif k in ALWAYS_ON_SERVICE_IDS:
            out.append(f"{name!r}: services cannot set always-on service {k!r}")
        if not isinstance(v, bool):
            out.append(f"{name!r}: services.{k} must be a boolean, got {v!r}")
    return out


def _validate_entry(name: Any, m: Any) -> list[str]:
    """Validate one normalized (name-bearing) manifest dict."""
    if not isinstance(m, dict):
        return [f"{name!r}: manifest must be an object"]
    p = lambda msg: f"{name!r}: {msg}"
    out: list[str] = []

    nm = m.get("name")
    if not isinstance(nm, str) or not NAME_RE.match(nm or ""):
        out.append(p(f"name must match {NAME_RE.pattern!r}, got {nm!r}"))

    if m.get("substrate") not in SUBSTRATES:
        out.append(p(f"substrate must be one of {SUBSTRATES}, got {m.get('substrate')!r}"))

    # Payload: image_overlay (bake) XOR repo/ref/setup (harness); neither is a
    # bare substrate, both is a configuration error.
    overlay = m.get("image_overlay")
    repo = m.get("repo")
    ref = m.get("ref")
    setup = m.get("setup")
    if overlay is not None and not _is_str(overlay):
        out.append(p("image_overlay must be a non-empty string or null"))
    if repo is not None and not _is_str(repo):
        out.append(p("repo must be a non-empty string or null"))
    if ref is not None and not _is_str(ref):
        out.append(p("ref must be a non-empty string or null"))
    if setup is not None and not _is_str(setup):
        out.append(p("setup must be a non-empty string or null"))
    has_overlay = _is_str(overlay)
    has_harness = _is_str(repo) or _is_str(setup)
    if has_overlay and has_harness:
        out.append(p("declare image_overlay OR repo/setup, not both (bake-vs-harness)"))
    if _is_str(repo) and not _is_str(ref):
        out.append(p("ref is required when repo is set (pin the clone — no drift)"))

    if "tabs" in m:
        out += _validate_tabs(nm if isinstance(nm, str) else str(name), m["tabs"])
    if "mcp_exports" in m:
        out += _validate_mcp_exports(nm if isinstance(nm, str) else str(name), m["mcp_exports"])
    if "resources" in m:
        out += _validate_resources(nm if isinstance(nm, str) else str(name), m["resources"])
    if "services" in m:
        out += _validate_services(nm if isinstance(nm, str) else str(name), m["services"])

    if "description" in m and not _is_str(m["description"]):
        out.append(p("description must be a non-empty string"))

    extras = set(m) - _ALLOWED_KEYS
    if extras:
        out.append(p(f"unknown keys: {sorted(extras)}"))
    return out


def _validate_envelope(data: Any) -> list[str]:
    """Validate the BYO registry wrapper (not its entries — load_registry does
    that on the normalized, name-injected dicts)."""
    if not isinstance(data, dict):
        return ["registry root must be a JSON object"]
    errs: list[str] = []
    if data.get("version") != VERSION:
        errs.append(f"version must be {VERSION}, got {data.get('version')!r}")
    if not isinstance(data.get("workflows"), dict):
        errs.append("'workflows' must be an object")
    extras = set(data) - {"version", "workflows"}
    if extras:
        errs.append(f"registry unknown keys: {sorted(extras)}")
    return errs


# ---------------------------------------------------------------------------
# Loaders.
# ---------------------------------------------------------------------------


def empty_registry() -> dict[str, Any]:
    return {"version": VERSION, "workflows": {}}


def load_builtins(builtin_dir: Path = BUILTIN_DIR) -> dict[str, dict]:
    """Read + validate every workflows/<name>.json built-in (bare name-bearing
    manifests). Raises WorkflowError on a malformed file, a name/file mismatch,
    or a duplicate name."""
    out: dict[str, dict] = {}
    if not builtin_dir.is_dir():
        return out
    for f in sorted(builtin_dir.glob("*.json")):
        try:
            m = json.loads(f.read_text())
        except json.JSONDecodeError as e:
            raise WorkflowError(f"builtin {f.name}: not valid JSON: {e}") from e
        errs = _validate_entry(f.stem, m)
        if errs:
            raise WorkflowError(f"builtin {f.name}: " + "; ".join(errs))
        nm = m["name"]
        if nm != f.stem:
            raise WorkflowError(
                f"builtin {f.name}: name {nm!r} must match filename stem {f.stem!r}")
        if nm in out:
            raise WorkflowError(f"duplicate built-in workflow name {nm!r}")
        out[nm] = m
    return out


def load_registry(path: Path = REGISTRY_PATH) -> dict[str, dict]:
    """Read + validate the host-side BYO registry. Missing file → {}. Entries
    are keyed by name and must NOT repeat it; the key is injected as `name`
    before per-entry validation so one validator serves both shapes."""
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        raise WorkflowError(f"registry not valid JSON: {path}: {e}") from e
    errs = _validate_envelope(data)
    if errs:
        raise WorkflowError("registry validation failed:\n  " + "\n  ".join(errs))
    out: dict[str, dict] = {}
    for name, entry in data["workflows"].items():
        if not isinstance(name, str) or not NAME_RE.match(name):
            raise WorkflowError(f"registry: invalid workflow name {name!r}")
        if isinstance(entry, dict) and "name" in entry:
            raise WorkflowError(
                f"registry: entry {name!r} must not repeat the 'name' field "
                "(entries are keyed by name)")
        m = dict(entry) if isinstance(entry, dict) else entry
        if isinstance(m, dict):
            m["name"] = name
        ev = _validate_entry(name, m)
        if ev:
            raise WorkflowError(f"registry: " + "; ".join(ev))
        out[name] = m
    return out


def load_catalog(builtin_dir: Path = BUILTIN_DIR,
                 registry_path: Path = REGISTRY_PATH) -> list[dict]:
    """The store catalog: built-ins + BYO, normalized to a single list of
    manifests sorted by name, each tagged with a non-schema `source`
    ('builtin'|'byo'). A BYO name shadowing a built-in is an error, not a silent
    override."""
    builtins = load_builtins(builtin_dir)
    byo = load_registry(registry_path)
    catalog: list[dict] = []
    for nm, m in builtins.items():
        e = dict(m)
        e["source"] = "builtin"
        catalog.append(e)
    for nm, m in byo.items():
        if nm in builtins:
            raise WorkflowError(
                f"BYO workflow {nm!r} shadows a built-in of the same name; rename it")
        e = dict(m)
        e["source"] = "byo"
        catalog.append(e)
    return sorted(catalog, key=lambda e: e["name"])


def payload_kind(m: dict) -> str:
    """One-word description of a manifest's payload for `workflow list`."""
    if _is_str(m.get("image_overlay")):
        return f"overlay:{m['image_overlay']}"
    if _is_str(m.get("repo")):
        return f"repo:{m['repo']}"
    return "bare"
