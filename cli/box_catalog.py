"""box_catalog — the box-preset catalog schema + store (STAGE_BOX_EXT_UX).

Stdlib only (the host CLI + broker import it; it must not drag docker/yaml in).
A *box preset* is what the "Add Box" window offers: a base image choice +
agent-default + bundled capability + baked instructions. This module is the data
layer — the JSON schema, its validator, and `load_catalog()` that merges the
in-repo built-ins with a host-side BYO registry (operator-defined box types,
F1→json-extendable). It mirrors `cli/workflow.py` deliberately.

A preset differs from a workflow in one structural way: its instruction TEXT is
catalog-driven and staged into a box at create (NOT baked per-image), so the only
image-level capability is the browser (base vs browser image). Built-ins keep
their instructions in a sibling `boxes/<name>.instructions.md` for readability;
the loader folds that text into the normalized entry's `instructions` key. A BYO
registry entry carries `instructions` inline (no sibling file).

Two on-disk shapes, ONE in-memory validator:
  • built-in file  boxes/<name>.json (+ optional boxes/<name>.instructions.md)
  • BYO registry   ~/.research-sandbox/box-registry.json — a {version, boxes}
    envelope keyed by name; entries do NOT repeat the name
`load_catalog()` normalizes both to a single list of name-bearing manifests
(injecting the registry key as `name`, folding the sibling .md into
`instructions`), each tagged with a non-schema `source` ('builtin'|'byo').
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

# Built-in presets are tracked product data at the repo root; the BYO registry is
# host-side, mirroring the MCP / workflow registries.
BUILTIN_DIR = Path(__file__).resolve().parent.parent / "boxes"
REGISTRY_DIR = Path.home() / ".research-sandbox"
REGISTRY_PATH = REGISTRY_DIR / "box-registry.json"

VERSION = 1

# A box preset selects one of the two existing box images (STAGE_DIND_UNIFY): the
# clean base, or the browser variant (Playwright + Chromium baked). Kept as bare
# strings — the image-level capability axis; lockstep with rs_sandbox.BOX_IMAGE /
# BOX_IMAGE_BROWSER (which map "base"/"browser" → the :latest tags).
IMAGES = ("base", "browser")

NAME_RE = re.compile(r"^[a-z][a-z0-9-]*$")

# Curated display order for the "Add box" window (the webui + the in-supervisor
# rs-sandbox render the catalog in list order). Built-ins sort by their index
# here; an unlisted built-in and every BYO operator type fall after, by name.
# Product order, not operator data — a constant here, not a manifest field.
_BUILTIN_ORDER = ("empty", "websearcher", "data-wrangler", "byo")

# `instructions` is folded in from the sibling .md (built-ins) or carried inline
# (BYO), so it is an allowed key on the normalized entry the validator sees.
_ALLOWED_KEYS = {"name", "image", "agent_default", "clone", "description",
                 "instructions"}


class BoxCatalogError(Exception):
    pass


def _is_str(v: Any) -> bool:
    return isinstance(v, str) and bool(v.strip())


def _validate_entry(name: Any, m: Any) -> list[str]:
    """Validate one normalized (name-bearing) box-preset manifest."""
    if not isinstance(m, dict):
        return [f"{name!r}: preset must be an object"]
    p = lambda msg: f"{name!r}: {msg}"
    out: list[str] = []

    nm = m.get("name")
    if not isinstance(nm, str) or not NAME_RE.match(nm or ""):
        out.append(p(f"name must match {NAME_RE.pattern!r}, got {nm!r}"))

    if m.get("image") not in IMAGES:
        out.append(p(f"image must be one of {IMAGES}, got {m.get('image')!r}"))

    for k in ("agent_default", "clone"):
        if not isinstance(m.get(k), bool):
            out.append(p(f"{k} must be a boolean, got {m.get(k)!r}"))

    # description is optional but, if present, must be a non-empty string.
    if "description" in m and not _is_str(m["description"]):
        out.append(p("description must be a non-empty string"))

    # instructions is optional and MAY be empty (the empty preset has none) —
    # but it must be a string when present.
    if "instructions" in m and not isinstance(m["instructions"], str):
        out.append(p("instructions must be a string"))

    extras = set(m) - _ALLOWED_KEYS
    if extras:
        out.append(p(f"unknown keys: {sorted(extras)}"))
    return out


def _validate_envelope(data: Any) -> list[str]:
    if not isinstance(data, dict):
        return ["registry root must be a JSON object"]
    errs: list[str] = []
    if data.get("version") != VERSION:
        errs.append(f"version must be {VERSION}, got {data.get('version')!r}")
    if not isinstance(data.get("boxes"), dict):
        errs.append("'boxes' must be an object")
    extras = set(data) - {"version", "boxes"}
    if extras:
        errs.append(f"registry unknown keys: {sorted(extras)}")
    return errs


def empty_registry() -> dict[str, Any]:
    return {"version": VERSION, "boxes": {}}


def load_builtins(builtin_dir: Path = BUILTIN_DIR) -> dict[str, dict]:
    """Read + validate every boxes/<name>.json built-in, folding the sibling
    boxes/<name>.instructions.md (if present) into `instructions`. Raises
    BoxCatalogError on a malformed file, a name/file mismatch, or a duplicate."""
    out: dict[str, dict] = {}
    if not builtin_dir.is_dir():
        return out
    for f in sorted(builtin_dir.glob("*.json")):
        try:
            m = json.loads(f.read_text())
        except json.JSONDecodeError as e:
            raise BoxCatalogError(f"builtin {f.name}: not valid JSON: {e}") from e
        if isinstance(m, dict) and "instructions" in m:
            raise BoxCatalogError(
                f"builtin {f.name}: 'instructions' belongs in the sibling "
                f"{f.stem}.instructions.md, not the JSON")
        sidecar = builtin_dir / f"{f.stem}.instructions.md"
        if isinstance(m, dict):
            m["instructions"] = sidecar.read_text() if sidecar.is_file() else ""
        errs = _validate_entry(f.stem, m)
        if errs:
            raise BoxCatalogError(f"builtin {f.name}: " + "; ".join(errs))
        nm = m["name"]
        if nm != f.stem:
            raise BoxCatalogError(
                f"builtin {f.name}: name {nm!r} must match filename stem {f.stem!r}")
        if nm in out:
            raise BoxCatalogError(f"duplicate built-in box preset {nm!r}")
        out[nm] = m
    return out


def load_registry(path: Path = REGISTRY_PATH) -> dict[str, dict]:
    """Read + validate the host-side BYO box registry. Missing file → {}. Entries
    are keyed by name and must NOT repeat it; the key is injected as `name` before
    per-entry validation so one validator serves both shapes."""
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        raise BoxCatalogError(f"registry not valid JSON: {path}: {e}") from e
    errs = _validate_envelope(data)
    if errs:
        raise BoxCatalogError("registry validation failed:\n  " + "\n  ".join(errs))
    out: dict[str, dict] = {}
    for name, entry in data["boxes"].items():
        if not isinstance(name, str) or not NAME_RE.match(name):
            raise BoxCatalogError(f"registry: invalid box name {name!r}")
        if isinstance(entry, dict) and "name" in entry:
            raise BoxCatalogError(
                f"registry: entry {name!r} must not repeat the 'name' field "
                "(entries are keyed by name)")
        m = dict(entry) if isinstance(entry, dict) else entry
        if isinstance(m, dict):
            m["name"] = name
        ev = _validate_entry(name, m)
        if ev:
            raise BoxCatalogError("registry: " + "; ".join(ev))
        out[name] = m
    return out


def load_catalog(builtin_dir: Path = BUILTIN_DIR,
                 registry_path: Path = REGISTRY_PATH) -> list[dict]:
    """The box-preset catalog: built-ins + BYO, normalized to a single list of
    manifests sorted by name, each tagged with a non-schema `source`
    ('builtin'|'byo'). A BYO name shadowing a built-in is an error, not a silent
    override (mirrors workflow.load_catalog)."""
    builtins = load_builtins(builtin_dir)
    byo = load_registry(registry_path)
    catalog: list[dict] = []
    for _nm, m in builtins.items():
        e = dict(m)
        e["source"] = "builtin"
        catalog.append(e)
    for nm, m in byo.items():
        if nm in builtins:
            raise BoxCatalogError(
                f"BYO box preset {nm!r} shadows a built-in of the same name; rename it")
        e = dict(m)
        e["source"] = "byo"
        catalog.append(e)
    def _key(e: dict) -> tuple:
        name = e["name"]
        rank = _BUILTIN_ORDER.index(name) if name in _BUILTIN_ORDER else len(_BUILTIN_ORDER)
        # is_byo first so an unlisted built-in still sorts before any BYO type.
        return (0 if e.get("source") == "builtin" else 1, rank, name)
    return sorted(catalog, key=_key)
