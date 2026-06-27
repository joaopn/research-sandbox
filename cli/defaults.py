"""defaults — host-side default-enablement for the worker + extension surfaces.

Mirrors the MCP registry's ``enabled`` → auto-allow model for the two surfaces
whose catalogs are code constants (worker services in ``role_mcp.ROLE_IMAGES``,
baked sandboxes in ``sandbox.BAKED_IMAGES``) plus the BYO sandbox types — none
of which has a per-entry host file to carry a flag the way ``mcp-registry.json``
does. The "enabled flag" lives as a host-level set per surface:

    ~/.research-sandbox/defaults.json
    {"worker":  {"on": [...], "off": [...]},
     "extension": {"on": [...], "off": [...]}}

A name's default-enabled state = ``(BUILTIN ∪ on) − off``. ``BUILTIN`` ships
some entries on out of the box (the wrangler + websearcher worker services);
``on`` adds operator extras, ``off`` overrides a builtin back off. A
default-enabled entry is auto-applied to every NEW project at `project create`
(subject to per-project `--disable`); it's create-time only — `project update`
does not re-apply it.

**Baked mirror sandboxes follow their worker.** wrangler/websearcher baked
sandboxes have no independent default flag — they come up iff their worker twin
does (the worker enable auto-enables the extension mirror). So ``BUILTIN["extension"]``
is empty and the CLI refuses to default-flag a mirror sandbox, pointing at the
worker instead. Only echo (no twin) and BYO types are sandbox-default targets.

Stdlib only.
"""

from __future__ import annotations

import json
from pathlib import Path

REGISTRY_DIR = Path.home() / ".research-sandbox"
PATH = REGISTRY_DIR / "defaults.json"
SURFACES = ("worker", "extension")

# Entries shipped default-on. Workers: just websearcher — an image-baked
# browser that's useful in every project and needs no allowed upstreams.
# (wrangler is deliberately NOT default-on: without allowed DB MCPs it's an
# inert container; enable it per-project or globally with `worker enable`.)
# The sandbox surface ships none — baked mirror sandboxes ride along via the
# worker→sandbox auto-mirror, so their default follows the worker.
BUILTIN: dict[str, tuple[str, ...]] = {
    "worker": ("websearcher",),
    "extension": (),
}


def _empty() -> dict:
    return {s: {"on": [], "off": []} for s in SURFACES}


def load() -> dict[str, dict[str, list[str]]]:
    """Read the overrides file (on/off per surface). Tolerant of missing or
    malformed content so it never blocks `project create`."""
    out = _empty()
    if not PATH.is_file():
        return out
    try:
        data = json.loads(PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return out
    if isinstance(data, dict):
        for s in SURFACES:
            v = data.get(s)
            if isinstance(v, dict):
                for k in ("on", "off"):
                    lst = v.get(k)
                    if isinstance(lst, list):
                        out[s][k] = [n for n in lst if isinstance(n, str)]
    return out


def save(data: dict) -> None:
    """Atomic-rename write; normalize to sorted unique on/off per surface."""
    PATH.parent.mkdir(parents=True, exist_ok=True)
    clean = {s: {"on": sorted(set(data.get(s, {}).get("on", []))),
                 "off": sorted(set(data.get(s, {}).get("off", [])))}
             for s in SURFACES}
    tmp = PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(clean, indent=2, sort_keys=True) + "\n")
    tmp.replace(PATH)


def enabled(surface: str) -> list[str]:
    """Sorted default-enabled names for ``surface`` = (BUILTIN ∪ on) − off."""
    d = load()
    on = set(BUILTIN.get(surface, ())) | set(d[surface]["on"])
    on -= set(d[surface]["off"])
    return sorted(on)


def is_enabled(surface: str, name: str) -> bool:
    return name in enabled(surface)


def is_builtin(surface: str, name: str) -> bool:
    return name in BUILTIN.get(surface, ())


def set_enabled(surface: str, name: str, on: bool) -> None:
    if surface not in SURFACES:
        raise ValueError(f"unknown surface {surface!r}")
    d = load()
    cur_on, cur_off = set(d[surface]["on"]), set(d[surface]["off"])
    if on:
        cur_on.add(name)
        cur_off.discard(name)
    else:
        cur_on.discard(name)
        # Only a builtin needs an explicit 'off' to stay disabled; for a
        # non-builtin, dropping it from 'on' is enough.
        cur_off.add(name) if name in BUILTIN.get(surface, ()) else cur_off.discard(name)
    d[surface] = {"on": sorted(cur_on), "off": sorted(cur_off)}
    save(d)
