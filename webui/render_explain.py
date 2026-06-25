#!/usr/bin/env python3
"""Render the per-workflow Explain docs into self-contained static HTML fragments
for the webui (STAGE_WORKFLOW_EXPLAIN). Build-time only; pure-Python (no node, no
Chromium). For each <name>.md in the source dir:

  * markdown -> HTML (fenced code, tables, header ids for the SVG jump-links),
  * the SVG marker is replaced VERBATIM with the sibling <name>.svg — done AFTER
    markdown rendering so the converter never touches the raw SVG,

and writes <out>/<name>.html plus <out>/index.json (the rendered names). The
output is a fragment the SPA injects under a `.explain-doc` container (the SVG's
own <style> is self-scoped to `.explain-doc svg …`).

Usage:  python render_explain.py <src_dir> <out_dir>
"""

import json
import sys
from pathlib import Path

import markdown

# A plain-alnum sentinel the .md places on its own line where the orchestration
# diagram goes. Plain alnum so markdown passes it through untouched (no emphasis /
# link mangling); after rendering it appears as `<p>RSORCHESTRATIONSVG</p>`.
SVG_MARKER = "RSORCHESTRATIONSVG"


def render_one(md_path: Path, svg_path: Path) -> str:
    html = markdown.markdown(
        md_path.read_text(encoding="utf-8"),
        extensions=["fenced_code", "tables", "toc"],
    )
    svg = svg_path.read_text(encoding="utf-8").strip() if svg_path.is_file() else ""
    # Replace the <p>-wrapped marker first (the normal case), then a bare marker as
    # a fallback, so a stray marker never survives into the page.
    if f"<p>{SVG_MARKER}</p>" in html:
        html = html.replace(f"<p>{SVG_MARKER}</p>", svg)
    elif SVG_MARKER in html:
        html = html.replace(SVG_MARKER, svg)
    return html


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    src, out = Path(argv[1]), Path(argv[2])
    if not src.is_dir():
        print(f"render_explain: source dir {src} not found", file=sys.stderr)
        return 1
    out.mkdir(parents=True, exist_ok=True)
    names: list[str] = []
    for md_path in sorted(src.glob("*.md")):
        name = md_path.stem
        (out / f"{name}.html").write_text(
            render_one(md_path, src / f"{name}.svg"), encoding="utf-8")
        names.append(name)
    (out / "index.json").write_text(json.dumps(names) + "\n", encoding="utf-8")
    print(f"render_explain: rendered {len(names)} doc(s): {', '.join(names)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
