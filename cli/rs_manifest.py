#!/opt/conda/bin/python
"""manifest — the sandbox artifact verb (STAGE_SANDBOX_ARTIFACTS).

The single, program-enforced write path for a sandbox's *file manifest*: an
identify-level index of every finished file on the publish surface, one line
each (filename + a one-line "what it is"). There is no schema, no per-file
descriptor, no per-type richness — deeper context comes from opening the
artifact. Because this program is the *only* sanctioned writer, the manifest's
structure cannot be corrupted by the (possibly weak / third-party) agent: the
agent passes a filename + a one-liner as arguments; this program writes the
entry. Content is trusted (not verified); structure is guaranteed.

Stdlib only (no third-party imports). The interpreter is pinned to
`/opt/conda/bin/python` — the repo convention for baked CLIs — which is present
in every sandbox container (baked roles and rs-pi-isolated are all FROM
rs-pi-base, a conda image). Pinning the interpreter rather than relying on
`env python3` makes the verb PATH-independent: it runs identically from the
Stop-hook exec context and from a bare `docker exec`, neither of which is
guaranteed a login shell with conda on PATH.

Subcommands:
  describe <file> <one-liner>   record/refresh the entry for <file>
  gate                          Stop-hook reconcile: block (exit 2) if any
                                published file is undescribed; prune orphans;
                                heal a corrupt manifest
  list                          print the manifest as JSON (supervisor / debug)

The publish surface defaults to /workspace/published (PI sessions are pinned to
/workspace); override with $RS_PUBLISHED_DIR. The manifest itself is
<published>/manifest.json and is excluded from the scan so the gate can't
self-trigger.

Exit codes: 0 clean, 2 gate-blocked (matches rs-audit-stop's convention so the
Claude Code Stop hook blocks and feeds stderr back to the model), 1 internal
error.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

MANIFEST_NAME = "manifest.json"
MANIFEST_VERSION = 1


def _published_dir() -> Path:
    return Path(os.environ.get("RS_PUBLISHED_DIR", "/workspace/published"))


def _manifest_path(published: Path) -> Path:
    return published / MANIFEST_NAME


def _now_iso() -> str:
    # Stamped by this program, never by the producer — so freshness can't be
    # faked. Whole-second UTC ISO 8601.
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _empty_manifest() -> dict:
    return {"version": MANIFEST_VERSION, "entries": {}}


def _load(published: Path) -> tuple[dict, bool]:
    """Return (manifest, healed). `healed` is True if the on-disk file was
    missing or unparseable and we substituted a fresh structure — the caller
    (gate) rewrites it so a corrupt/hand-edited manifest self-repairs, and
    every real file then re-surfaces as undescribed."""
    p = _manifest_path(published)
    if not p.is_file():
        return _empty_manifest(), False
    try:
        data = json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return _empty_manifest(), True
    if not isinstance(data, dict) or not isinstance(data.get("entries"), dict):
        return _empty_manifest(), True
    # Drop structurally-bad entries (program-owned structure: anything that
    # isn't {id:str, ts:str} is noise a hand-edit introduced).
    clean: dict[str, dict] = {}
    healed = False
    for rel, entry in data["entries"].items():
        if (
            isinstance(rel, str)
            and isinstance(entry, dict)
            and isinstance(entry.get("id"), str)
            and entry["id"].strip()
        ):
            ts = entry.get("ts")
            clean[rel] = {"id": entry["id"], "ts": ts if isinstance(ts, str) else _now_iso()}
        else:
            healed = True
    return {"version": MANIFEST_VERSION, "entries": clean}, healed


def _save(published: Path, manifest: dict) -> None:
    """Atomic-rename write. published/ is on the parent-dir bind-mount, so the
    inode swap is visible to the supervisor immediately (the single-file
    bind-mount pitfall does not apply to a file inside a mounted dir)."""
    p = _manifest_path(published)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    tmp.replace(p)


def _scan_files(published: Path) -> list[str]:
    """Every file under published/ as a POSIX relative path, excluding the
    manifest itself and its temp sibling. Directories are not entries."""
    out: list[str] = []
    for path in published.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(published).as_posix()
        if rel in (MANIFEST_NAME, MANIFEST_NAME + ".tmp"):
            continue
        out.append(rel)
    return sorted(out)


def _resolve_rel(published: Path, file_arg: str) -> str | None:
    """Map a user-supplied path (absolute, or relative to published/ or cwd) to
    a POSIX path relative to published/. Returns None if it lands outside
    published/ — describing something off-surface is a mistake, not an entry."""
    raw = Path(file_arg)
    candidates = [raw, published / raw, Path.cwd() / raw]
    pub = published.resolve()
    for c in candidates:
        try:
            resolved = c.resolve()
        except OSError:
            continue
        try:
            return resolved.relative_to(pub).as_posix()
        except ValueError:
            continue
    return None


# --- subcommands -----------------------------------------------------------


def cmd_describe(args: argparse.Namespace) -> int:
    published = _published_dir()
    one_liner = " ".join(args.one_liner.split()).strip()
    if not one_liner:
        print("manifest: refusing an empty description", file=sys.stderr)
        return 1
    rel = _resolve_rel(published, args.file)
    if rel is None:
        print(
            f"manifest: {args.file!r} is not under the publish surface "
            f"({published}); move it there first, or it isn't a deliverable",
            file=sys.stderr,
        )
        return 1
    if not (published / rel).is_file():
        print(f"manifest: no such file on the publish surface: {rel}", file=sys.stderr)
        return 1
    manifest, _ = _load(published)
    manifest["entries"][rel] = {"id": one_liner, "ts": _now_iso()}
    _save(published, manifest)
    print(f"manifest: described {rel}")
    return 0


def cmd_gate(_args: argparse.Namespace) -> int:
    """Stop-hook reconcile. Block (exit 2) while any published file lacks an
    entry; prune orphan entries; persist a healed/pruned manifest. Staleness is
    surfaced by `list`, never blocked here (no modify-trigger by design)."""
    # Drain stdin if the hook payload is piped in; we don't need it.
    if not sys.stdin.isatty():
        try:
            sys.stdin.read()
        except OSError:
            pass

    published = _published_dir()
    if not published.is_dir():
        # No surface yet → nothing to gate.
        return 0

    manifest, healed = _load(published)
    files = set(_scan_files(published))
    described = set(manifest["entries"])

    # Prune orphans (entry whose file is gone).
    orphans = described - files
    if orphans:
        for rel in orphans:
            del manifest["entries"][rel]

    undescribed = sorted(files - described)

    if orphans or healed:
        _save(published, manifest)

    if undescribed:
        listing = "\n".join(f"  - {rel}" for rel in undescribed)
        print(
            "Unfinished artifact bookkeeping — you cannot stop yet.\n"
            f"These files are on your publish surface ({published}) with no "
            "manifest entry:\n"
            f"{listing}\n\n"
            "For EACH file, do ONE of:\n"
            '  • describe it:  manifest describe <file> "<one line: what it is>"\n'
            "  • or, if it is not a deliverable, move it to ../internal/ "
            "(or delete it).\n\n"
            "Then you may stop.",
            file=sys.stderr,
        )
        return 2
    return 0


def cmd_list(_args: argparse.Namespace) -> int:
    """Print the manifest with a per-entry freshness flag (stale = file newer
    than its entry). Surfaced, never enforced."""
    published = _published_dir()
    manifest, _ = _load(published)
    out = {"version": manifest["version"], "published": str(published), "entries": {}}
    for rel, entry in sorted(manifest["entries"].items()):
        f = published / rel
        stale = False
        if f.is_file():
            try:
                # Floor mtime to whole seconds to match the entry ts resolution
                # (entries are stamped at whole-second granularity); otherwise a
                # file and its entry written in the same second compare as
                # "file newer" and flag a fresh entry stale.
                file_mtime = datetime.fromtimestamp(int(f.stat().st_mtime), tz=timezone.utc)
                entry_ts = datetime.fromisoformat(entry["ts"])
                stale = file_mtime > entry_ts
            except (ValueError, OSError):
                stale = False
        out["entries"][rel] = {"id": entry["id"], "ts": entry["ts"], "stale": stale}
    print(json.dumps(out, indent=2, sort_keys=True))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="manifest",
        description="Sandbox artifact file-manifest verb (identify-level).",
    )
    sub = p.add_subparsers(dest="command", required=True)

    d = sub.add_parser("describe", help="record/refresh a file's one-line identification")
    d.add_argument("file", help="path to the artifact on the publish surface")
    d.add_argument("one_liner", help="one line: what this file is")
    d.set_defaults(func=cmd_describe)

    g = sub.add_parser("gate", help="Stop-hook reconcile; exit 2 if anything is undescribed")
    g.set_defaults(func=cmd_gate)

    l = sub.add_parser("list", help="print the manifest as JSON")
    l.set_defaults(func=cmd_list)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
