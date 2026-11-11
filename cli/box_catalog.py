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
import stat
from pathlib import Path
from typing import Any

# Built-in presets are tracked product data at the repo root; the BYO registry is
# host-side, mirroring the MCP / workflow registries.
BUILTIN_DIR = Path(__file__).resolve().parent.parent / "boxes"
REGISTRY_DIR = Path.home() / ".research-sandbox"
REGISTRY_PATH = REGISTRY_DIR / "box-registry.json"
# How a refusal names the registry. Browser-reachable (the broker relays a
# BoxCatalogError's text verbatim), so it carries the file's NAME only, never
# its folder: a host path must not reach the browser.
REGISTRY_TEXT = f"the box type registry on the host ({REGISTRY_PATH.name})"

VERSION = 1

# A box preset selects one of the two existing box images (STAGE_DIND_UNIFY): the
# clean base, or the browser variant (Playwright + Chromium baked). Kept as bare
# strings — the image-level capability axis; lockstep with rs_sandbox.BOX_IMAGE /
# BOX_IMAGE_BROWSER (which map "base"/"browser" → the :latest tags).
IMAGES = ("base", "browser")

NAME_RE = re.compile(r"^[a-z][a-z0-9-]*$")

# --- preset input fields (declared inputs the box window renders) ------------
# A field's value reaches the box as environment (sourced from a private 0600
# file), so its NAME is an env-var name — and anything the box machinery itself
# delivers via env is reserved: RS_* (box wiring), GITEA_* (dev lane),
# ANTHROPIC_*/CLAUDE_* (the agent's model/effort/auth channel — a sourced file
# would silently override STAGE_MODEL_SELECT's delivery), plus the shell's own
# load-bearing names. Rule of thumb for future channels: if an entrypoint or
# _run_box ever exports it, it belongs here.
FIELD_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
RESERVED_FIELD_PREFIXES = ("RS_", "GITEA_", "ANTHROPIC_", "CLAUDE_")
RESERVED_FIELD_NAMES = frozenset({"PATH", "HOME", "USER", "SHELL", "TERM",
                                  "LOGNAME"})
# ${NAME} tokens inside mcp_servers env values. The DELIVERY CONTRACT
# (spike-verified on the discovery path at the pinned agent version): the agent
# expands these from its own environment at config load — the staging side
# never expands, so no field VALUE ever lands in a workspace file. Every
# reference must name a DECLARED field (kills the typo-expands-to-nothing
# class), and references are ENV-ONLY: a ${ in command/args is rejected, so a
# value can never reach the in-box argv (the security invariant stays literal).
# The only accepted reference form is ${NAME} with NAME a declared field: pi
# reads every other $/! form differently from Claude Code's substitution, so
# _validate_entry refuses them (measured on pi 1.5.0):
#   ${NAME:-default}  pi keeps it as literal text; claude applies the default
#   $NAME             pi expands it; claude keeps it as text
#   $$ and $!         pi turns them into $ and !; claude keeps them
#   leading !         pi RUNS the value as a shell command; claude passes text
# Every field is required and non-empty, so a :-default could never apply.
_FIELD_REF_RE = re.compile(r"\$\{([^}]*)\}")

# The browser image's baked stdio MCP server name(s) — source of truth is
# container/role-mcp/websearcher/extra-mcps.yaml (mcpServers: playwright:). A
# preset declaring a colliding mcp_servers name on a browser-image box would
# crash-loop at boot (the entrypoint's merge refuses), so box_add pre-flights
# against this host-side. MIRRORED in cli/rs_sandbox.py (staged standalone,
# cannot import this module) — pytest-pinned equality.
BAKED_BROWSER_MCPS = ("playwright",)


def is_reserved_field(name: str) -> bool:
    return name in RESERVED_FIELD_NAMES or name.startswith(RESERVED_FIELD_PREFIXES)

# Curated display order for the "Add box" window (the webui + the in-supervisor
# rs-sandbox render the catalog in list order). Built-ins sort by their index
# here; an unlisted built-in and every BYO operator type fall after, by name.
# Product order, not operator data — a constant here, not a manifest field.
_BUILTIN_ORDER = ("empty", "websearcher", "zotero", "data-wrangler", "byo",
                  "paper-orchestra", "open-knowledge")

# `instructions` is folded in from the sibling .md (built-ins) or carried inline
# (BYO), so it is an allowed key on the normalized entry the validator sees.
# `repo` bakes a clone URL into the preset (clone:true presets only); `editor_default`
# pre-selects the box's editor toggle in the webui (a UI default — NOT honored by
# the in-supervisor rs-sandbox, so a box stays opt-out-able). `dev` marks the
# gitea dev-lane preset (STAGE_DEV_GITEA): rs-sandbox runs it on a dedicated
# bridge with the ADS hardening flags and an authed fork clone — mutually
# exclusive with the auth-less `clone` lane and base-image-only.
_ALLOWED_KEYS = {"name", "image", "agent_default", "clone", "description",
                 "instructions", "repo", "editor_default", "dev",
                 # setup: a visible boot script (run once per container fs);
                 # fields: declared inputs the box window renders (secret ⇒
                 # password input; values travel as a private env file);
                 # mcp_servers: preset-declared stdio MCP entries, staged as a
                 # third merge source for the box's .mcp.json.
                 "setup", "fields", "mcp_servers",
                 # node: the project's pinned node seed is copied into the box
                 # home as an RS step in front of the setup; greeting: the
                 # message the box's terminal tab prints when it opens.
                 "node", "greeting",
                 # gitea_history: the box may keep its notes' history on the
                 # shared Gitea (built-in presets only — load_registry refuses
                 # it on an operator-registered type).
                 "gitea_history",
                 # web: a web app the box runs itself and the webui shows as a
                 # tab — {port, label, start}; built-in presets only.
                 "web",
                 # feed_skill / ingest_skill: SKILL.md texts folded from the
                 # sibling <name>.feed-skill.md / <name>.ingest-skill.md. A feed
                 # skill is offered to every OTHER agent of the project (each
                 # box, the project agent); an ingest skill is the preset's
                 # own. Built-in presets only; staged by the box tool.
                 "feed_skill", "ingest_skill",
                 # share_default: the box window's "Share files" card starts
                 # ticked for this type (a default only — the operator sees and
                 # can untick it at creation; any type may carry it).
                 "share_default"}

# The skill texts ride the catalog like `instructions` does: a sibling file per
# key, folded at load, never inline in the JSON. The skill's slash-command name
# is the `name` of its frontmatter, which is also the folder the box tool
# writes it under (`<home>/<name>/SKILL.md`) — so it must be a path-safe name
# and unique across the built-ins (two feeds sharing a name would overwrite
# each other in every box).
SKILL_SIDECARS = {"feed_skill": "feed-skill", "ingest_skill": "ingest-skill"}

# The box's own internal service ports (its editor stub and the reader), which
# a preset's web app may not take. MIRROR of what the box images bind.
BOX_INTERNAL_PORTS = (8443, 8444)


class BoxCatalogError(Exception):
    pass


def skill_name(text: Any) -> str:
    """The `name:` of a SKILL.md's YAML frontmatter (the block between the
    file's first line `---` and the next `---`), or "" when the text has no
    frontmatter or no name there. MIRRORED in cli/rs_sandbox.py (staged
    standalone; pytest-pinned on the same samples)."""
    if not isinstance(text, str):
        return ""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return ""
    for ln in lines[1:]:
        if ln.strip() == "---":
            return ""
        key, sep, val = ln.partition(":")
        if sep and key.strip() == "name":
            return val.strip()
    return ""


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

    # editor_default is optional; when present it must be a boolean.
    if "editor_default" in m and not isinstance(m["editor_default"], bool):
        out.append(p(f"editor_default must be a boolean, got {m.get('editor_default')!r}"))

    # dev is optional; a dev preset clones via the authed gitea lane (NOT the
    # auth-less RS_BOX_CLONE lane) and runs on the base image only.
    if "dev" in m:
        if not isinstance(m["dev"], bool):
            out.append(p(f"dev must be a boolean, got {m.get('dev')!r}"))
        elif m["dev"]:
            if m.get("clone") is not False:
                out.append(p("dev:true requires clone:false (the dev clone is the "
                             "authed gitea lane, not the byo clone lane)"))
            if m.get("image") != "base":
                out.append(p("dev:true requires image:'base'"))

    # repo bakes a clone URL into the preset; it must be a non-empty string and is
    # only meaningful for a clone preset (so a non-empty repo requires clone:true).
    if "repo" in m:
        if not _is_str(m["repo"]):
            out.append(p("repo must be a non-empty string"))
        elif m.get("clone") is not True:
            out.append(p("repo requires clone:true (a baked repo only applies to a clone preset)"))

    # setup: a user-visible boot script (run once per container filesystem by
    # the entrypoint). One setup mechanism per preset: the clone (byo) lane has
    # its own request-level setup, so a preset-level one requires clone:false.
    if "setup" in m:
        if not _is_str(m["setup"]):
            out.append(p("setup must be a non-empty string"))
        elif m.get("clone") is not False:
            out.append(p("setup requires clone:false (a clone preset takes its "
                         "setup per-box, not from the preset)"))

    # fields: declared inputs. Names are env-var names (the value reaches the
    # box as environment from a private file), so the reserved set guards the
    # box's own machinery channels.
    declared_fields: set[str] = set()
    if "fields" in m:
        if not isinstance(m["fields"], list):
            out.append(p("fields must be a list"))
        else:
            for i, f in enumerate(m["fields"]):
                q = lambda msg: p(f"fields[{i}]: {msg}")
                if not isinstance(f, dict):
                    out.append(q("must be an object"))
                    continue
                fn = f.get("name")
                if not (isinstance(fn, str) and FIELD_NAME_RE.match(fn)):
                    out.append(q(f"name must match {FIELD_NAME_RE.pattern!r}, "
                                 f"got {fn!r}"))
                elif is_reserved_field(fn):
                    out.append(q(f"name {fn!r} is reserved (box machinery "
                                 f"delivers it via env)"))
                elif fn in declared_fields:
                    out.append(q(f"duplicate field name {fn!r}"))
                else:
                    declared_fields.add(fn)
                if not _is_str(f.get("label")):
                    out.append(q("label must be a non-empty string"))
                if "secret" in f and not isinstance(f["secret"], bool):
                    out.append(q("secret must be a boolean"))
                fextras = set(f) - {"name", "label", "secret"}
                if fextras:
                    out.append(q(f"unknown keys: {sorted(fextras)}"))

    # mcp_servers: preset-declared stdio MCP entries. DELIVERY CONTRACT for
    # field values: ${NAME} references in env VALUES, expanded by the AGENT at
    # config load (never by staging — no value ever lands in a workspace file).
    # References are env-only and must name declared fields; a ${ in
    # command/args is rejected outright so a field value can never reach the
    # in-box argv. An env value is literal text plus ${NAME} references only:
    # no other $ in it and no leading !, because pi reads those differently
    # from Claude Code (see _FIELD_REF_RE). Errors name the env KEY, never the
    # value (a preset may carry a literal secret in env).
    if "mcp_servers" in m:
        ms = m["mcp_servers"]
        if not isinstance(ms, dict):
            out.append(p("mcp_servers must be an object"))
        else:
            for sn, cfg in ms.items():
                q = lambda msg: p(f"mcp_servers[{sn!r}]: {msg}")
                if not (isinstance(sn, str) and NAME_RE.match(sn)):
                    out.append(p(f"mcp_servers: invalid server name {sn!r}"))
                if not isinstance(cfg, dict):
                    out.append(q("must be an object"))
                    continue
                if not _is_str(cfg.get("command")):
                    out.append(q("command must be a non-empty string"))
                elif "${" in cfg["command"]:
                    out.append(q("no ${...} references in command (env-only)"))
                if "args" in cfg:
                    if not (isinstance(cfg["args"], list)
                            and all(isinstance(a, str) for a in cfg["args"])):
                        out.append(q("args must be a list of strings"))
                    elif any("${" in a for a in cfg["args"]):
                        out.append(q("no ${...} references in args (env-only)"))
                if "env" in cfg:
                    if not (isinstance(cfg["env"], dict)
                            and all(isinstance(k, str) and isinstance(v, str)
                                    for k, v in cfg["env"].items())):
                        out.append(q("env must be an object of string values"))
                    else:
                        for ek, ev in cfg["env"].items():
                            if ev.startswith("!"):
                                out.append(q(
                                    f"env {ek!r} starts with '!', which pi "
                                    f"runs as a shell command while Claude "
                                    f"Code passes it as text; a preset env "
                                    f"value cannot start with '!'"))
                            bad_form = False
                            for tok in _FIELD_REF_RE.findall(ev):
                                if not FIELD_NAME_RE.fullmatch(tok):
                                    bad_form = True
                                elif tok not in declared_fields:
                                    out.append(q(
                                        f"env references undeclared field "
                                        f"{tok!r} (declare it in fields)"))
                            if bad_form:
                                out.append(q(
                                    f"env {ek!r} has a reference other than "
                                    f"${{FIELD}} (such as "
                                    f"${{FIELD:-default}}), which pi reads as "
                                    f"literal text; every field is required, "
                                    f"so write ${{FIELD}}"))
                            if "$" in _FIELD_REF_RE.sub("", ev):
                                out.append(q(
                                    f"env {ek!r} has a '$' outside a "
                                    f"${{FIELD}} reference; pi expands $NAME "
                                    f"and turns $$ and $! into $ and !, where "
                                    f"Claude Code keeps them as text"))
                cextras = set(cfg) - {"command", "args", "env"}
                if cextras:
                    out.append(q(f"unknown keys: {sorted(cextras)}"))

    # node: copied into the box home by an RS step that runs in front of the
    # preset's own setup (the run-once setup block). A clone preset's per-box
    # setup runs earlier, in a login shell, so it could never see that node —
    # hence clone:false, the same rule setup has.
    if "node" in m:
        if not isinstance(m["node"], bool):
            out.append(p(f"node must be a boolean, got {m.get('node')!r}"))
        elif m["node"] and m.get("clone") is not False:
            out.append(p("node requires clone:false (node arrives in front of the "
                         "preset setup, after a clone preset's own setup ran)"))

    # greeting: the text the box's terminal tab prints when it opens a session
    # (written to the box's /workspace/.rs-greeting).
    if "greeting" in m and not _is_str(m["greeting"]):
        out.append(p("greeting must be a non-empty string"))

    # gitea_history: RS provisions a private notes repo + a box account on the
    # shared Gitea and wires the box's notes/ to it (OpenKnowledge's own sync
    # pushes). It needs the node+setup lane the built-in carries.
    if "gitea_history" in m and not isinstance(m["gitea_history"], bool):
        out.append(p(f"gitea_history must be a boolean, got "
                     f"{m.get('gitea_history')!r}"))

    # share_default: pre-ticks "Share files" in the box window.
    if "share_default" in m and not isinstance(m["share_default"], bool):
        out.append(p(f"share_default must be a boolean, got "
                     f"{m.get('share_default')!r}"))

    # web: the box keeps `start` running with the tab's address in
    # $RS_WEB_ADDRESS, and the webui shows `port` as a tab named `label`. It
    # needs a setup: the box reports the app ready only once its setup is done.
    if "web" in m:
        w = m["web"]
        if not isinstance(w, dict) or set(w) != {"port", "label", "start"}:
            out.append(p("web must be an object with exactly port, label, start"))
        else:
            port = w.get("port")
            if (not isinstance(port, int) or isinstance(port, bool)
                    or not 1 <= port <= 65535 or port in BOX_INTERNAL_PORTS):
                out.append(p(f"web.port must be a TCP port other than "
                             f"{'/'.join(map(str, BOX_INTERNAL_PORTS))}, got {port!r}"))
            if not _is_str(w.get("label")):
                out.append(p("web.label must be a non-empty string"))
            if not _is_str(w.get("start")):
                out.append(p("web.start must be a non-empty string"))
        if not _is_str(m.get("setup")):
            out.append(p("web requires a setup (the app is reported ready only "
                         "after the setup ran)"))

    # feed_skill / ingest_skill: a SKILL.md text whose frontmatter names the
    # skill; the name is a folder the box tool creates, so it is path-safe.
    for k in SKILL_SIDECARS:
        if k in m:
            if not _is_str(m[k]):
                out.append(p(f"{k} must be a non-empty string"))
            elif not NAME_RE.match(skill_name(m[k])):
                out.append(p(f"{k}: the SKILL.md frontmatter must carry a "
                             f"`name:` matching {NAME_RE.pattern!r}, got "
                             f"{skill_name(m[k])!r}"))

    # The dev lane stays isolated: a dev preset carries none of the new keys.
    if m.get("dev"):
        for k in ("setup", "fields", "mcp_servers", "node", "greeting",
                  "gitea_history", "web", "feed_skill", "ingest_skill"):
            if k in m:
                out.append(p(f"dev:true forbids {k!r}"))

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


def _read_builtin(f: Path) -> str:
    """One of RS's own box type files, as UTF-8 text. A read or decode failure
    is a BoxCatalogError naming the file's name only (the text reaches the
    browser; the folder is a host path)."""
    try:
        return f.read_text(encoding="utf-8")
    except OSError as e:
        raise BoxCatalogError(
            f"RS's built-in box type file {f.name} cannot be read "
            f"({e.strerror or 'read failed'})") from e
    except UnicodeDecodeError as e:
        raise BoxCatalogError(
            f"RS's built-in box type file {f.name} is not UTF-8 text") from e


def load_builtins(builtin_dir: Path = BUILTIN_DIR) -> dict[str, dict]:
    """Read + validate every boxes/<name>.json built-in, folding the sibling
    boxes/<name>.instructions.md (if present) into `instructions`. Raises
    BoxCatalogError on a malformed file, a name/file mismatch, or a duplicate."""
    out: dict[str, dict] = {}
    if not builtin_dir.is_dir():
        return out
    for f in sorted(builtin_dir.glob("*.json")):
        text = _read_builtin(f)
        try:
            m = json.loads(text)
        except json.JSONDecodeError as e:
            raise BoxCatalogError(f"builtin {f.name}: not valid JSON: {e}") from e
        except (ValueError, RecursionError) as e:
            raise BoxCatalogError(
                f"builtin {f.name}: not valid JSON (nested too deeply or out "
                f"of range)") from e
        if isinstance(m, dict) and "instructions" in m:
            raise BoxCatalogError(
                f"builtin {f.name}: 'instructions' belongs in the sibling "
                f"{f.stem}.instructions.md, not the JSON")
        sidecar = builtin_dir / f"{f.stem}.instructions.md"
        if isinstance(m, dict):
            m["instructions"] = _read_builtin(sidecar) if sidecar.is_file() else ""
        for key, suffix in SKILL_SIDECARS.items():
            if isinstance(m, dict) and key in m:
                raise BoxCatalogError(
                    f"builtin {f.name}: {key!r} belongs in the sibling "
                    f"{f.stem}.{suffix}.md, not the JSON")
            skill_file = builtin_dir / f"{f.stem}.{suffix}.md"
            # Folded only when the sidecar exists: the validator requires a
            # non-empty text, and most presets carry no skill at all.
            if isinstance(m, dict) and skill_file.is_file():
                m[key] = _read_builtin(skill_file)
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
    # One skill name, one text: the box tool writes each skill under its
    # frontmatter name, so two built-ins whose skills share a name would
    # overwrite each other in every box.
    seen: dict[str, str] = {}
    for nm, m in out.items():
        for key in SKILL_SIDECARS:
            if key in m:
                sn = skill_name(m[key])
                if sn in seen:
                    raise BoxCatalogError(
                        f"builtin {nm}.json: its {key} is named {sn!r}, which "
                        f"{seen[sn]} already uses; skill names are unique")
                seen[sn] = f"{nm}.json's skill"
    return out


def load_registry(path: Path = REGISTRY_PATH) -> dict[str, dict]:
    """Read + validate the host-side BYO box registry. Missing file → {}. Entries
    are keyed by name and must NOT repeat it; the key is injected as `name` before
    per-entry validation so one validator serves both shapes.

    Every failure is a BoxCatalogError whose text carries no path (it reaches
    the browser through the broker): an unreadable file or folder, something
    other than a regular file at the path, bytes that are not UTF-8, or text
    that is not JSON. Missing (a dangling link included) is "no operator
    types", never a refusal. The existence check is an explicit stat() rather
    than is_file(), whose handling of an unreadable folder differs across
    Python versions (3.14 swallows it and would hide the operator's types)."""
    try:
        st = path.stat()
    except (FileNotFoundError, NotADirectoryError):
        return {}
    except OSError as e:
        raise BoxCatalogError(
            f"{REGISTRY_TEXT} cannot be read ({e.strerror or 'read failed'})") from e
    if not stat.S_ISREG(st.st_mode):
        raise BoxCatalogError(f"{REGISTRY_TEXT} is not a file")
    try:
        raw = path.read_bytes()
    except OSError as e:
        raise BoxCatalogError(
            f"{REGISTRY_TEXT} cannot be read ({e.strerror or 'read failed'})") from e
    # Decoded explicitly (JSON is UTF-8): json.loads on bytes would guess a
    # UTF-16 encoding from a byte-order mark. UnicodeDecodeError and
    # JSONDecodeError are both ValueErrors, so they are caught first.
    try:
        data = json.loads(raw.decode("utf-8"))
    except UnicodeDecodeError as e:
        raise BoxCatalogError(f"{REGISTRY_TEXT} is not UTF-8 text") from e
    except json.JSONDecodeError as e:
        raise BoxCatalogError(
            f"{REGISTRY_TEXT} is not valid JSON (line {e.lineno} column "
            f"{e.colno}: {e.msg})") from e
    except (ValueError, RecursionError) as e:
        raise BoxCatalogError(
            f"{REGISTRY_TEXT} is not valid JSON (nested too deeply or out of "
            f"range)") from e
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
        # Gitea history wires an RS-owned Gitea account into the box; only a
        # built-in type may ask for it (the loader is the one place that
        # knows where an entry came from).
        if isinstance(m, dict) and m.get("gitea_history"):
            raise BoxCatalogError(
                f"registry: entry {name!r}: gitea_history is reserved for the "
                f"built-in box types")
        # A web app runs a start command the box keeps alive and gets a tab
        # without being asked for one: built-in types only, like history.
        if isinstance(m, dict) and "web" in m:
            raise BoxCatalogError(
                f"registry: entry {name!r}: web is reserved for the built-in "
                f"box types")
        # A skill is text every agent of the project reads as instructions:
        # built-in types only, like history and the web app.
        for key in SKILL_SIDECARS:
            if isinstance(m, dict) and key in m:
                raise BoxCatalogError(
                    f"registry: entry {name!r}: {key} is reserved for the "
                    f"built-in box types")
        out[name] = m
    return out


def load_catalog(builtin_dir: Path = BUILTIN_DIR,
                 registry_path: Path = REGISTRY_PATH) -> list[dict]:
    """The box-preset catalog: built-ins + BYO, normalized to a single list of
    manifests sorted by name, each tagged with a non-schema `source`
    ('builtin'|'byo'). A BYO name shadowing a built-in is an error, not a silent
    override (mirrors workflow.load_catalog).

    Raises only BoxCatalogError for a file it cannot read or parse, with a
    pathless text: callers map it to a refusal and need no other catch."""
    try:
        builtins = load_builtins(builtin_dir)
    except BoxCatalogError:
        raise
    except (OSError, ValueError, RecursionError) as e:
        # The folder check, its listing and the sidecar checks in
        # load_builtins sit outside _read_builtin; a broken RS install there
        # still answers with a refusal, built from strerror, never str(e).
        why = getattr(e, "strerror", None) or "a file is unreadable or malformed"
        raise BoxCatalogError(f"RS's built-in box types cannot be read ({why})") from e
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
