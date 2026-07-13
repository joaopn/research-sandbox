"""model_catalog — the agent model/effort catalog + per-container-type defaults.

Stdlib only. The host CLI, `rscore`, and the broker all import it, so it must not
drag in docker/yaml — which is also why the operator-facing defaults file is JSON
and not YAML (PyYAML is not stdlib, and the broker imports `rscore` headless).

Two data files, two jobs:

  * ``models/catalog.json``   — which model tiers exist and what each supports.
    Aliases (``opus``/``sonnet``/``haiku``), not pinned model ids: an alias follows
    the vendor's current model for that tier, so this file does not go stale on
    every release. A tier with ``efforts: []`` does not accept an effort parameter
    at all (haiku is such a tier today).

  * ``models/defaults.json`` — the default (model, effort) pair for each container
    TYPE, layered exactly like ``versions.env``: this tracked file is the base and
    an untracked ``~/.research-sandbox/model-defaults.json`` overrides it per key.
    Types resolve independently; there is NO inheritance between them.

`load_defaults()` READS AND VALIDATES (mirroring `box_catalog.load_registry`) — it
is not a bare merge. That matters because the resolved pairs are frozen into a
project's `.orchestrator/project.json` at create and then read back VERBATIM by
`rs-worker` / `rs-sandbox` inside the supervisor, which have no catalog to check
them against (they are staged as standalone stdlib scripts; there is no `cli/`
package in the image). A typo in the operator's override file would otherwise be
merged, frozen, and emitted as `--model opuss` on every spawn, failing at agent
launch arbitrarily far from the file that caused it.

The same reasoning drives `resolve()`'s asymmetry: an effort the resolved model
cannot support is a hard error when the operator ASKED for it, and is silently
dropped when it merely came from a default — otherwise picking an effort-less tier
(haiku) for a type whose default effort is set would be impossible to express.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

# Catalog + base defaults are tracked product data at the repo root; the operator
# override is host-side, mirroring the other ~/.research-sandbox registries.
BUILTIN_DIR = Path(__file__).resolve().parent.parent / "models"
CATALOG_PATH = BUILTIN_DIR / "catalog.json"
DEFAULTS_PATH = BUILTIN_DIR / "defaults.json"
OVERRIDE_DIR = Path.home() / ".research-sandbox"
OVERRIDE_PATH = OVERRIDE_DIR / "model-defaults.json"

# The container types that run an agent. Each resolves its own pair; a box does
# NOT inherit the supervisor's, a worker does not inherit anything. Lockstep with
# the marker's `models` block and with rscore's per-type request fields.
TYPES = ("supervisor", "worker", "role", "box")

ALIAS_RE = re.compile(r"^[a-z][a-z0-9-]*$")

_MODEL_KEYS = {"alias", "label", "blurb", "efforts"}
_PAIR_KEYS = {"model", "effort"}


class ModelCatalogError(Exception):
    pass


def _strip_comments(d: dict) -> dict:
    """Drop `_`-prefixed keys so the data files can carry a `_comment` without
    tripping the unknown-key validators."""
    return {k: v for k, v in d.items() if not k.startswith("_")}


def _load_json(path: Path, label: str) -> Any:
    try:
        return json.loads(path.read_text())
    except FileNotFoundError as e:
        raise ModelCatalogError(f"{label} missing: {path}") from e
    except json.JSONDecodeError as e:
        raise ModelCatalogError(f"{label} is not valid JSON: {path}: {e}") from e


def load_catalog(path: Path | None = None) -> dict:
    """Read + validate the model catalog. Returns the normalized dict:
    ``{"efforts": [...], "models": [{alias,label,blurb,efforts}, ...]}`` with the
    model list in file order (the webui renders its options in that order).

    The path defaults at CALL time, not at import: a default-argument binding
    would freeze it into the function object, which is invisible in production
    (the path never changes) but makes the module untestable through its real
    callers — `_resolve_all_model_pairs` and the broker's `models` verb both call
    this with no arguments, so a test could not inject a fixture any other way."""
    path = path or CATALOG_PATH
    data = _load_json(path, "model catalog")
    if not isinstance(data, dict):
        raise ModelCatalogError(f"model catalog root must be an object: {path}")
    data = _strip_comments(data)

    extras = set(data) - {"efforts", "models"}
    if extras:
        raise ModelCatalogError(f"model catalog: unknown keys: {sorted(extras)}")

    efforts = data.get("efforts")
    if (not isinstance(efforts, list) or not efforts
            or not all(isinstance(e, str) and e.strip() for e in efforts)):
        raise ModelCatalogError("model catalog: 'efforts' must be a non-empty list of strings")

    models = data.get("models")
    if not isinstance(models, list) or not models:
        raise ModelCatalogError("model catalog: 'models' must be a non-empty list")

    seen: set[str] = set()
    for m in models:
        if not isinstance(m, dict):
            raise ModelCatalogError("model catalog: each model must be an object")
        me = set(m) - _MODEL_KEYS
        if me:
            raise ModelCatalogError(f"model catalog: unknown model keys: {sorted(me)}")
        alias = m.get("alias")
        if not isinstance(alias, str) or not ALIAS_RE.match(alias or ""):
            raise ModelCatalogError(
                f"model catalog: alias must match {ALIAS_RE.pattern!r}, got {alias!r}")
        if alias in seen:
            raise ModelCatalogError(f"model catalog: duplicate model alias {alias!r}")
        seen.add(alias)
        if not isinstance(m.get("label"), str) or not m["label"].strip():
            raise ModelCatalogError(f"model catalog: {alias!r}: label must be a non-empty string")
        if "blurb" in m and not isinstance(m["blurb"], str):
            raise ModelCatalogError(f"model catalog: {alias!r}: blurb must be a string")
        me_efforts = m.get("efforts")
        if not isinstance(me_efforts, list):
            raise ModelCatalogError(
                f"model catalog: {alias!r}: efforts must be a list "
                "(use [] for a tier that accepts no effort level)")
        unknown = [e for e in me_efforts if e not in efforts]
        if unknown:
            raise ModelCatalogError(
                f"model catalog: {alias!r}: unknown effort level(s) {unknown}; "
                f"must be drawn from {efforts}")
    return {"efforts": list(efforts), "models": list(models)}


def aliases(catalog: dict) -> list[str]:
    return [m["alias"] for m in catalog["models"]]


def supports_effort(alias: str, effort: str, catalog: dict) -> bool:
    """True iff `alias` accepts `effort`. An empty effort ("" = emit nothing) is
    supported by every model."""
    if not effort:
        return True
    for m in catalog["models"]:
        if m["alias"] == alias:
            return effort in m["efforts"]
    return False


def _validate_pair_block(label: str, ctype: str, block: Any, catalog: dict) -> None:
    if not isinstance(block, dict):
        raise ModelCatalogError(f"{label}: {ctype!r} must be an object")
    extras = set(block) - _PAIR_KEYS
    if extras:
        raise ModelCatalogError(f"{label}: {ctype!r}: unknown keys: {sorted(extras)}")
    model = block.get("model")
    if model is not None:
        if not isinstance(model, str) or model not in aliases(catalog):
            raise ModelCatalogError(
                f"{label}: {ctype!r}: unknown model {model!r}; "
                f"must be one of {aliases(catalog)}")
    effort = block.get("effort")
    if effort is not None:
        # "" is legal and means "emit no effort level for this type".
        if not isinstance(effort, str) or (effort and effort not in catalog["efforts"]):
            raise ModelCatalogError(
                f"{label}: {ctype!r}: unknown effort {effort!r}; "
                f"must be one of {catalog['efforts']} (or \"\" for none)")


def load_defaults(base_path: Path | None = None,
                  override_path: Path | None = None,
                  catalog: dict | None = None) -> dict[str, dict[str, str]]:
    """Read + VALIDATE the per-type defaults: the tracked base file, overridden by
    the operator's untracked file per key. The override may be partial at both
    levels — it may name a subset of the types, and a named type may set only
    `model` or only `effort` (the other half stays as the base's).

    Raises ModelCatalogError on malformed JSON, an unknown container-type key, an
    unknown model alias, or an unknown effort level — in EITHER file. This is a
    validating loader, not a merge: its output is frozen into a project's marker
    at create and read back verbatim inside the supervisor, where nothing can
    check it (see the module docstring).

    Paths default at CALL time, not at import — see load_catalog.
    """
    base_path = base_path or DEFAULTS_PATH
    override_path = override_path or OVERRIDE_PATH
    catalog = catalog or load_catalog()

    base = _load_json(base_path, "model defaults")
    if not isinstance(base, dict):
        raise ModelCatalogError(f"model defaults root must be an object: {base_path}")
    base = _strip_comments(base)

    missing = [t for t in TYPES if t not in base]
    if missing:
        raise ModelCatalogError(
            f"model defaults {base_path}: missing container type(s) {missing}; "
            f"the base file must define all of {list(TYPES)}")
    unknown = set(base) - set(TYPES)
    if unknown:
        raise ModelCatalogError(
            f"model defaults {base_path}: unknown container type(s) {sorted(unknown)}; "
            f"must be drawn from {list(TYPES)}")

    merged: dict[str, dict[str, str]] = {}
    for t in TYPES:
        _validate_pair_block(f"model defaults {base_path.name}", t, base[t], catalog)
        blk = base[t]
        if "model" not in blk or "effort" not in blk:
            raise ModelCatalogError(
                f"model defaults {base_path}: {t!r} must define both 'model' and 'effort'")
        merged[t] = {"model": blk["model"], "effort": blk["effort"]}

    if override_path.is_file():
        ov = _load_json(override_path, "model-defaults override")
        if not isinstance(ov, dict):
            raise ModelCatalogError(
                f"model-defaults override root must be an object: {override_path}")
        ov = _strip_comments(ov)
        unknown = set(ov) - set(TYPES)
        if unknown:
            raise ModelCatalogError(
                f"model-defaults override {override_path}: unknown container type(s) "
                f"{sorted(unknown)}; must be drawn from {list(TYPES)}")
        for t, blk in ov.items():
            _validate_pair_block(f"model-defaults override {override_path.name}",
                                 t, blk, catalog)
            # Per-field merge: a partial entry keeps the base's other half.
            merged[t].update({k: v for k, v in blk.items() if k in _PAIR_KEYS})

    return merged


def resolve(ctype: str, model: str = "", effort: str = "", *,
            catalog: dict | None = None,
            defaults: dict[str, dict[str, str]] | None = None) -> tuple[str, str]:
    """Resolve one container type's (model, effort) pair.

    `model`/`effort` are the caller's EXPLICIT choices; "" means "not specified —
    use the type's default". Returns a pair that is always valid together, so the
    result is safe to freeze into the project marker (which in-supervisor readers
    consume verbatim, with no catalog to check it against).

    The asymmetry is deliberate:
      * an explicitly-supplied effort the resolved model cannot support RAISES —
        the operator asked for something the tier does not have;
      * a DEFAULTED effort the resolved model cannot support is dropped to "" —
        otherwise choosing an effort-less tier (haiku) for a type whose default
        effort is set could not be expressed at all.
    """
    catalog = catalog or load_catalog()
    if defaults is None:
        defaults = load_defaults(catalog=catalog)
    if ctype not in TYPES:
        raise ModelCatalogError(f"unknown container type {ctype!r}; must be one of {list(TYPES)}")

    effort_explicit = bool(effort)
    d = defaults[ctype]
    m = model or d["model"]
    e = effort or d["effort"]

    if m not in aliases(catalog):
        raise ModelCatalogError(
            f"unknown model {m!r}; must be one of {aliases(catalog)}")
    if e and e not in catalog["efforts"]:
        raise ModelCatalogError(
            f"unknown effort level {e!r}; must be one of {catalog['efforts']}")

    if e and not supports_effort(m, e, catalog):
        if effort_explicit:
            raise ModelCatalogError(
                f"model {m!r} does not accept an effort level; "
                f"remove the effort setting for this agent")
        e = ""          # a merely-defaulted effort never blocks a model choice
    return m, e


def payload() -> dict:
    """Catalog + merged defaults, for the broker's `models` read verb (the webui
    has no access to `models/` — it is outside the scoped ./webui build context —
    so this is its only source, and it pre-selects its controls from `defaults`)."""
    catalog = load_catalog()
    return {"efforts": catalog["efforts"],
            "models": catalog["models"],
            "defaults": load_defaults(catalog=catalog),
            "types": list(TYPES)}
