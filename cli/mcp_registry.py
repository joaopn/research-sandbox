"""mcp_registry — host-side MCP registry for the research-sandbox.

Stdlib only. Used by research.py (host) to read/write
~/.research-sandbox/mcp-registry.json, and by Stage 2.2's in-supervisor
consumers (rs-worker, supervisor entrypoint) once the file is bind-mounted
read-only.
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
REGISTRY_PATH = REGISTRY_DIR / "mcp-registry.json"
LOCK_PATH = REGISTRY_DIR / "mcp-registry.lock"

VERSION = 1
KINDS = ("external", "shared")
TRANSPORTS = ("http", "sse")
DEFAULT_PATH = "/mcp"
NAME_RE = re.compile(r"^[a-z][a-z0-9-]*$")
PATH_RE = re.compile(r"^/[A-Za-z0-9/_.-]*$")
_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)")


class RegistryError(Exception):
    pass


def empty() -> dict[str, Any]:
    return {"version": VERSION, "mcps": {}}


def load(expand: bool = True, path: Path = REGISTRY_PATH) -> dict[str, Any]:
    """Read + validate the registry. Returns an empty registry if the file
    is missing. With ``expand=True`` (default), ${VAR} and $VAR placeholders
    in string values are resolved against ``os.environ``; an unset variable
    raises RegistryError. Pass ``expand=False`` for cosmetic ops (list/show)."""
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
    return load(expand=expand)["mcps"].get(name)


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
    mcps = data.get("mcps")
    if not isinstance(mcps, dict):
        errs.append("'mcps' must be an object")
        return errs
    for name, entry in mcps.items():
        if not isinstance(name, str) or not NAME_RE.match(name):
            errs.append(f"{name!r}: invalid name (must match {NAME_RE.pattern!r})")
        errs.extend(_validate_entry(name, entry))
    return errs


def _validate_entry(name: str, e: Any) -> list[str]:
    p = lambda m: f"{name!r}: {m}"
    if not isinstance(e, dict):
        return [p("entry must be an object")]
    out: list[str] = []
    kind = e.get("kind")
    if kind not in KINDS:
        return [p(f"kind must be one of {KINDS}, got {kind!r}")]
    if e.get("transport") not in TRANSPORTS:
        out.append(p(f"transport must be one of {TRANSPORTS}, got {e.get('transport')!r}"))
    if "path" in e:
        path = e["path"]
        if not isinstance(path, str) or not PATH_RE.match(path) \
                or any(seg == ".." for seg in path.split("/")):
            out.append(p(f"path must match {PATH_RE.pattern!r} and contain no '..' segments, got {path!r}"))

    if "enabled" in e and not isinstance(e["enabled"], bool):
        out.append(p("enabled must be a boolean"))

    if "description" in e:
        d = e["description"]
        if not isinstance(d, str) or not d.strip():
            out.append(p("description must be a non-empty string"))

    if kind == "external":
        if not isinstance(e.get("host_port"), int) or isinstance(e.get("host_port"), bool):
            out.append(p("external entry needs integer host_port"))
        ha = e.get("host_address", "host.docker.internal")
        if not isinstance(ha, str) or not ha:
            out.append(p("host_address must be a non-empty string"))
        headers = e.get("headers", {})
        if not isinstance(headers, dict):
            out.append(p("headers must be an object"))
        else:
            for k, v in headers.items():
                if not isinstance(v, str):
                    out.append(p(f"header {k!r}: value must be a string"))
        allowed = {"kind", "transport", "host_port", "host_address", "headers", "path", "enabled", "description"}
    else:  # shared
        img = e.get("image")
        if not isinstance(img, str) or not img:
            out.append(p("shared entry needs a non-empty image string"))
        if not isinstance(e.get("port"), int) or isinstance(e.get("port"), bool):
            out.append(p("shared entry needs integer port"))
        env = e.get("env", {})
        if not isinstance(env, dict):
            out.append(p("env must be an object"))
        else:
            for k, v in env.items():
                if not isinstance(v, str):
                    out.append(p(f"env {k!r}: value must be a string"))
        allowed = {"kind", "transport", "image", "port", "env", "path", "enabled", "description"}

    extras = set(e) - allowed
    if extras:
        out.append(p(f"unknown keys: {sorted(extras)}"))
    return out


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
