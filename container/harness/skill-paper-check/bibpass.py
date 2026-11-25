#!/usr/bin/env python3
"""The bibliography pass of the paper check: look every entry of one or more
BibTeX files up in OpenAlex and print one JSON object per entry on stdout.

    python3 bibpass.py references.bib [more.bib ...]

An entry is looked up by its DOI when it has one, else by its arXiv id (an
`eprint` field) through the DOI arXiv gives every preprint, else by a title
search. A DOI or arXiv lookup that finds a record is a hit. A title search is a
hit only when a candidate's title equals the entry's (case, punctuation and
spacing aside); either way its first candidates are printed, so a near miss
(a real work with a slightly different title, or a later version) shows.

Each object carries the entry's key, type, title, year and venue as written,
how it was looked up, whether it was found, and for a hit: the record's title,
year, venue, first authors, DOI, whether it is retracted, its open-access
status and URL, and its abstract rebuilt from OpenAlex's inverted index. A
network or parse failure for one entry is reported in that entry's object (an
`error` field) and the pass goes on with the next.

Standard library only; runs under any Python 3.9 or later. It sends a plain
generic User-Agent and nothing about the person running it.

The BibTeX reading is deliberately simple. It handles entries of any type,
`{…}` and `(…)` delimiters, braced values with nested braces, `"`-quoted values
(braces inside them included), bare numbers and words, and `#` concatenation.
It does NOT expand @string macros (a bare macro name stays as its name), does
not read @preamble or @comment, does not treat a `%` line as a comment (an @
on it starts an entry, as in BibTeX), does not follow `crossref` fields, and turns
LaTeX in a title into plain text only roughly (braces and backslash commands
are dropped, accented letters keep their letter).
"""
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

OPENALEX = "https://api.openalex.org"
HEADERS = {"User-Agent": "bibpass (python urllib)", "Accept": "application/json"}
# How many title-search candidates each entry shows: the hit (when there is
# one) and the next ones beside it, enough for a reader to see a near miss or
# a second version of the same work without a page of unrelated results.
TITLE_CANDIDATES = 3
# How many authors a record shows by name (the full count is printed beside
# them): enough to compare with the entry's first authors; a collaboration of
# hundreds would otherwise drown the line.
AUTHORS_SHOWN = 4
# BibTeX types that are not bibliography entries.
NON_ENTRIES = {"comment", "preamble", "string"}


class BibError(Exception):
    pass


# --- BibTeX -----------------------------------------------------------------

def _skip_ws(s, i):
    while i < len(s) and s[i].isspace():
        i += 1
    return i


def _braced(s, i):
    """The text inside the braces opening at s[i], and the index after them."""
    depth = 0
    start = i + 1
    while i < len(s):
        c = s[i]
        if c == "\\":
            i += 2
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return s[start:i], i + 1
        i += 1
    raise BibError("unbalanced braces")


def _quoted(s, i):
    """The text inside the quotes opening at s[i] (a quote inside braces does
    not end it), and the index after the closing quote."""
    depth = 0
    start = i + 1
    i += 1
    while i < len(s):
        c = s[i]
        if c == "\\":
            i += 2
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
        elif c == '"' and depth == 0:
            return s[start:i], i + 1
        i += 1
    raise BibError("unterminated quoted value")


def _value(s, i):
    """A field value (parts joined by #), and the index after it."""
    parts = []
    while True:
        i = _skip_ws(s, i)
        if i >= len(s):
            raise BibError("value runs to the end of the file")
        if s[i] == "{":
            part, i = _braced(s, i)
        elif s[i] == '"':
            part, i = _quoted(s, i)
        else:
            m = re.compile(r"[^\s,#}\)]+").match(s, i)
            if not m:
                raise BibError(f"unexpected {s[i]!r} where a value should be")
            part, i = m.group(0), m.end()
        parts.append(part)
        i = _skip_ws(s, i)
        if i < len(s) and s[i] == "#":
            i += 1
            continue
        return "".join(parts), i


def _entry(s, i):
    """Parse the entry whose @ is at s[i]: (type, key, fields, index after it).
    Raises BibError, with the key once it is known."""
    m = re.compile(r"@\s*([A-Za-z][\w-]*)\s*([{(])").match(s, i)
    if not m:
        raise BibError("not an entry header")
    etype, opener = m.group(1).lower(), m.group(2)
    closer = "}" if opener == "{" else ")"
    i = m.end()
    if etype in NON_ENTRIES:
        if opener == "{":
            _, j = _braced(s, m.end() - 1)
        else:
            j = s.find(")", i)
            if j < 0:
                raise BibError("unterminated @" + etype)
            j += 1
        return etype, None, None, j
    km = re.compile(r"\s*([^\s,{}()]+)\s*,").match(s, i)
    if not km:
        raise BibError("no key")
    key = km.group(1)
    i = km.end()
    fields = {}
    try:
        while True:
            i = _skip_ws(s, i)
            if i < len(s) and s[i] == closer:
                return etype, key, fields, i + 1
            fm = re.compile(r"([A-Za-z][\w:.+-]*)\s*=").match(s, i)
            if not fm:
                raise BibError(f"expected a field name at offset {i}")
            name = fm.group(1).lower()
            val, i = _value(s, fm.end())
            fields[name] = val
            i = _skip_ws(s, i)
            if i < len(s) and s[i] == ",":
                i += 1
            elif i < len(s) and s[i] == closer:
                return etype, key, fields, i + 1
            else:
                raise BibError(f"expected , or {closer} after field {name!r}")
    except BibError as e:
        e.key = key
        raise


def parse_bib(text):
    """Yield (type, key, fields) for each entry, or ("error", key, message)
    for one that could not be read; reading goes on at the next line that
    starts with @."""
    i = 0
    while True:
        i = text.find("@", i)
        if i < 0:
            return
        try:
            etype, key, fields, j = _entry(text, i)
        except BibError as e:
            yield "error", getattr(e, "key", None), str(e)
            nxt = re.compile(r"\n\s*@").search(text, i + 1)
            if not nxt:
                return
            i = nxt.end() - 1
            continue
        if key is not None:
            yield etype, key, fields
        i = j


def plain(s):
    """A LaTeX field value as plain text (roughly: see the module text)."""
    s = re.sub(r"\\([&%$_#])", r"\1", s)          # escaped characters: \& -> &
    s = re.sub(r"\\[^A-Za-z\s]", "", s)          # accent commands: \'a -> a
    s = re.sub(r"\\[A-Za-z]+\s*", "", s)           # other commands
    s = s.replace("{", "").replace("}", "").replace("~", " ")
    return re.sub(r"\s+", " ", s).strip()


def norm_title(s):
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


# --- OpenAlex ---------------------------------------------------------------

def fetch(url):
    """(record, None) or (None, why)."""
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req) as r:
            return json.loads(r.read().decode("utf-8")), None
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code}"
    except (urllib.error.URLError, OSError) as e:
        return None, f"network: {getattr(e, 'reason', e)}"
    except (ValueError, UnicodeDecodeError) as e:
        return None, f"unreadable reply: {e}"


def abstract(inv):
    if not isinstance(inv, dict) or not inv:
        return None
    words = {}
    for w, positions in inv.items():
        for p in positions or []:
            words[p] = w
    return " ".join(words[p] for p in sorted(words))


def venue(w):
    src = (w.get("primary_location") or {}).get("source") or {}
    return src.get("display_name")


def record(w):
    oa = w.get("open_access") or {}
    authors = [((a.get("author") or {}).get("display_name"))
               for a in w.get("authorships") or []]
    return {"title": w.get("title"), "year": w.get("publication_year"),
            "venue": venue(w), "authors": authors[:AUTHORS_SHOWN],
            "authors_total": len(authors), "doi": w.get("doi"),
            "retracted": w.get("is_retracted"), "oa_status": oa.get("oa_status"),
            "oa_url": oa.get("oa_url"), "abstract": abstract(w.get("abstract_inverted_index")),
            "openalex": w.get("id")}


def candidate(w):
    return {"title": w.get("title"), "year": w.get("publication_year"),
            "venue": venue(w), "doi": w.get("doi")}


def clean_doi(d):
    d = plain(d)
    return re.sub(r"^(https?://(dx\.)?doi\.org/|doi:)", "", d, flags=re.I).strip()


def clean_arxiv(e):
    e = plain(e)
    e = re.sub(r"^(arxiv:)", "", e, flags=re.I).strip()
    return re.sub(r"v\d+$", "", e)


def check(etype, key, f):
    title = plain(f.get("title", ""))
    out = {"key": key, "type": etype,
           "entry": {"title": title or None, "year": plain(f.get("year", "")) or None,
                     "venue": plain(f.get("journal") or f.get("booktitle")
                                    or f.get("publisher") or "") or None}}
    if f.get("doi"):
        doi = clean_doi(f["doi"])
        out["lookup"] = {"by": "doi", "value": doi}
        w, why = fetch(f"{OPENALEX}/works/doi:{urllib.parse.quote(doi, safe='/')}")
    elif f.get("eprint"):
        arxiv = clean_arxiv(f["eprint"])
        out["lookup"] = {"by": "arxiv", "value": arxiv}
        # arXiv registers a DOI for every preprint: 10.48550/arXiv.<id>.
        w, why = fetch(f"{OPENALEX}/works/doi:10.48550/arXiv.{urllib.parse.quote(arxiv, safe='/')}")
    elif title:
        out["lookup"] = {"by": "title", "value": title}
        q = urllib.parse.urlencode({"search": title, "per-page": TITLE_CANDIDATES})
        res, why = fetch(f"{OPENALEX}/works?{q}")
        w = None
        if res is not None:
            results = res.get("results") if isinstance(res, dict) else None
            if not isinstance(results, list):
                why = "unreadable reply: no results list"
            else:
                out["candidates"] = [candidate(r) for r in results if isinstance(r, dict)]
                want = norm_title(title)
                w = next((r for r in results if isinstance(r, dict)
                          and norm_title(r.get("title")) == want), None)
                if w is None:
                    out["found"] = False
                    return out
    else:
        out["found"] = False
        out["error"] = "the entry has no DOI, arXiv id or title to look up"
        return out
    if w is None:
        out["found"] = False
        if why == "HTTP 404":
            out["note"] = "OpenAlex has no record under this identifier"
        else:
            out["error"] = why
        return out
    if not isinstance(w, dict):
        out["found"] = False
        out["error"] = "unreadable reply: not a record"
        return out
    out["found"] = True
    out["record"] = record(w)
    return out


def main(argv):
    if not argv:
        print("usage: bibpass.py FILE.bib [FILE.bib ...]", file=sys.stderr)
        return 2
    for path in argv:
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError as e:
            print(json.dumps({"file": path, "error": f"cannot read: {e.strerror}"}), flush=True)
            continue
        for item in parse_bib(text):
            if item[0] == "error":
                print(json.dumps({"file": path, "key": item[1],
                                  "error": f"could not parse the entry: {item[2]}"}), flush=True)
                continue
            etype, key, fields = item
            try:
                obj = check(etype, key, fields)
            except Exception as e:   # one entry's surprise must not end the pass
                obj = {"key": key, "type": etype, "error": f"{type(e).__name__}: {e}"}
            obj["file"] = path
            print(json.dumps(obj, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
