"""mcp_paste.py — read a pasted MCP server configuration into drafts of the Add form.

The operator pastes what a server's README gives: a one-liner (`npx -y pkg`,
`uvx pkg`, `docker run … image`, `claude mcp add …`, `pip install pkg`) or
an mcp.json block (Claude Desktop / Claude Code `mcpServers`, VS Code
`servers`, Zed `context_servers`, opencode `mcp`, one bare entry). `read`
returns one DRAFT per server found: the fields of the existing add requests
(rscore.McpServerAddRequest for an address, rscore.McpRunAddRequest for an
image or a package) plus `kind` and `notes`. Nothing is saved here and
nothing pasted is ever run: the text is parsed with `json` and `shlex`
(POSIX) only, no shell, no process, and no address from the paste is
fetched. The only outside calls are the two registry lookups the caller
injects (`npm_manifest`, `pypi_version`), each called with a package name
that passed its registry's own name grammar.

Every part of the paste that could not be mapped becomes a note — on the
draft it belongs to, or on the reply when no draft came of it — never a
silent drop. A note is `{"about": <topic>, "part": <the pasted piece or "">,
"text": <one sentence>}`; the page shows `text`. Notes reach the browser:
they quote only what the operator pasted, and never name a host command.

Stdlib only (the broker imports it headless through rscore).
"""

from __future__ import annotations

import json
import re
import shlex
from typing import Any, Callable

import mcp_run
import mcp_servers

KIND_ADDRESS = "address"
KIND_IMAGE = "image"
KIND_PACKAGE = "package"

# --- note topics (what a note is about; tests key on these, the page on text) --
N_PARSE = "parse"            # the text as a whole could not be read
N_LINE = "line"              # lines that are not a server's configuration
N_ENTRY = "entry"            # an entry that names no package, image or address RS can use
N_KEY = "key"                # entry keys RS does not use
N_FLAG = "flag"              # an option RS does not use
N_PATH = "path"              # a path on the operator's machine (dropped)
N_MOUNT = "mount"            # a host folder mounted into an image
N_PORT = "port"              # a published port
N_TRANSPORT = "transport"    # a package told to serve HTTP itself (dropped)
N_STDIO = "stdio"            # an image that looks like it speaks only stdio
N_HTTP = "http"              # an image must serve MCP over HTTP (always on an image draft)
N_VERSION = "version"        # a given version that is not one exact version
N_LOOKUP = "lookup"          # a registry lookup that failed or ran out of time
N_COMMAND = "command"        # the command could not be told
N_PLACEHOLDER = "placeholder"  # values left empty because they were placeholders
N_QUERY = "query"            # an address's query, which RS does not keep
N_EXTRAS = "extras"          # PyPI extras, which RS does not install
N_SETTING = "setting"        # a setting that could not be taken
N_HEADER = "header"          # a header that could not be taken
N_SSE = "sse"                # an address that names the older SSE transport

# A quoted part is there so the operator can find it in what they pasted; one
# terminal line of it is enough to recognise it (the full text is still in the
# box), and it keeps a note one short sentence.
PART_MAX = 80
# The deepest nesting a configuration needs is four objects (VS Code's
# `mcp.servers.<name>.env`); a paste nested deeper than this is not one, and
# walking it stops there.
JSON_WALK_DEPTH = 4


# --- placeholders ---------------------------------------------------------------
# A value a README writes for the reader to replace, never a real value: the
# draft leaves it EMPTY so the operator types the real one. Each rule is a
# convention measured in real READMEs (2026-11; the samples behind the unit
# test's fixtures). One place, so a new convention is one line here.
PLACEHOLDER_RULES = (
    re.compile(r"\$\{[^}]*\}"),                       # ${VAR}, ${input:id}, ${workspaceFolder} (VS Code)
    re.compile(r"^\$[A-Za-z_][A-Za-z0-9_]*$"),        # $GITHUB_PAT: a shell variable the reader sets
    re.compile(r"<[^<>]*>"),                          # <your-token>, https://<your domain>
    re.compile(r"(?i)\byour[\s_-]"),                  # YOUR_API_KEY, fc-YOUR_API_KEY, your-api-key-here, Your Name
    re.compile(r"(?i)(?:^|[-_:])x{3,}"),              # xxx, sk-xxxx
    re.compile(r"\.\.\.|…|\*{3,}"),              # ntn_..., ntn_****, …
    re.compile(r"(?i)(?:^|[@./])example\.(?:com|org|net)\b"),  # you@example.com, grafana.example.com
    re.compile(r"(?i)(?:^|/)path/to/"),               # /path/to/file
)


def is_placeholder(value: str) -> bool:
    return value.strip() == "" or any(r.search(value) for r in PLACEHOLDER_RULES)


# A word in a server's arguments that names a place on the operator's machine.
# A server RS runs sees only its own data folder, so such a word is dropped
# (with the option it belongs to) and named in a note. `/projects`, `/certs/x`
# — folders inside an image — are not host paths.
_HOST_PATH_RULES = (
    re.compile(r"^(?:/Users|/home|/Volumes|/mnt|/absolute)/"),
    re.compile(r"^~(?:/|$)"),
    re.compile(r"^\.\.?/"),
    re.compile(r"^[A-Za-z]:(?![0-9])"),    # C:\x (and C:x once a shell line's \ are read)
    re.compile(r"\$\{[^}]*\}"),
    re.compile(r"(?i)(?:^|/|\{)path/to/"),
    re.compile(r"<[^<>]*>"),
)


def _host_path(word: str) -> bool:
    value = word.split("=", 1)[1] if word.startswith("-") and "=" in word else word
    return not value.startswith("-") and any(r.search(value) for r in _HOST_PATH_RULES)


# --- small helpers --------------------------------------------------------------

def _part(s: Any) -> str:
    s = s if isinstance(s, str) else json.dumps(s)
    return s if len(s) <= PART_MAX else s[:PART_MAX - 1] + "…"


def _note(notes: list, about: str, part: Any, text: str) -> None:
    notes.append({"about": about, "part": _part(part) if part != "" else "", "text": text})


def propose_name(raw: str) -> str:
    """A list name (^[a-z][a-z0-9-]*$) from a key, a package or an image."""
    s = re.sub(r"[^a-z0-9-]+", "-", raw.lower())
    s = re.sub(r"-{2,}", "-", s).strip("-")
    s = re.sub(r"^[^a-z]+", "", s)
    return s or "server"


def _name_from_npm(pkg: str) -> str:
    scope, _, base = pkg[1:].partition("/") if pkg.startswith("@") else ("", "", pkg)
    # `@playwright/mcp` → playwright-mcp: a bare `mcp` says nothing.
    return propose_name(f"{scope}-{base}" if scope and base in ("mcp", "server", "mcp-server")
                        else base)


def _name_from_image(image: str) -> str:
    repo = image.split("@", 1)[0]
    last = repo.rsplit("/", 1)[-1]
    return propose_name(last.split(":", 1)[0])


_URL_RE = re.compile(r"^https?://", re.I)
INSPECTOR = "@modelcontextprotocol/inspector"


def _name_from_url(url: Any) -> str:
    """mcp.tavily.com → tavily: the host's name below its top-level domain."""
    host = _URL_RE.sub("", url).split("/", 1)[0].split(":", 1)[0] \
        if isinstance(url, str) else ""
    labels = [x for x in host.split(".") if x]
    return propose_name(labels[-2] if len(labels) >= 2 else (labels[0] if labels else
                                                              "remote"))
_ASSIGN_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", re.S)
_HTTP_TYPES = frozenset({"http", "streamable-http", "streamablehttp", "sse", "remote"})


class _Lookups:
    """The injected registry lookups, each asked once per (registry, name, ref)
    in one read. A lookup raises on any failure; TimeoutError means it ran out
    of time."""

    def __init__(self, npm_manifest: Callable[[str, str], dict],
                 pypi_version: Callable[[str], str]):
        self._npm, self._pypi, self._memo = npm_manifest, pypi_version, {}

    def _ask(self, key: tuple, call: Callable[[], Any]) -> tuple[Any, str | None]:
        """(answer, None) or (None, "timeout" | "failed")."""
        if key not in self._memo:
            try:
                self._memo[key] = (call(), None)
            except TimeoutError:
                self._memo[key] = (None, "timeout")
            except Exception:  # any read failure: its text never reaches a note
                self._memo[key] = (None, "failed")
        return self._memo[key]

    def npm(self, pkg: str, ref: str):
        return self._ask(("npm", pkg, ref), lambda: self._npm(pkg, ref))

    def pypi(self, pkg: str):
        return self._ask(("pypi", pkg), lambda: self._pypi(pkg))


def _lookup_note(notes: list, why: str, pkg: str, what: str) -> None:
    when = " in time" if why == "timeout" else ""
    _note(notes, N_LOOKUP, pkg, f"the registry could not be asked{when} for the {what} "
          f"of {_part(pkg)}; fill it in")


# --- drafts ---------------------------------------------------------------------

def _settings(env: dict, notes: list) -> dict:
    """{NAME: {"secret": True, "value": v}}: every setting secret by default; a
    placeholder value is left empty."""
    out, blank = {}, []
    for k, v in env.items():
        if mcp_run.setting_name_problem(k):
            _note(notes, N_SETTING, k, f"the setting {_part(k)} does not have a name a "
                  "server's environment can carry, so it was left out")
            continue
        if isinstance(v, bool):
            v = "true" if v else "false"
        elif isinstance(v, (int, float)):
            v = json.dumps(v)
        elif not isinstance(v, str):
            _note(notes, N_SETTING, k, f"the value of the setting {_part(k)} is not text, "
                  "so it was left empty")
            v = ""
        if v and is_placeholder(v):
            blank.append(k)
            v = ""
        out[k] = {"secret": True, "value": v}
    if blank:
        _note(notes, N_PLACEHOLDER, ", ".join(blank),
              "these values were placeholders and are left empty: " + _part(", ".join(blank)))
    return out


_SCHEME_RE = re.compile(r"^(Bearer|Basic|Token)\s+(.+)$", re.I)


def _headers(headers: Any, notes: list) -> dict:
    if headers is None:
        return {}
    if not isinstance(headers, dict):
        _note(notes, N_HEADER, "headers", "the headers are not name and value pairs, so "
              "they were left out")
        return {}
    out, blank = {}, []
    for k, v in headers.items():
        if not isinstance(k, str) or not mcp_servers.HEADER_NAME_RE.fullmatch(k) \
                or not isinstance(v, str):
            _note(notes, N_HEADER, k, f"the header {_part(k)} could not be taken")
            continue
        m = _SCHEME_RE.match(v)
        if m and is_placeholder(m.group(2)):
            out[k] = m.group(1) + " "       # keep "Bearer ": the token goes after it
            blank.append(k)
        elif is_placeholder(v):
            out[k] = ""
            blank.append(k)
        else:
            out[k] = v
    if blank:
        _note(notes, N_PLACEHOLDER, ", ".join(blank),
              "these header values were placeholders and are left to fill in: "
              + _part(", ".join(blank)))
    return out


def _address(name: str, url: Any, headers: Any, description: str, notes: list,
             sse: bool = False) -> dict | None:
    if not isinstance(url, str) or not url.strip():
        _note(notes, N_ENTRY, name, f"the server {_part(name)} names no address")
        return None
    url = url.strip()
    if "?" in url or "#" in url:
        cut = min(i for i in (url.find("?"), url.find("#")) if i >= 0)
        _note(notes, N_QUERY, url[cut:], "the address carried a query, which RS does not "
              "keep; a key the server needs goes into a secret header")
        url = url[:cut]
    if is_placeholder(url):
        _note(notes, N_PLACEHOLDER, url, "the address holds a placeholder; fill in the "
              "real address")
    if sse:
        _note(notes, N_SSE, "sse", "the server is configured for the older SSE transport; "
              "RS calls servers over streamable HTTP, so Test whether it answers")
    return {"kind": KIND_ADDRESS, "name": name, "url": url, "description": description,
            "headers": _headers(headers, notes), "notes": notes}


def _run_common(name: str, args: list, env: dict, description: str, notes: list,
                data: str = "") -> dict:
    return {"name": name, "args": args, "data": data, "settings": _settings(env, notes),
            "headers": {}, "destinations": [], "description": description}


def _drop_host_paths(args: list, notes: list) -> list:
    out, dropped = [], []
    for w in args:
        if _host_path(w):
            if out and out[-1].startswith("-") and "=" not in out[-1] \
                    and not _host_path(out[-1]):
                dropped.append(out.pop())
            dropped.append(w)
        else:
            out.append(w)
    if dropped:
        _note(notes, N_PATH, " ".join(dropped), "a server RS runs sees only its own data "
              "folder, so the paths on your machine were left out of its arguments: "
              + _part(" ".join(dropped)))
    held = [w for w in out if is_placeholder(w)]
    if held:
        _note(notes, N_PLACEHOLDER, " ".join(held), "these arguments are placeholders; "
              "replace them: " + _part(" ".join(held)))
    return out


def _package_args(args: list, notes: list) -> list:
    """A package server is served by RS's own bridge, which talks to it over
    stdio: an option that makes it serve HTTP itself is dropped."""
    out, i = [], 0
    while i < len(args):
        w = args[i]
        if w == "--transport" and i + 1 < len(args):
            if args[i + 1].lower() != "stdio":
                _note(notes, N_TRANSPORT, f"{w} {args[i + 1]}", "RS serves a package server "
                      "itself and talks to it over stdio, so its own transport option "
                      f"{_part(w + ' ' + args[i + 1])} was left out")
                i += 2
                continue
            out.extend(args[i:i + 2])
            i += 2
            continue
        if w.startswith("--transport="):
            if w.split("=", 1)[1].lower() != "stdio":
                _note(notes, N_TRANSPORT, w, "RS serves a package server itself and talks "
                      f"to it over stdio, so {_part(w)} was left out")
                i += 1
                continue
        if w == "--port" or w.startswith("--port="):
            _note(notes, N_TRANSPORT, w, f"the option {_part(w)} may make the server serve "
                  "HTTP itself, while RS talks to a package server over stdio")
        out.append(w)
        i += 1
    return _drop_host_paths(out, notes)


def _npm_command(manifest: Any, pkg: str, notes: list) -> str:
    """The program the package installs: its `bin` named like the package, or
    its only one; several or none leave it empty with a note."""
    base = pkg.rsplit("/", 1)[-1]
    binv = manifest.get("bin") if isinstance(manifest, dict) else None
    if isinstance(binv, str):
        names = [base]
    elif isinstance(binv, dict):
        names = sorted(n for n in binv if isinstance(n, str))
    else:
        names = []
    names = [n for n in names if not mcp_run.command_problem(n)]
    if base in names:
        return base
    if len(names) == 1:
        return names[0]
    if names:
        _note(notes, N_COMMAND, ", ".join(names), "the package installs several programs ("
              + _part(", ".join(names)) + "); choose the one that serves MCP")
    else:
        _note(notes, N_COMMAND, pkg, "the package's record names no program; fill in the "
              "command it installs")
    return ""


def _npm_draft(name: str | None, spec: str, command: str | None, args: list, env: dict,
               description: str, notes: list, look: _Lookups) -> dict | None:
    """`command` None = the one the package's record names; "" = left for the
    operator (the caller has said why)."""
    at = spec.find("@", 1) if spec.startswith("@") else spec.find("@")
    pkg, ver = (spec[:at], spec[at + 1:]) if at > 0 else (spec, "")
    if mcp_run.package_problem("npm", pkg):
        _note(notes, N_ENTRY, spec, f"{_part(spec)} is not an npm package name (a path, an "
              "address or a git source cannot be run)")
        return None
    if pkg == "mcp-remote":
        return _mcp_remote(name, args, env, description, notes)
    if pkg == INSPECTOR and args:
        # The MCP inspector is a test client wrapped around the real command.
        return _from_words(name, args, env, description, notes, look)
    args = _package_args(args, notes)
    looked = ver in ("", "latest")
    exact = not looked and not mcp_run.version_problem("npm", ver)
    if not looked and not exact:
        _note(notes, N_VERSION, ver, f"the version {_part(ver)} is not one exact version; "
              "RS installs only an exact one, as in 1.2.3")
    manifest, why = (None, None)
    if looked or (exact and command is None):
        manifest, why = look.npm(pkg, "latest" if looked else ver)
        if why:
            _lookup_note(notes, why, pkg, "version and command" if looked else "command")
    version = ver
    if looked:
        version = ""
        v = manifest.get("version") if isinstance(manifest, dict) else None
        if isinstance(v, str) and not mcp_run.version_problem("npm", v):
            version = v
        elif manifest is not None:
            _note(notes, N_LOOKUP, pkg, "the registry's answer held no exact version; "
                  "fill it in")
    if command is None:
        if manifest is not None:
            command = _npm_command(manifest, pkg, notes)
        else:
            command = ""
            if not why:                    # an inexact version: nothing was asked
                _note(notes, N_COMMAND, pkg, "fill in the command the package installs")
    elif command and mcp_run.command_problem(command):
        _note(notes, N_COMMAND, command, f"{_part(command)} is not a program name; fill in "
              "the command the package installs")
        command = ""
    draft = {"kind": KIND_PACKAGE, "registry": "npm", "package": pkg, "version": version,
             "command": command}
    draft.update(_run_common(name or _name_from_npm(pkg), args, env, description, notes))
    draft["notes"] = notes
    return draft


def _mcp_remote(name: str | None, args: list, env: dict, description: str,
                notes: list) -> dict | None:
    """`npx mcp-remote <url> [--header "Name: value"]…`: a stdio proxy in front
    of a server at an address — the address is the draft."""
    url, headers, i = None, {}, 0
    while i < len(args):
        w = args[i]
        if w == "--header" and i + 1 < len(args):
            hn, sep, hv = args[i + 1].partition(":")
            if sep:
                headers[hn.strip()] = hv.strip()
            else:
                _note(notes, N_HEADER, args[i + 1], "a header could not be read")
            i += 2
            continue
        if url is None and not w.startswith("-"):
            url = w
        else:
            _note(notes, N_FLAG, w, f"the option {_part(w)} was not used")
        i += 1
    if env:
        _note(notes, N_SETTING, ", ".join(env), "an address server takes no settings; a "
              "value its headers need is to be typed into the header")
    return _address(name or _name_from_url(url), url, headers, description, notes)


_PYPI_SPEC_RE = re.compile(r"^([A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)(\[[^\]]*\])?(.*)$")


def _pypi_draft(name: str | None, spec: str, command: str | None, args: list, env: dict,
                description: str, notes: list, look: _Lookups) -> dict | None:
    m = _PYPI_SPEC_RE.match(spec)
    if not m or "://" in spec or spec.startswith(("git+", ".", "/")):
        _note(notes, N_ENTRY, spec, f"{_part(spec)} is not a PyPI package name (a path, an "
              "address or a git source cannot be run)")
        return None
    pkg, extras, rest = m.group(1), m.group(2), m.group(3).strip()
    if extras:
        _note(notes, N_EXTRAS, extras, f"RS installs the package without its extras "
              f"{_part(extras)}")
    if rest.startswith("=="):
        ver = rest[2:].strip()
    elif rest.startswith("@"):
        ver = rest[1:].strip()
    else:
        ver = rest
    looked = ver in ("", "latest")
    if not looked and mcp_run.version_problem("pypi", ver):
        _note(notes, N_VERSION, ver, f"the version {_part(ver)} is not one exact version; "
              "RS installs only an exact one, as in 1.2.3")
    version = ver
    if looked:
        version = ""
        v, why = look.pypi(pkg)
        if why:
            _lookup_note(notes, why, pkg, "version")
        elif isinstance(v, str) and not mcp_run.version_problem("pypi", v):
            version = v
        else:
            _note(notes, N_LOOKUP, pkg, "the registry's answer held no exact version; "
                  "fill it in")
    if command is None:
        command = pkg           # what uvx runs when no --from names another
    if command and mcp_run.command_problem(command):
        _note(notes, N_COMMAND, command, f"{_part(command)} is not a program name; fill in "
              "the command the package installs")
        command = ""
    draft = {"kind": KIND_PACKAGE, "registry": "pypi", "package": pkg, "version": version,
             "command": command}
    draft.update(_run_common(name or propose_name(pkg), _package_args(args, notes), env,
                             description, notes))
    draft["notes"] = notes
    return draft


def _npx(name, rest: list, env: dict, description: str, notes: list, look: _Lookups):
    i, pkg_opt = 0, None
    while i < len(rest) and rest[i].startswith("-"):
        f = rest[i]
        if f == "--":
            i += 1
            break
        if f in ("-y", "--yes", "-q", "--quiet", "--silent"):
            i += 1
        elif f in ("-p", "--package") and i + 1 < len(rest):
            pkg_opt, i = rest[i + 1], i + 2
        elif f.startswith("--package="):
            pkg_opt, i = f.split("=", 1)[1], i + 1
        elif f in ("--prefix", "--registry", "--cache", "-c", "--call") and i + 1 < len(rest):
            _note(notes, N_FLAG, f"{f} {rest[i + 1]}", f"the option {_part(f)} was not used")
            i += 2
        else:
            _note(notes, N_FLAG, f, f"the option {_part(f)} was not used")
            i += 1
    if pkg_opt is not None:
        if i >= len(rest):
            _note(notes, N_COMMAND, pkg_opt, "no command follows the package; fill it in")
            return _npm_draft(name, pkg_opt, "", [], env, description, notes, look)
        return _npm_draft(name, pkg_opt, rest[i], rest[i + 1:], env, description, notes, look)
    if i >= len(rest):
        _note(notes, N_ENTRY, "npx", "no package follows npx")
        return None
    return _npm_draft(name, rest[i], None, rest[i + 1:], env, description, notes, look)


# uvx options: those about uv's own cache say nothing about the server;
# the others change what is installed or run, which RS decides itself.
_UVX_SILENT = frozenset({"-q", "--quiet", "--refresh", "--isolated", "--no-cache",
                         "-n", "--offline"})
_UVX_SILENT_VALUE = frozenset({"--refresh-package"})
_UVX_VALUE = frozenset({"--with", "--with-editable", "--with-requirements", "--python",
                        "-p", "--index", "--index-url", "--extra-index-url",
                        "--constraints", "-c", "--directory", "--env-file"})


def _uvx(name, rest: list, env: dict, description: str, notes: list, look: _Lookups):
    i, frm = 0, None
    while i < len(rest) and rest[i].startswith("-"):
        f = rest[i]
        if f == "--":
            i += 1
            break
        if f == "--from" and i + 1 < len(rest):
            frm, i = rest[i + 1], i + 2
        elif f.startswith("--from="):
            frm, i = f.split("=", 1)[1], i + 1
        elif f in _UVX_SILENT:
            i += 1
        elif f in _UVX_SILENT_VALUE and i + 1 < len(rest):
            i += 2
        elif f in _UVX_VALUE and i + 1 < len(rest):
            _note(notes, N_FLAG, f"{f} {rest[i + 1]}", f"the option {_part(f)} was not "
                  "used: RS installs only the package itself, on its own Python")
            i += 2
        else:
            _note(notes, N_FLAG, f, f"the option {_part(f)} was not used")
            i += 1
    if i >= len(rest):
        _note(notes, N_ENTRY, "uvx", "no package follows uvx")
        return None
    if frm is not None:
        return _pypi_draft(name, frm, rest[i], rest[i + 1:], env, description, notes, look)
    return _pypi_draft(name, rest[i], None, rest[i + 1:], env, description, notes, look)


# docker run options that take a value (the subset READMEs use, and those whose
# value must not be mistaken for the image).
_DOCKER_VALUE = frozenset({
    "-e", "--env", "--env-file", "-v", "--volume", "--mount", "-p", "--publish",
    "--name", "--pull", "--entrypoint", "-u", "--user", "-w", "--workdir", "--network",
    "--net", "-l", "--label", "--platform", "--add-host", "--cap-add", "--cap-drop",
    "-m", "--memory", "--cpus", "--device", "--dns", "-h", "--hostname", "--restart",
    "--security-opt", "--shm-size", "--ulimit", "--tmpfs", "--expose", "--log-driver",
    "--log-opt", "--ipc", "--pid", "--gpus", "--runtime", "--stop-signal", "--group-add"})
# Options that change nothing about what RS runs: RS starts, names, pulls and
# removes the server's container itself.
_DOCKER_SILENT = frozenset({"-i", "-t", "-d", "--rm", "--init", "--interactive", "--tty",
                            "--detach", "--name", "--pull"})
_VOLUME_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


def _docker(name, rest: list, env0: dict, description: str, notes: list):
    env, data, i, flags, published = {}, "", 0, set(), []

    def take_data(dst: str, part: str) -> None:
        nonlocal data
        if data or mcp_run.data_problem(dst):
            _note(notes, N_MOUNT, part, f"the volume {_part(part)} was not used; a server RS "
                  "runs keeps one data folder")
        else:
            data = dst

    while i < len(rest) and rest[i].startswith("-"):
        f = rest[i]
        if f == "--":
            i += 1
            break
        opt, eq, val = f.partition("=") if f.startswith("--") else (f, "", "")
        if not eq and opt in _DOCKER_VALUE:
            if i + 1 >= len(rest):
                _note(notes, N_FLAG, f, f"the option {_part(f)} has no value")
                i += 1
                break
            val, i = rest[i + 1], i + 2
        else:
            i += 1
        if re.fullmatch(r"-[itd]{2,3}", opt):     # -it, -dit
            flags.update("-" + c for c in opt[1:])
            continue
        if opt in _DOCKER_SILENT:
            flags.add(opt)
            continue
        if opt in ("-e", "--env"):
            k, sep, v = val.partition("=")
            if sep:
                env[k] = v
            else:
                env[k] = env0.get(k, "")
        elif opt in ("-v", "--volume"):
            src, _, dst = val.partition(":")
            dst = dst.split(":", 1)[0]
            if _VOLUME_NAME_RE.fullmatch(src) and dst:
                take_data(dst, val)
            else:
                _note(notes, N_MOUNT, val, f"the host folder {_part(val)} is not mounted: "
                      "a server RS runs sees only its own data folder")
        elif opt == "--mount":
            parts = dict(p.partition("=")[::2] for p in val.split(","))
            dst = parts.get("dst") or parts.get("destination") or parts.get("target") or ""
            if parts.get("type", "volume") == "volume" and dst:
                take_data(dst, val)
            else:
                _note(notes, N_MOUNT, val, f"the host folder {_part(val)} is not mounted: "
                      "a server RS runs sees only its own data folder")
        elif opt in ("-p", "--publish"):
            published.append(val)
            _note(notes, N_PORT, val, f"the paste publishes {_part(val)}; RS publishes "
                  "nothing, fill in the port the server listens on inside its container")
        else:
            _note(notes, N_FLAG, f"{opt} {val}".strip(), f"the option {_part(opt)} was not "
                  "used")
    if i >= len(rest):
        _note(notes, N_ENTRY, "docker", "no image follows the options")
        return None
    image, args = rest[i], rest[i + 1:]
    if mcp_run.image_problem(image):
        _note(notes, N_ENTRY, image, f"{_part(image)} is not an image reference")
        return None
    for k, v in env0.items():
        env.setdefault(k, v)
    low = [a.lower() for a in args]
    # stdin kept open and no port published is how a stdio server is run.
    stdio = "stdio" in low or "--transport=stdio" in low \
        or (bool(flags & {"-i", "--interactive"}) and not published)
    if stdio:
        _note(notes, N_STDIO, "stdio", "the paste runs the image over stdio; RS runs an "
              "image only when it serves MCP over HTTP, so check that this image can")
    _note(notes, N_HTTP, image, "RS runs an image as an HTTP server: fill in the port and "
          "the path it serves MCP on")
    draft = {"kind": KIND_IMAGE, "image": image, "port": None, "path": None}
    draft.update(_run_common(name or _name_from_image(image), _drop_host_paths(args, notes),
                             env, description, notes, data))
    draft["notes"] = notes
    return draft


def _from_words(name: str | None, words: list, env: dict, description: str, notes: list,
                look: _Lookups) -> dict | None:
    """A draft from a command and its arguments, or None (with a note)."""
    if len(words) > 1 and words[0].lower() in ("cmd", "cmd.exe") and words[1].lower() == "/c":
        words = words[2:]          # Windows' wrapper around the same command
    if not words:
        _note(notes, N_ENTRY, name or "", "the entry names no command")
        return None
    w0 = words[0]
    if _URL_RE.match(w0):
        return _address(name or "remote", w0, None, description, notes)
    if w0 == "npx":
        return _npx(name, words[1:], env, description, notes, look)
    if w0 == "uvx":
        return _uvx(name, words[1:], env, description, notes, look)
    if words[:3] == ["uv", "tool", "run"]:
        return _uvx(name, words[3:], env, description, notes, look)
    if w0 == "docker" and words[1:2] == ["run"]:
        return _docker(name, words[2:], env, description, notes)
    _note(notes, N_ENTRY, w0, f"the server runs {_part(w0)}, a program on your machine; RS "
          "runs a server from an npm or PyPI package, an image, or uses its address")
    return None


# --- install lines ----------------------------------------------------------------

# Install options that say nothing about the package (where and how pip/npm
# install it), and those whose next word is their value, not a package.
_INSTALL_SILENT = frozenset({"-g", "--global", "-U", "--upgrade", "--user", "-q", "--quiet"})
_INSTALL_VALUE = frozenset({"-p", "--python", "--with", "-w", "-i", "--index-url",
                            "--extra-index-url", "--index", "-r", "--requirement", "-c",
                            "--constraint", "--prefix", "--target", "-t", "--registry"})

def _install(words: list, env: dict, notes: list, look: _Lookups) -> dict | None | bool:
    """`pip install X`, `pipx install X`, `uv tool install X`, `npm install -g X`
    → a package draft whose command the operator names (False: not an install
    line)."""
    if words[:2] in (["pip", "install"], ["pip3", "install"], ["pipx", "install"]):
        reg, rest = "pypi", words[2:]
    elif words[:3] in (["uv", "tool", "install"], ["uv", "pip", "install"]) \
            or words[:4] == ["python", "-m", "pip", "install"]:
        reg, rest = "pypi", words[3:] if words[0] == "uv" else words[4:]
    elif words[:2] in (["npm", "install"], ["npm", "i"]):
        reg, rest = "npm", words[2:]
    else:
        return False
    specs, i = [], 0
    while i < len(rest):
        w = rest[i]
        if not w.startswith("-"):
            specs.append(w)
        elif w not in _INSTALL_SILENT:
            took = w in _INSTALL_VALUE and i + 1 < len(rest)
            _note(notes, N_FLAG, f"{w} {rest[i + 1]}" if took else w,
                  f"the option {_part(w)} was not used")
            i += 1 if took else 0
        i += 1
    if not specs:
        _note(notes, N_ENTRY, words[0], "no package follows the install")
        return None
    if len(specs) > 1:
        _note(notes, N_ENTRY, " ".join(specs[1:]), "only the first package was taken: "
              + _part(" ".join(specs[1:])))
    if reg == "npm":
        return _npm_draft(None, specs[0], None, [], env, "", notes, look)
    _note(notes, N_COMMAND, specs[0], "an install line does not say which program serves "
          "MCP; fill in the command the package installs")
    return _pypi_draft(None, specs[0], "", [], env, "", notes, look)


# --- JSON -------------------------------------------------------------------------

def _outside_strings(text: str, step: Callable[[str, int], tuple[str, int]]) -> str:
    """Copy JSON strings untouched; hand every other position to `step`, which
    returns (what to emit, the next position)."""
    out, i, n = [], 0, len(text)
    while i < n:
        if text[i] == '"':
            j = i + 1
            while j < n and text[j] != '"':
                j += 2 if text[j] == "\\" else 1
            out.append(text[i:j + 1])
            i = j + 1
        else:
            emit, i = step(text, i)
            out.append(emit)
    return "".join(out)


def _no_comment(text: str, i: int) -> tuple[str, int]:
    if text.startswith("//", i):
        end = text.find("\n", i)
        return "", len(text) if end < 0 else end
    if text.startswith("/*", i):
        end = text.find("*/", i + 2)
        return " ", len(text) if end < 0 else end + 2
    return text[i], i + 1


def _no_trailing_comma(text: str, i: int) -> tuple[str, int]:
    if text[i] == ",":
        j = i + 1
        while j < len(text) and text[j] in " \t\r\n":
            j += 1
        if j < len(text) and text[j] in "}]":
            return "", i + 1
    return text[i], i + 1


def _strip_jsonc(text: str) -> str:
    """JSON with comments (`//`, `/* */`), trailing commas and `...` elision
    lines — what VS Code's files and READMEs hold (measured: VS Code and
    Grafana's blocks carry comments, Brave's and Exa's trailing commas,
    GitHub's `...` lines) — as plain JSON. Strings are copied untouched; a
    line holding only `...` can never be inside one (a JSON string holds no
    raw line break)."""
    s = _outside_strings(text, _no_comment)
    s = re.sub(r"(?m)^\s*(?:\.\.\.|…),?\s*$", "", s)
    return _outside_strings(s, _no_trailing_comma)


def _load_json(text: str) -> tuple[Any, str | None]:
    """(object, None) or (None, why)."""
    plain = _strip_jsonc(text)
    attempts = [plain]
    if plain.lstrip().startswith('"'):
        attempts.append("{" + plain + "}")     # a fragment: `"mcpServers": {…}`
    why = None
    for a in attempts:
        try:
            return json.loads(a), None
        except json.JSONDecodeError as e:
            why = why or f"line {e.lineno}"
        except (ValueError, RecursionError):
            why = why or "it is nested too deeply"
    return None, why


_ENTRY_KEYS = frozenset({"command", "args", "env", "type", "url", "serverUrl", "httpUrl",
                         "headers", "name", "description"})
_CONTAINER_KEYS = frozenset({"mcpServers", "servers", "context_servers", "mcp_servers"})
# Keys beside the servers that hold no server: VS Code's prompts for its own
# ${input:…} placeholders (left empty here) and a schema pointer.
_SKIP_KEYS = frozenset({"inputs", "$schema"})


def _is_entry(v: Any) -> bool:
    return isinstance(v, dict) and any(k in v for k in ("command", "url", "serverUrl",
                                                         "httpUrl"))


def _json_specs(obj: Any, notes: list, depth: int = 0) -> list:
    """[(proposed name | None, entry)] found in a parsed paste."""
    if _is_entry(obj):
        nm = obj.get("name") if isinstance(obj.get("name"), str) else None
        return [(nm, obj)]
    if not isinstance(obj, dict) or depth >= JSON_WALK_DEPTH:
        _note(notes, N_PARSE, "", "the text holds no server configuration")
        return []
    found = []
    values_are_entries = obj and all(_is_entry(v) for v in obj.values())
    for k, v in obj.items():
        if k in _SKIP_KEYS:
            continue
        if (k in _CONTAINER_KEYS or k.endswith(".mcpServers")) and isinstance(v, dict):
            for kk, vv in v.items():
                if _is_entry(vv):
                    found.append((kk, vv))
                else:
                    _note(notes, N_ENTRY, kk, f"the entry {_part(kk)} names no command or "
                          "address")
        elif k == "mcp" and isinstance(v, dict):
            found.extend(_json_specs(v, notes, depth + 1) if not _is_entry(v) else [(k, v)])
        elif values_are_entries or (len(obj) == 1 and _is_entry(v)):
            found.append((k, v))           # an object of entries, or one named entry
        else:
            _note(notes, N_KEY, k, f"the key {_part(k)} holds no server and was not read")
    if not found and not any(n["about"] in (N_ENTRY, N_KEY) for n in notes):
        _note(notes, N_PARSE, "", "the text holds no server configuration")
    return found


def _from_entry(key: str | None, entry: dict, look: _Lookups) -> tuple[dict | None, list]:
    notes: list = []
    nm = propose_name(key) if key else None
    unknown = sorted(set(entry) - _ENTRY_KEYS)
    if unknown:
        _note(notes, N_KEY, ", ".join(unknown), "RS does not use these keys of the entry: "
              + _part(", ".join(unknown)))
    desc = entry.get("description")
    desc = desc.strip() if isinstance(desc, str) and not any(c in desc for c in "\r\n\0") \
        else ""
    typ = entry.get("type")
    typ = typ.lower() if isinstance(typ, str) else ""
    url = next((entry[k] for k in ("url", "serverUrl", "httpUrl") if k in entry), None)
    if url is not None or typ in _HTTP_TYPES:
        if "command" in entry:
            _note(notes, N_KEY, "command", "the entry names both an address and a command; "
                  "the address was taken")
        return _address(nm or "remote", url, entry.get("headers"), desc, notes,
                        sse=typ == "sse"), notes
    command, args, env = entry.get("command"), entry.get("args"), entry.get("env")
    if isinstance(command, dict):                  # Zed: {"path", "args", "env"}
        command, args, env = command.get("path"), command.get("args"), command.get("env")
    if args is None:
        args = []
    if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
        _note(notes, N_KEY, "args", "the arguments are not a list of words, so they were "
              "left out")
        args = []
    if env is None:
        env = {}
    if not isinstance(env, dict):
        _note(notes, N_KEY, "env", "the settings are not name and value pairs, so they were "
              "left out")
        env = {}
    if isinstance(command, list) and command and all(isinstance(c, str) for c in command):
        words = command + args                     # opencode: the command as a list
    elif isinstance(command, str) and command.strip():
        try:
            words = (shlex.split(command) if not args and " " in command.strip()
                     else [command]) + args
        except ValueError:
            _note(notes, N_ENTRY, command, "the command could not be read")
            return None, notes
    else:
        _note(notes, N_ENTRY, key or "", "the entry names no command")
        return None, notes
    if "headers" in entry:
        _note(notes, N_KEY, "headers", "headers belong to an address; a server RS runs "
              "takes settings")
    return _from_words(nm, words, env, desc, notes, look), notes


# --- command lines ------------------------------------------------------------------

# The MCP clients whose README one-liner is `<client> mcp add [options] NAME …`.
CLIENT_CLIS = frozenset({"claude", "codex", "gemini", "amp", "grok", "droid", "qwen"})
_CLIENT_VALUE = frozenset({"-s", "--scope", "-t", "--transport", "-H", "--header", "--url",
                           "-e", "--env", "--bearer-token-env-var", "--client-id",
                           "--client-secret", "--callback-port", "--timeout"})


def _client_add(rest: list, env: dict, notes: list, look: _Lookups) -> dict | None:
    transport, headers, url, pos, i = "", {}, None, [], 0
    env = dict(env)
    while i < len(rest):
        w = rest[i]
        if w == "--":
            pos.extend(rest[i + 1:])
            break
        if len(pos) >= 2:                  # NAME and the command: the rest is the command's
            pos.extend(rest[i:])
            break
        if not w.startswith("-"):
            pos.append(w)
            i += 1
            continue
        opt, eq, val = w.partition("=") if w.startswith("--") else (w, "", "")
        if not eq and opt in _CLIENT_VALUE:
            if i + 1 >= len(rest):
                _note(notes, N_FLAG, w, f"the option {_part(w)} has no value")
                break
            val, i = rest[i + 1], i + 2
        else:
            i += 1
        if opt in ("-e", "--env"):
            vals = [val]
            while i < len(rest) and _ASSIGN_RE.match(rest[i]) and len(pos) < 2:
                vals.append(rest[i])       # claude's -e takes several KEY=VALUE
                i += 1
            for kv in vals:
                m = _ASSIGN_RE.match(kv)
                if m:
                    env[m.group(1)] = m.group(2)
                else:
                    _note(notes, N_SETTING, kv, f"the setting {_part(kv)} has no value")
        elif opt in ("-t", "--transport"):
            transport = val.lower()
        elif opt in ("-H", "--header"):
            hn, sep, hv = val.partition(":")
            if sep:
                headers[hn.strip()] = hv.strip()
            else:
                _note(notes, N_HEADER, val, "a header could not be read")
        elif opt == "--url":
            url = val
        elif opt in ("-s", "--scope"):
            pass                            # where the client keeps it: not a server's setting
        else:
            _note(notes, N_FLAG, opt, f"the option {_part(opt)} was not used")
    if not pos:
        _note(notes, N_ENTRY, "mcp add", "no server name follows mcp add")
        return None
    name, cmd = propose_name(pos[0]), pos[1:]
    if url is not None or transport in _HTTP_TYPES or (cmd and _URL_RE.match(cmd[0])):
        if url is None:
            url = cmd[0] if cmd else None
        if env:
            _note(notes, N_SETTING, ", ".join(env), "an address server takes no settings")
        return _address(name, url, headers, "", notes, sse=transport == "sse")
    if len(cmd) == 1 and " " in cmd[0].strip():
        try:
            cmd = shlex.split(cmd[0])       # droid: the command as one quoted word
        except ValueError:
            pass
    if headers:
        _note(notes, N_HEADER, ", ".join(headers), "headers belong to an address; a server "
              "RS runs takes settings")
    return _from_words(name, cmd, env, "", notes, look)


# What ends the one command a line runs: a comment, a pipe, a list or a
# redirection. What follows is not the server's.
_SHELL_ENDS = frozenset({"&", "&&", "|", "||", ";", ">", ">>", "<", "2>&1", "2>", "1>"})
# A README's `<your service account token>` is not quoted, and a shell would
# read its `<` as a redirection: it is quoted first so it stays one word.
_ANGLE_RE = re.compile(r"<[^<>'\"\n]*>")


def _line_words(line: str, notes: list) -> list | None:
    try:
        words = shlex.split(_ANGLE_RE.sub(lambda m: "'" + m.group(0) + "'", line),
                            posix=True)
    except ValueError:
        return None
    for j, w in enumerate(words):
        if w.startswith("#"):              # a trailing `# comment`
            return words[:j]
        if w in _SHELL_ENDS:
            _note(notes, N_FLAG, w, f"what follows {_part(w)} on the line was not read")
            return words[:j]
    return words


def _shell(text: str, notes: list, look: _Lookups) -> list:
    drafts, unread = [], []
    lines = text.replace("\r\n", "\n").split("\n")
    joined, start = [], None
    buf = ""
    for n, raw in enumerate(lines, 1):     # join `\` continuations; keep the first line's number
        if start is None:
            start = n
        if raw.rstrip().endswith("\\"):
            buf += raw.rstrip()[:-1] + " "
            continue
        joined.append((start, buf + raw))
        buf, start = "", None
    if buf:
        joined.append((start, buf))
    for n, line in joined:
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        own: list = []
        words = _line_words(s, own)
        if words is None:
            unread.append(n)
            continue
        if words[:1] == ["$"]:
            words = words[1:]              # a prompt sign copied with the line
        if words[:1] == ["env"]:
            words = words[1:]
        env = {}
        while words and _ASSIGN_RE.match(words[0]):
            m = _ASSIGN_RE.match(words[0])
            env[m.group(1)] = m.group(2)
            words = words[1:]
        if not words:
            unread.append(n)
            continue
        draft: Any = False
        if words[0] in CLIENT_CLIS and words[1:3] == ["mcp", "add"]:
            draft = _client_add(words[3:], env, own, look)
        elif words[0] in CLIENT_CLIS and words[1:3] == ["mcp", "add-json"] and len(words) >= 5:
            obj, _why = _load_json(words[4])
            draft = _from_entry(words[3], obj, look)[0] if _is_entry(obj) else None
            if draft is None and not _is_entry(obj):
                _note(own, N_PARSE, words[3], "the server's JSON could not be read")
        elif words[0] in ("code", "code-insiders") and "--add-mcp" in words:
            k = words.index("--add-mcp")
            obj, _why = _load_json(words[k + 1]) if k + 1 < len(words) else (None, None)
            if _is_entry(obj):
                nm = obj.get("name") if isinstance(obj.get("name"), str) else None
                draft = _from_entry(nm, obj, look)[0]
            else:
                draft = None
                _note(own, N_PARSE, "--add-mcp", "the server's JSON could not be read")
        else:
            draft = _install(words, env, own, look)
            if draft is False:
                if words[0] in ("npx", "uvx", "cmd") or words[:3] == ["uv", "tool", "run"] \
                        or words[:2] == ["docker", "run"] or _URL_RE.match(words[0]):
                    draft = _from_words(None, words, env, "", own, look)
                else:
                    unread.append(n)
                    continue
        if draft:
            own_line = [x for x in own if not any(x is y for y in draft["notes"])]
            draft["notes"][:0] = own_line   # the line's own notes (a cut-off tail)
            drafts.append(draft)
        else:
            for note in own:
                note["text"] = f"line {n}: " + note["text"]
            notes.extend(own)
    if unread:
        part = ", ".join(map(str, unread))
        _note(notes, N_LINE, part, f"not a server's configuration, so not read: line"
              f"{'s' if len(unread) > 1 else ''} {part}")
    return drafts


# --- the entry point ------------------------------------------------------------------

def read(text: str, *, npm_manifest: Callable[[str, str], dict],
         pypi_version: Callable[[str], str]) -> dict:
    """{"drafts": [draft, …], "notes": [note, …]} for a pasted text. `notes`
    holds what belongs to no draft. `npm_manifest(package, ref)` returns npm's
    manifest of `ref` ("latest" or an exact version); `pypi_version(package)`
    PyPI's current version; each raises on any failure (TimeoutError when out
    of time)."""
    look = _Lookups(npm_manifest, pypi_version)
    notes: list = []
    drafts: list = []
    body = text.strip()
    if not body:
        _note(notes, N_PARSE, "", "nothing was pasted")
    elif body[0] in "{[\"":
        obj, why = _load_json(body)
        if why is not None:
            _note(notes, N_PARSE, why, f"the text looks like JSON but could not be read "
                  f"({why})")
        else:
            for key, entry in _json_specs(obj, notes):
                draft, own = _from_entry(key, entry, look)
                if draft is not None:
                    drafts.append(draft)
                else:
                    notes.extend(own)
    else:
        drafts = _shell(body, notes, look)
    seen: set = set()
    for d in drafts:                       # two servers never share a proposed name
        base, k = d["name"], 2
        while d["name"] in seen:
            d["name"], k = f"{base}-{k}", k + 1
        seen.add(d["name"])
    return {"drafts": drafts, "notes": notes}
