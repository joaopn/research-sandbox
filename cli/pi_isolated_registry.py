"""pi_isolated_registry — host-side registry of PI-isolated agent *types*.

Stdlib only. Used by research.py (host) to read/write
~/.research-sandbox/pi-isolated-registry.json.

A PI-isolated type is a reusable definition of an externally-authored,
skill-based agent harness (an arbitrary github repo, e.g. an LLM-wiki /
Zettelkasten / writing kit) plus the host folder it operates on. Unlike
the per-project-only role-MCP and PI-role registries (cli/role_mcp.py,
cli/pi.py), type definitions are reusable across projects — exactly like
the general MCP registry's external-capability definitions — so they live
host-side and are referenced by name at `project create|update --enable`.

The substrate is fully generic: clone `repo` at `ref`, run `setup`, mount
`<root>/<project>/` at `mount`, run claude. RS has zero knowledge of any
harness's conventions; no type is pre-baked — the registry ships empty.

Per-type fields:
  root        (required) host folder for the type; the per-project subdir
              <root>/<project>/ is the RW external mount. May contain ~ and
              ${VAR}; expanded by the consumer at mount-build time.
  repo        (optional) git URL cloned into the container at enable time.
              null/absent → a pure external-folder mount with no harness.
  ref         (required iff repo set) commit/tag checked out after clone.
              Pinning is mandatory — no silent upstream drift. The operator
              need not supply it: `pi-isolated add` without --ref resolves
              the repo's default-branch HEAD and stores that SHA, so a
              stored entry with a repo ALWAYS carries a concrete ref.
  setup       (optional) shell command run in the container after checkout
              (symlink skills, pip install, …). Harness's responsibility.
  mount       (optional) absolute container path for the external folder
              (default DEFAULT_MOUNT). Keep under /workspace/ so the agent's
              claude sees it as part of its workspace.
  description (optional) operator note; surfaced in `pi-isolated list`.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
from pathlib import Path
from typing import Any, Iterator

REGISTRY_DIR = Path.home() / ".research-sandbox"
# The BYO sandbox type registry. Renamed from pi-isolated-registry.json with
# the CLI-taxonomy refactor (greenfield — no existing registry to migrate);
# this module is now the BYO sub-component of cli/extension.py.
REGISTRY_PATH = REGISTRY_DIR / "extension-registry.json"
LOCK_PATH = REGISTRY_DIR / "extension-registry.lock"

VERSION = 1
DEFAULT_MOUNT = "/workspace/external"
NAME_RE = re.compile(r"^[a-z][a-z0-9-]*$")
# Container path: absolute, no '..' segments. Validated against the same
# traversal guard the MCP registry uses for upstream paths.
MOUNT_RE = re.compile(r"^/[A-Za-z0-9/_.-]*$")
_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)")

_ALLOWED_KEYS = {"root", "repo", "ref", "setup", "mount", "description"}


class RegistryError(Exception):
    pass


def empty() -> dict[str, Any]:
    return {"version": VERSION, "types": {}}


def load(expand: bool = True, path: Path = REGISTRY_PATH) -> dict[str, Any]:
    """Read + validate the registry. Returns an empty registry if the file
    is missing. With ``expand=True`` (default), ${VAR}/$VAR placeholders in
    string values are resolved against ``os.environ``; an unset variable
    raises RegistryError. ``~`` is NOT expanded here — it's a host path the
    consumer resolves with ``Path.expanduser()`` at mount-build time, so the
    stored value stays portable. Pass ``expand=False`` for cosmetic ops."""
    if not path.is_file():
        return empty()
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        raise RegistryError(f"registry not valid JSON: {path}: {e}") from e
    errs = validate(data)
    if errs:
        raise RegistryError("registry validation failed:\n  " + "\n  ".join(errs))
    return _expand(data) if expand else data


def save_atomic(data: dict[str, Any], path: Path = REGISTRY_PATH) -> None:
    errs = validate(data)
    if errs:
        raise RegistryError("registry validation failed:\n  " + "\n  ".join(errs))
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    tmp.replace(path)


def entry_for(name: str, expand: bool = True) -> dict[str, Any] | None:
    return load(expand=expand)["types"].get(name)


@contextlib.contextmanager
def lock() -> Iterator[None]:
    """Serialize concurrent registry edits across processes."""
    REGISTRY_DIR.mkdir(parents=True, exist_ok=True)
    fd = os.open(LOCK_PATH, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def validate(data: Any) -> list[str]:
    errs: list[str] = []
    if not isinstance(data, dict):
        return ["registry root must be a JSON object"]
    if data.get("version") != VERSION:
        errs.append(f"version must be {VERSION}, got {data.get('version')!r}")
    types = data.get("types")
    if not isinstance(types, dict):
        errs.append("'types' must be an object")
        return errs
    for name, entry in types.items():
        if not isinstance(name, str) or not NAME_RE.match(name):
            errs.append(f"{name!r}: invalid name (must match {NAME_RE.pattern!r})")
        errs.extend(_validate_entry(name, entry))
    return errs


def _validate_entry(name: str, e: Any) -> list[str]:
    p = lambda m: f"{name!r}: {m}"
    if not isinstance(e, dict):
        return [p("entry must be an object")]
    out: list[str] = []

    root = e.get("root")
    if not isinstance(root, str) or not root.strip():
        out.append(p("root must be a non-empty string (host folder)"))

    repo = e.get("repo")
    if repo is not None and (not isinstance(repo, str) or not repo.strip()):
        out.append(p("repo must be a non-empty string or null"))

    ref = e.get("ref")
    if ref is not None and (not isinstance(ref, str) or not ref.strip()):
        out.append(p("ref must be a non-empty string or null"))
    if isinstance(repo, str) and repo.strip() and not (isinstance(ref, str) and ref.strip()):
        out.append(p("ref is required when repo is set (pin the clone — no "
                     "silent upstream drift)"))

    setup = e.get("setup")
    if setup is not None and (not isinstance(setup, str) or not setup.strip()):
        out.append(p("setup must be a non-empty string or null"))

    if "mount" in e:
        mount = e["mount"]
        if not isinstance(mount, str) or not MOUNT_RE.match(mount) \
                or any(seg == ".." for seg in mount.split("/")):
            out.append(p(f"mount must match {MOUNT_RE.pattern!r} and contain no "
                         f"'..' segments, got {mount!r}"))

    if "description" in e:
        d = e["description"]
        if not isinstance(d, str) or not d.strip():
            out.append(p("description must be a non-empty string"))

    extras = set(e) - _ALLOWED_KEYS
    if extras:
        out.append(p(f"unknown keys: {sorted(extras)}"))
    return out


def mount_for(entry: dict[str, Any]) -> str:
    """The container path the external folder lands at. Defaults applied here
    so callers don't each re-implement the fallback."""
    m = entry.get("mount")
    return m if isinstance(m, str) and m else DEFAULT_MOUNT


def _expand(data: dict[str, Any]) -> dict[str, Any]:
    return _walk(data)


def _walk(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _walk(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_walk(v) for v in obj]
    if isinstance(obj, str):
        return _expand_str(obj)
    return obj


def _expand_str(s: str) -> str:
    def repl(m: "re.Match[str]") -> str:
        name = m.group(1) or m.group(2)
        v = os.environ.get(name)
        if v is None:
            raise RegistryError(f"environment variable not set: {name}")
        return v
    return _VAR_RE.sub(repl, s)
