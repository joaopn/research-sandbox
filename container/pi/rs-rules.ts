// rs-rules v0.6.0
//
// Claude Code's rules capability for pi, one-to-one, as an RS-owned extension.
// Shipped inside the pi dist (share/pi-agent/rs/rs-rules.ts) and named from the
// dist's canonical settings.json (`extensions: ["../rs/rs-rules.ts"]`), so every
// container that deploys pi carries it and no project can alter it. The version
// on the first line is pinned to RS_PI_RULES_VERSION in versions.env at build.
//
// What it reproduces (Claude Code's memory.md contract, item by item):
//   - `.claude/CLAUDE.md` and `CLAUDE.local.md` at the project root, plus every
//     `.claude/rules/**/*.md` WITHOUT a `paths:` frontmatter, and `~/.claude/rules`,
//     go into the system prompt at launch, in Claude's order (user rules, project
//     instructions, project rules).
//   - a rule WITH `paths:` globs is attached to the result of the first `read` of
//     a matching file (or the `write` that creates a new matching file), once per
//     session, re-armed by compaction; a subdirectory's `.claude/rules/*.md` and
//     its `CLAUDE.md` / `CLAUDE.local.md` (or `AGENTS.md` when neither exists)
//     attach when a file under that subdirectory is touched.
//   - Claude's read-before-edit rule: an `edit`, or a `write` of an existing file,
//     is refused while the file has not been read this session, and refused again
//     when the file changed on disk since it was read — with Claude's own texts.
// One guard beyond the rules, for the subagent tool pi-subagents adds: a launch
// of a single child with no brief is refused before any child starts (Claude
// Code's Task tool requires its prompt; pi-subagents treats the single child's
// `task` as optional and would start a child on an empty task).
// And it reaches every subagent pi itself runs: at session start it registers its
// own file with pi-subagents as a required child extension, so every child of the
// session — foreground and background — loads it too and gets the same launch
// rules, scoped rules and read-before-edit refusal (a Claude Code subagent gets
// the project's instructions; a foreground pi-subagents child otherwise loads no
// extension at all). Agents on pi-subagents' external-CLI runners (claude-code,
// codex-exec, …) are other programs and load nothing of pi's; a launch whose
// capability ceiling denies extensions is refused by pi-subagents.
// Not reproduced, by decision: `claudeMdExcludes`, `@path` imports,
// `--setting-sources`.
//
// And Claude Code's shell directory: a `cd` in one `bash` call carries over to
// the next as long as it stays inside the session's start directory; a command
// that leaves it is followed by "Shell cwd was reset to <start>" and the next
// command starts there again; relative paths given to read, edit, write, grep,
// find and ls resolve against that same current directory.
//
// MCP servers from a parent folder. Claude finds `.mcp.json` from a subfolder;
// pi reads project servers only from `<cwd>/.pi/mcp.json`, with no walk-up. So
// at session start, when the session folder has no `.pi/mcp.json` of its own,
// the extension registers the servers of the NEAREST parent folder's
// `.pi/mcp.json` (in a box that file is the link to the box's `.mcp.json`),
// only when pi would trust that parent folder (its entry in the agent dir's
// trust.json, else the global `defaultProjectTrust: "always"`), and entries are
// passed as written except three rules pi applies to project files: an http
// entry with `auth` is skipped, an override of a global server (an entry with
// no `command`, `url` or `type`) is skipped, and a stdio entry's `cwd` is
// resolved against the parent folder (pi spawns stdio servers in the session
// folder).
// Settings extensions load before pi's built-ins and handlers run in load
// order, so these registrations exist when the built-in MCP reads them at its
// own session start: they connect at once and the first request declares them.
// Fail-closed divergences: a command-line trust override, another extension's
// project_trust answer and a session-only trust choice are invisible here, so
// the extension may decline where pi in that folder would have trusted it.
// Residuals an extension cannot change: a server's `roots` and `!command`
// values are evaluated in the session folder; a same-name server in the global
// mcp.json wins over a registered one; the parent file's `autoEnableCodemode`
// is not carried; the shell command `pi mcp list` (which loads no extension)
// does not list these servers from a subfolder.
//
// Every handler is wrapped: a failure warns and returns nothing (a thrown
// tool_call handler would BLOCK the tool — the catch is load-bearing).

import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";
import * as crypto from "node:crypto";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { fileURLToPath } from "node:url";

export const RS_RULES_VERSION = "0.6.0";

// The system-prompt SECTION the launch rules ride in (pi wraps it as
// `<project_rules>…</project_rules>`). A section, never a returned
// `systemPrompt`: since pi 1.0 a returned prompt is FORCED — opaque text that
// overrides every section another extension sets, the built-in MCP's
// `mcp_servers` listing included.
export const RS_RULES_SECTION = "project_rules";

// Claude Code's documented budgets for a rule's `paths:` list (memory.md, the
// glob grammar): brace groups multiply; past either budget a pattern is used
// UNEXPANDED and its literal braces match nothing.
export const CLAUDE_BRACE_EXPANSION_LIMIT = 1000;
export const CLAUDE_PATTERN_BYTES_LIMIT = 4 * 1024 * 1024;

// Claude Code's two Edit-tool refusals, verbatim.
export const NOT_READ_MSG = "File has not been read yet. Read it first before writing to it.";
export const STALE_READ_MSG =
  "File has been modified since read, either by the user or by a linter. Read it again before attempting to write it.";

export const ENTRY_TYPE = "rs-rules";

// The subagent tool pi-subagents registers, and the refusal of a single-child
// launch that carries no brief.
export const SUBAGENT_TOOL = "subagent";
export const NO_BRIEF_MSG = (agent: string) =>
  `subagent: this launch names agent '${agent}' but carries no brief. ` +
  "Put the full, self-contained brief in `task` and call again.";
export const RESUME_HINT = ' To continue an existing run, use action: "resume" with id and message.';

// The id this extension registers under with pi-subagents, and where the
// pi download ships pi-subagents relative to this file: share/pi-agent/rs/
// holds rs-rules.ts and pi-subagents/package.json, a wrapper whose `version`
// names the folder the package is installed in.
export const CHILD_EXTENSION_ID = "rs-rules";
export const SUBAGENTS_WRAPPER = path.join("pi-subagents", "package.json");
export const SUBAGENTS_API = path.join("node_modules", "pi-subagents", "bundle", "required-child-extensions.js");
// Set on the process by an instance that has a UI, to a function showing a
// warning on that UI. A foreground subagent runs inside its parent's process
// without a UI, and its stderr is the parent's screen: when this is set, an
// instance without a UI hands its warnings to the parent's UI ("rs-rules
// (subagent): …"); when it is not set (a headless `pi -p`, a background child's
// own process) it writes them to stderr as before.
export const UI_FLAG = Symbol.for("rs-rules.ui.v1");
// Discovery warnings already shown on an interactive UI in this process: project
// root + message (labels are relative to the root). An instance with its own UI
// always shows its warnings (a `/rules reload` that still finds a broken file
// says so again) and records them here; a foreground child forwarding to the
// parent's UI skips what is recorded, so it never repeats the parent's warnings,
// while a rule file only the child reaches (another root, a subdirectory the
// parent never touched) is shown once.
export const WARNED = Symbol.for("rs-rules.warned.v1");

// On resume, a file whose mtime is later than its read entry's timestamp by more
// than this is stale. The slack covers the write-then-append ordering of a
// tool's own edit (its result entry is written after the file, so the file is
// never "newer" by more than the append latency) and coarse filesystem clocks.
export const RESUME_MTIME_SLACK_MS = 2000;

export type RuleKind = "rule" | "claude_md" | "claude_local" | "agents_md";
export type RuleScope = "user" | "project" | "nested";

export interface Rule {
  id: string; // realpath of the file (the once-per-session key)
  file: string; // as discovered
  label: string; // shown to the model and in /rules
  kind: RuleKind;
  scope: RuleScope;
  base: string; // directory the globs are relative to (realpath)
  paths: string[] | null; // null = unconditional
  body: string;
  compiled: Compiled | null;
}

export interface Compiled {
  regexes: RegExp[];
  invalid: string[]; // patterns that match nothing (unreadable `[`)
  unexpanded: string[]; // patterns kept literal (over budget)
}

// ---------------------------------------------------------------------------
// Frontmatter: the YAML subset Claude's docs use for `paths:`.
// ---------------------------------------------------------------------------

export interface Frontmatter {
  paths: string[] | null;
  body: string;
  error?: string;
}

// A YAML scalar: a trailing ` # comment` is dropped outside quotes; quotes are
// removed. (Claude's YAML parser does the same; a literal `#` belongs in quotes.)
function unquote(s: string): string {
  let t = s.trim();
  if (!(t.startsWith('"') || t.startsWith("'"))) {
    const hash = t.search(/(^|\s)#/);
    if (hash >= 0) t = t.slice(0, hash).trim();
  } else {
    const q = t[0];
    const close = t.indexOf(q, 1);
    if (close > 0) t = t.slice(0, close + 1);
  }
  if ((t.startsWith('"') && t.endsWith('"')) || (t.startsWith("'") && t.endsWith("'"))) {
    return t.slice(1, -1);
  }
  return t;
}

// Split an inline list on top-level commas: a brace group or a quoted string
// keeps its commas.
export function splitInlineList(inner: string): string[] {
  const out: string[] = [];
  let depth = 0;
  let quote: string | null = null;
  let cur = "";
  for (const ch of inner) {
    if (quote) {
      cur += ch;
      if (ch === quote) quote = null;
      continue;
    }
    if (ch === '"' || ch === "'") {
      quote = ch;
      cur += ch;
      continue;
    }
    if (ch === "{") depth++;
    if (ch === "}") depth = Math.max(0, depth - 1);
    if (ch === "," && depth === 0) {
      out.push(cur);
      cur = "";
      continue;
    }
    cur += ch;
  }
  if (cur.trim()) out.push(cur);
  return out.map(unquote).filter((s) => s.length > 0);
}

export function parseFrontmatter(text: string): Frontmatter {
  if (!text.startsWith("---\n") && !text.startsWith("---\r\n") && text !== "---") {
    return { paths: null, body: text };
  }
  const lines = text.split(/\r?\n/);
  let end = -1;
  for (let i = 1; i < lines.length; i++) {
    if (lines[i].trim() === "---") {
      end = i;
      break;
    }
  }
  if (end < 0) return { paths: null, body: "", error: "unclosed frontmatter" };
  const fm = lines.slice(1, end);
  const body = lines.slice(end + 1).join("\n");
  let paths: string[] | null = null;
  for (let i = 0; i < fm.length; i++) {
    const line = fm[i];
    const m = /^paths\s*:\s*(.*)$/.exec(line);
    if (!m) continue;
    const rest = m[1].trim();
    if (rest === "" || rest === "|" || rest === ">") {
      // block list on the following lines
      const items: string[] = [];
      for (let j = i + 1; j < fm.length; j++) {
        const lt = fm[j].trim();
        if (lt === "" || lt.startsWith("#")) continue; // blank and comment lines inside the list
        const lm = /^\s*-(?:\s+(.*))?$/.exec(fm[j]);
        if (!lm) break;
        const item = unquote(lm[1] ?? "");
        if (item) items.push(item); // a bare `-` or an empty scalar is no pattern
      }
      paths = items;
    } else if (rest.startsWith("[")) {
      const close = rest.lastIndexOf("]");
      if (close < 0) return { paths: null, body, error: "malformed paths list" };
      paths = splitInlineList(rest.slice(1, close));
    } else {
      const one = unquote(rest);
      paths = one ? [one] : [];
    }
    break;
  }
  if (paths !== null && paths.length === 0) paths = null;
  return { paths, body };
}

// ---------------------------------------------------------------------------
// Comment stripping: block-level HTML comments on their own lines, outside
// fenced code.
// ---------------------------------------------------------------------------

export function stripBlockComments(body: string): string {
  const lines = body.split("\n");
  const out: string[] = [];
  let fence: string | null = null;
  let inComment = false;
  for (const line of lines) {
    const t = line.trim();
    if (!inComment) {
      const f = /^(`{3,}|~{3,})/.exec(t);
      if (f) {
        if (!fence) fence = f[1][0].repeat(f[1].length);
        else if (t.startsWith(fence)) fence = null;
        out.push(line);
        continue;
      }
      if (fence) {
        out.push(line);
        continue;
      }
      if (t.startsWith("<!--")) {
        const close = t.indexOf("-->", 2); // `<!-->` closes itself
        if (close < 0) {
          inComment = true; // opens a multi-line block comment
          continue;
        }
        if (close === t.length - 3) continue; // a whole-line comment
        // an inline comment followed by text is not a block comment: keep the line
      }
      out.push(line);
    } else {
      const close = t.indexOf("-->");
      if (close >= 0) {
        inComment = false;
        const rest = t.slice(close + 3).trim();
        if (rest) out.push(rest); // text after the closer survives
      }
    }
  }
  return out.join("\n");
}

// ---------------------------------------------------------------------------
// Globs: brace expansion with Claude's budget, then a per-pattern regex.
// ---------------------------------------------------------------------------

// Expand the FIRST top-level brace group; returns null when there is none.
function expandOnce(p: string): string[] | null {
  let depth = 0;
  let start = -1;
  for (let i = 0; i < p.length; i++) {
    const ch = p[i];
    if (ch === "\\") {
      i++;
      continue;
    }
    if (ch === "{") {
      if (depth === 0) start = i;
      depth++;
    } else if (ch === "}") {
      depth--;
      if (depth === 0 && start >= 0) {
        const inner = p.slice(start + 1, i);
        const alts = splitTopLevel(inner);
        if (alts.length < 2) {
          // `{a}` is not a group: keep looking past it
          start = -1;
          continue;
        }
        const head = p.slice(0, start);
        const tail = p.slice(i + 1);
        return alts.map((a) => head + a + tail);
      }
    }
  }
  return null;
}

function splitTopLevel(inner: string): string[] {
  const out: string[] = [];
  let depth = 0;
  let cur = "";
  for (let i = 0; i < inner.length; i++) {
    const ch = inner[i];
    if (ch === "\\") {
      cur += ch + (inner[i + 1] ?? "");
      i++;
      continue;
    }
    if (ch === "{") depth++;
    if (ch === "}") depth--;
    if (ch === "," && depth === 0) {
      out.push(cur);
      cur = "";
      continue;
    }
    cur += ch;
  }
  out.push(cur);
  return out;
}

export interface Expansion {
  patterns: string[];
  overBudget: boolean;
}

// Fully expand one pattern under the budget (count + bytes, shared by the
// whole list through `budget`). Over budget: the ORIGINAL pattern, unexpanded.
export function expandBraces(pattern: string, budget = { count: 0, bytes: 0 }): Expansion {
  let work = [pattern];
  let done: string[] = [];
  let guard = 0;
  while (work.length) {
    const p = work.pop() as string;
    const alts = expandOnce(p);
    if (!alts) {
      done.push(p);
    } else {
      work.push(...alts);
    }
    if (++guard > CLAUDE_BRACE_EXPANSION_LIMIT * 4) break;
    const count = budget.count + done.length + work.length;
    const bytes = budget.bytes + done.reduce((n, s) => n + s.length, 0) + work.reduce((n, s) => n + s.length, 0);
    if (count > CLAUDE_BRACE_EXPANSION_LIMIT || bytes > CLAUDE_PATTERN_BYTES_LIMIT) {
      return { patterns: [pattern], overBudget: true };
    }
  }
  done = done.reverse();
  budget.count += done.length;
  budget.bytes += done.reduce((n, s) => n + s.length, 0);
  return { patterns: done, overBudget: false };
}

// One glob → an anchored RegExp over a `/`-separated relative path. `literalBraces`
// keeps `{`/`}` as literal characters (the over-budget posture). Returns null for
// an unreadable pattern (an unclosed `[`), which then matches nothing.
export function globToRegex(glob: string, literalBraces = false): RegExp | null {
  let g = glob; // a backslash is an ESCAPE in a glob (`\[`), never a separator
  if (g.startsWith("./")) g = g.slice(2);
  if (g.startsWith("/")) g = g.slice(1);
  let re = "";
  let i = 0;
  while (i < g.length) {
    const ch = g[i];
    if (ch === "\\") {
      const next = g[i + 1];
      if (next === undefined) return null;
      re += escapeRe(next);
      i += 2;
      continue;
    }
    if (ch === "*") {
      const wholeSegment = (i === 0 || g[i - 1] === "/") && (g[i + 2] === undefined || g[i + 2] === "/");
      if (g[i + 1] === "*" && !wholeSegment) {
        // `a**b`: a doubled star inside a segment is just `*` (minimatch semantics)
        re += "[^/]*";
        i += 2;
        continue;
      }
      if (g[i + 1] === "*") {
        // `**`: zero or more directories. Consume a following `/` so `a/**/b`
        // also matches `a/b`; a trailing `**` matches everything below.
        let j = i + 2;
        if (g[j] === "/") {
          j++;
          re += "(?:.*/)?";
        } else {
          re += ".*";
        }
        i = j;
        continue;
      }
      re += "[^/]*";
      i++;
      continue;
    }
    if (ch === "?") {
      re += "[^/]";
      i++;
      continue;
    }
    if (ch === "[") {
      const close = findClassEnd(g, i);
      if (close < 0) return null; // unreadable bracket: this pattern matches nothing
      let cls = g.slice(i + 1, close);
      let neg = false;
      if (cls.startsWith("!") || cls.startsWith("^")) {
        neg = true;
        cls = cls.slice(1);
      }
      cls = cls.replace(/\\/g, "\\\\").replace(/\]/g, "\\]");
      re += "[" + (neg ? "^/" : "") + cls + "]";
      i = close + 1;
      continue;
    }
    if (ch === "{" || ch === "}") {
      // only reachable when expansion was skipped (over budget) or the group
      // was not a real alternation; literal either way
      re += escapeRe(ch);
      i++;
      continue;
    }
    re += escapeRe(ch);
    i++;
  }
  try {
    return new RegExp("^" + re + "$");
  } catch {
    return null;
  }
}

function findClassEnd(g: string, open: number): number {
  let i = open + 1;
  if (g[i] === "!" || g[i] === "^") i++;
  if (g[i] === "]") i++; // a leading `]` is literal
  for (; i < g.length; i++) {
    if (g[i] === "\\") {
      i++;
      continue;
    }
    if (g[i] === "]") return i;
    if (g[i] === "/") return -1; // a class never spans a separator
  }
  return -1;
}

function escapeRe(ch: string): string {
  return /[.*+?^${}()|[\]\\/]/.test(ch) ? "\\" + ch : ch;
}

export function compileGlobs(patterns: string[]): Compiled {
  const budget = { count: 0, bytes: 0 };
  const regexes: RegExp[] = [];
  const invalid: string[] = [];
  const unexpanded: string[] = [];
  for (const p of patterns) {
    const ex = expandBraces(p, budget);
    if (ex.overBudget) unexpanded.push(p);
    for (const q of ex.patterns) {
      const re = globToRegex(q, ex.overBudget);
      if (re) regexes.push(re);
      else invalid.push(q);
    }
  }
  return { regexes, invalid, unexpanded };
}

export function matchTarget(compiled: Compiled, rel: string): boolean {
  const r = rel.replace(/\\/g, "/");
  return compiled.regexes.some((re) => re.test(r));
}

// ---------------------------------------------------------------------------
// Discovery.
// ---------------------------------------------------------------------------

function realpathOrNull(p: string): string | null {
  try {
    return fs.realpathSync(p);
  } catch {
    return null;
  }
}

function isDir(p: string): boolean {
  try {
    return fs.statSync(p).isDirectory();
  } catch {
    return false;
  }
}

function isFile(p: string): boolean {
  try {
    return fs.statSync(p).isFile();
  } catch {
    return false;
  }
}

// The nearest directory at or above `cwd` carrying `.claude/`; else `cwd`.
export function findProjectRoot(cwd: string, home?: string): string {
  let dir = path.resolve(cwd);
  const homeReal = home ? (realpathOrNull(home) ?? path.resolve(home)) : null;
  for (;;) {
    // `~/.claude` is Claude Code's own directory (user rules, memory), never a
    // project: a session under $HOME with no project `.claude/` gets cwd as root.
    if (isDir(path.join(dir, ".claude")) && (realpathOrNull(dir) ?? dir) !== homeReal) return dir;
    const parent = path.dirname(dir);
    if (parent === dir) return path.resolve(cwd);
    dir = parent;
  }
}

// Every *.md under `dir`, recursive, lexical, symlinks followed with a realpath
// cycle guard; dangling links and unreadable entries skipped.
export function listMarkdown(dir: string, warn: (m: string) => void): string[] {
  const out: string[] = [];
  const seen = new Set<string>();
  const walk = (d: string) => {
    const real = realpathOrNull(d);
    if (!real || seen.has(real)) return;
    seen.add(real);
    let entries: fs.Dirent[];
    try {
      entries = fs.readdirSync(d, { withFileTypes: true });
    } catch (e) {
      warn(`cannot read ${d}: ${String(e)}`);
      return;
    }
    entries.sort((a, b) => (a.name < b.name ? -1 : a.name > b.name ? 1 : 0));
    for (const ent of entries) {
      const full = path.join(d, ent.name);
      if (isDir(full)) walk(full);
      else if (ent.name.endsWith(".md") && isFile(full)) out.push(full);
    }
  };
  walk(dir);
  return out;
}

function loadRule(file: string, kind: RuleKind, scope: RuleScope, base: string, label: string, warn: (m: string) => void): Rule | null {
  const real = realpathOrNull(file);
  if (!real) {
    warn(`skipping ${file}: unreadable`);
    return null;
  }
  let text: string;
  try {
    text = fs.readFileSync(real, "utf8");
  } catch (e) {
    warn(`skipping ${file}: ${String(e)}`);
    return null;
  }
  let paths: string[] | null = null;
  let body = text;
  if (kind === "rule") {
    const fm = parseFrontmatter(text);
    if (fm.error) {
      warn(`skipping ${label}: ${fm.error}`);
      return null;
    }
    paths = fm.paths;
    body = fm.body;
  }
  body = stripBlockComments(body).trim();
  const compiled = paths ? compileGlobs(paths) : null;
  if (compiled) {
    for (const bad of compiled.invalid) warn(`${label}: pattern ${JSON.stringify(bad)} is unreadable and matches nothing`);
    for (const u of compiled.unexpanded) warn(`${label}: pattern ${JSON.stringify(u)} exceeds the brace-expansion budget and is used literally`);
  }
  return { id: real, file, label, kind, scope, base, paths, body, compiled };
}

export interface Discovered {
  root: string; // realpath
  rules: Rule[]; // user + project (root-level) rules, launch and scoped
  claudeMd: Rule | null; // <root>/.claude/CLAUDE.md
  claudeLocal: Rule | null; // <root>/CLAUDE.local.md
}

export function discoverRules(cwd: string, home: string, warn: (m: string) => void): Discovered {
  const rootPath = findProjectRoot(cwd, home);
  const root = realpathOrNull(rootPath) ?? rootPath;
  const rules: Rule[] = [];
  const userDir = path.join(home, ".claude", "rules");
  if (isDir(userDir)) {
    for (const f of listMarkdown(userDir, warn)) {
      const r = loadRule(f, "rule", "user", realpathOrNull(cwd) ?? cwd, "~/.claude/rules/" + path.relative(userDir, f), warn);
      if (r) rules.push(r);
    }
  }
  const projDir = path.join(root, ".claude", "rules");
  if (isDir(projDir)) {
    for (const f of listMarkdown(projDir, warn)) {
      const r = loadRule(f, "rule", "project", root, ".claude/rules/" + path.relative(projDir, f), warn);
      if (r) rules.push(r);
    }
  }
  const cm = path.join(root, ".claude", "CLAUDE.md");
  const claudeMd = isFile(cm) ? loadRule(cm, "claude_md", "project", root, ".claude/CLAUDE.md", warn) : null;
  const cl = path.join(root, "CLAUDE.local.md");
  const claudeLocal = isFile(cl) ? loadRule(cl, "claude_local", "project", root, "CLAUDE.local.md", warn) : null;
  return { root, rules, claudeMd, claudeLocal };
}

// A subdirectory's own rules: its `.claude/rules/**/*.md` (globs relative to
// the subdirectory) and its `CLAUDE.md` / `CLAUDE.local.md` — or `AGENTS.md`
// when neither exists — as implicit rules for everything under it.
export function discoverNested(dir: string, root: string, warn: (m: string) => void): Rule[] {
  const rel = path.relative(root, dir).replace(/\\/g, "/");
  const out: Rule[] = [];
  const rulesDir = path.join(dir, ".claude", "rules");
  if (isDir(rulesDir)) {
    for (const f of listMarkdown(rulesDir, warn)) {
      const r = loadRule(f, "rule", "nested", dir, rel + "/.claude/rules/" + path.relative(rulesDir, f), warn);
      if (!r) continue;
      if (!r.paths) {
        // a nested rule without `paths:` is scoped by its own subtree (Claude's
        // "loaded on demand when files in that subdirectory are read")
        r.paths = ["**"];
        r.compiled = compileGlobs(r.paths);
      }
      out.push(r);
    }
  }
  const cm = path.join(dir, "CLAUDE.md");
  const cl = path.join(dir, "CLAUDE.local.md");
  const ag = path.join(dir, "AGENTS.md");
  const implicit = (file: string, kind: RuleKind, label: string) => {
    const r = loadRule(file, kind, "nested", dir, label, warn);
    if (r) {
      r.paths = ["**"];
      r.compiled = compileGlobs(r.paths);
      out.push(r);
    }
  };
  if (isFile(cm)) implicit(cm, "claude_md", rel + "/CLAUDE.md");
  if (isFile(cl)) implicit(cl, "claude_local", rel + "/CLAUDE.local.md");
  if (!isFile(cm) && !isFile(cl) && isFile(ag)) implicit(ag, "agents_md", rel + "/AGENTS.md");
  return out;
}

// ---------------------------------------------------------------------------
// Target normalisation (pi's own path handling, reproduced).
// ---------------------------------------------------------------------------

// pi's own spelling rules for a tool path, without resolving it: a leading `@`,
// a file:// URL, unicode spaces, a leading `~`.
export function expandToolPath(raw: string, home: string, trim = true): string {
  let p = String(raw ?? "");
  if (trim) p = p.trim();
  if (p.startsWith("@")) p = p.slice(1);
  if (p.startsWith("file://")) {
    try {
      p = decodeURIComponent(p.slice("file://".length));
    } catch {
      p = p.slice("file://".length);
    }
  }
  p = p.replace(/[\u00a0\u1680\u2000-\u200b\u202f\u205f\u3000\ufeff]/g, " ");
  if (p === "~" || p.startsWith("~/")) p = path.join(home, p.slice(1));
  return p;
}

export function normalizeTarget(raw: string, cwd: string, home: string): string {
  const abs = path.resolve(cwd, expandToolPath(raw, home));
  return realpathOrNull(abs) ?? path.normalize(abs);
}

// ---------------------------------------------------------------------------
// The shell's directory.
// ---------------------------------------------------------------------------

export const SHELL_CWD_SECTION = "rs_shell_cwd";
export const CWD_FILE_PREFIX = "rs-cwd-";
export const RESET_NOTICE = (start: string) => `Shell cwd was reset to ${start}`;
// The shells whose syntax the command prefix uses (`trap … EXIT`, `cd --`, `>|`).
export const POSIX_SHELLS = ["bash", "sh", "dash", "zsh"];
// The file tools whose `path` follows the shell's directory; for the last three
// `path` is optional and defaults to the session's directory.
export const PATH_TOOLS_REQUIRED = ["read", "edit", "write"];
export const PATH_TOOLS_OPTIONAL = ["grep", "find", "ls"];

// A single-quoted shell word.
export function shellQuote(s: string): string {
  return "'" + s.replace(/'/g, `'\\''`) + "'";
}

// Whether `target` is `base` or below it (both absolute, already real paths).
export function isInside(base: string, target: string): boolean {
  const rel = path.relative(base, target);
  return rel === "" || (rel !== ".." && !rel.startsWith(".." + path.sep) && !path.isAbsolute(rel));
}

// Whether a configured shellPath is one the prefix works in; none = pi's default
// (bash, else sh).
export function shellIsPosix(shellPath: unknown): boolean {
  if (shellPath === undefined || shellPath === null || shellPath === "") return true;
  if (typeof shellPath !== "string") return false;
  return POSIX_SHELLS.includes(path.basename(shellPath.trim()));
}

// The lines put in front of a command: record where it ends (also on `exit`;
// a killed command records nothing), then start in `dir` unless that is the
// start directory already.
export function shellPrefix(file: string, dir: string, start: string): string {
  const trap = `trap ${shellQuote(`pwd -P >| ${shellQuote(file)} 2>/dev/null`)} EXIT\n`;
  return dir === start ? trap : trap + `cd -- ${shellQuote(dir)} || exit 1\n`;
}

// Where the next command starts, from what a command recorded: inside the start
// directory → there; outside → back at the start with the notice; nothing
// recorded (its own EXIT trap replaced ours, or it was killed) → null (keep).
export function cwdAfter(recorded: string | null, start: string): { dir: string; notice: string | null } | null {
  if (recorded === null) return null;
  const d = recorded.trim();
  if (!d || !path.isAbsolute(d)) return null;
  const real = realpathOrNull(d) ?? d;
  return isInside(start, real) ? { dir: real, notice: null } : { dir: start, notice: RESET_NOTICE(start) };
}

// A tool path made absolute against the shell's directory, or null when it is
// already absolute (pi's spelling rules applied first, untrimmed as pi resolves
// the path it acts on, never a realpath).
export function followPath(raw: string, cwdNow: string, home: string): string | null {
  const p = expandToolPath(raw, home, false);
  if (path.isAbsolute(p)) return null;
  return path.resolve(cwdNow, p);
}

// The shellPath values that may be in force: the project's .pi/settings.json
// (pi skips it for an untrusted project) and the agent dir's settings.json. Both
// must be POSIX for the prefix to be used.
function configuredShellPaths(cwd: string, home: string): unknown[] {
  const read = (f: string): Record<string, unknown> | null => {
    try {
      return JSON.parse(fs.readFileSync(f, "utf8"));
    } catch {
      return null;
    }
  };
  const agentDir = process.env.PI_CODING_AGENT_DIR
    ? expandToolPath(process.env.PI_CODING_AGENT_DIR, home)
    : path.join(home, ".pi", "agent");
  return [read(path.join(cwd, ".pi", "settings.json"))?.shellPath, read(path.join(agentDir, "settings.json"))?.shellPath];
}

function relTo(base: string, target: string): string | null {
  const rel = path.relative(base, target).replace(/\\/g, "/");
  if (rel === "" || rel.startsWith("../") || rel === ".." || path.isAbsolute(rel)) return null;
  return rel;
}

// ---------------------------------------------------------------------------
// MCP servers of the nearest parent folder (see the header).
// ---------------------------------------------------------------------------

export interface AncestorMcpFile {
  dir: string; // the parent folder, as reached by the lexical walk
  file: string; // <dir>/.pi/mcp.json
}

const piMcpFile = (dir: string) => path.join(dir, ".pi", "mcp.json");

function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

function stripBom(text: string): string {
  return text.charCodeAt(0) === 0xfeff ? text.slice(1) : text;
}

// pi's own `~` rule: exactly `~` and `~/…` are home-relative, nothing else.
function expandHome(p: string, home: string): string {
  if (p === "~") return home;
  if (p.startsWith("~/")) return path.join(home, p.slice(2));
  return p;
}

// The nearest folder STRICTLY above `cwd` holding `.pi/mcp.json`, or null when
// `cwd` has its own (pi reads that one itself and never merges two projects'
// files) or no parent has one. `existsSync` follows links, as pi's check does,
// so a dangling link counts as absent at every level.
export function findAncestorMcpFile(cwd: string): AncestorMcpFile | null {
  let dir = path.resolve(cwd);
  if (fs.existsSync(piMcpFile(dir))) return null;
  for (;;) {
    const parent = path.dirname(dir);
    if (parent === dir) return null;
    dir = parent;
    const file = piMcpFile(dir);
    if (fs.existsSync(file)) return { dir, file };
  }
}

// pi's agent dir: PI_CODING_AGENT_DIR (`~` expanded) else ~/.pi/agent.
export function piAgentDir(env: Record<string, string | undefined>, home: string): string {
  const v = env.PI_CODING_AGENT_DIR;
  if (v) return expandHome(v, home);
  return path.join(home, ".pi", "agent");
}

// Would pi trust `dir` as a project folder? Mirrors pi's decision for a folder
// that has a `.pi/mcp.json`: the session must be trusted, then the nearest
// trust.json entry for realpath(dir) walking up (null entries skipped), else
// the GLOBAL settings' `defaultProjectTrust: "always"`. The trust store is read
// the way pi reads it (one BOM stripped, a plain object, every value
// true/false/null) and an invalid store refuses outright. Never throws.
export function ancestorTrusted(dir: string, agentDir: string, sessionTrusted: boolean): { trusted: boolean; reason?: string } {
  if (!sessionTrusted) return { trusted: false, reason: "the session's own folder is not trusted" };
  const storePath = path.join(agentDir, "trust.json");
  let store: Record<string, unknown> = {};
  if (fs.existsSync(storePath)) {
    let parsed: unknown;
    try {
      parsed = JSON.parse(stripBom(fs.readFileSync(storePath, "utf8")));
    } catch (e) {
      return { trusted: false, reason: `trust store ${storePath} unreadable: ${e instanceof Error ? e.message : String(e)}` };
    }
    if (!isRecord(parsed)) return { trusted: false, reason: `trust store ${storePath} unreadable: expected an object` };
    for (const [key, value] of Object.entries(parsed)) {
      if (value !== true && value !== false && value !== null) {
        return { trusted: false, reason: `trust store ${storePath} unreadable: value for ${JSON.stringify(key)} must be true, false, or null` };
      }
    }
    store = parsed;
  }
  let cur = realpathOrNull(dir) ?? path.resolve(dir);
  for (;;) {
    const v = store[cur];
    if (v === true) return { trusted: true };
    if (v === false) return { trusted: false, reason: `${dir} is not trusted (${storePath}: ${JSON.stringify(cur)} is false)` };
    const parent = path.dirname(cur);
    if (parent === cur) break;
    cur = parent;
  }
  let defaultTrust: unknown;
  try {
    const settings = JSON.parse(stripBom(fs.readFileSync(path.join(agentDir, "settings.json"), "utf8")));
    if (isRecord(settings)) defaultTrust = settings.defaultProjectTrust;
  } catch {
    /* missing or unparsable: the default ("ask") applies */
  }
  if (defaultTrust === "always") return { trusted: true };
  return { trusted: false, reason: `${dir} is not trusted (no trust entry, and defaultProjectTrust is not "always")` };
}

function isHttpEntry(v: Record<string, unknown>): boolean {
  return typeof v.url === "string" && (v.type === undefined || v.type === "http" || v.type === "streamable-http");
}

function isStdioEntry(v: Record<string, unknown>): boolean {
  return !isHttpEntry(v) && typeof v.command === "string" && (v.type === undefined || v.type === "stdio");
}

// The servers of the parent folder's file, ready to register: every entry as
// written, except an http entry with `auth` (skipped — pi refuses `auth` in a
// project file, so a repository cannot pick where a credential goes), an entry
// with no `command`, `url` or `type` (skipped — in a project file that is an
// override of a global server's settings, which a registration cannot be: a
// same-name global server always wins over a registered one) and a
// stdio entry's `cwd` (absent → `dir`; `~` / `~/…` kept for pi to expand;
// anything else resolved against `dir`, which keeps an absolute path). A
// malformed entry is passed through for pi's own validation to refuse.
export function ancestorServers(file: string, dir: string): { servers: Array<[string, unknown]>; skipped: string[]; error?: string } {
  const servers: Array<[string, unknown]> = [];
  const skipped: string[] = [];
  let parsed: unknown;
  try {
    parsed = JSON.parse(stripBom(fs.readFileSync(file, "utf8")));
  } catch (e) {
    return { servers, skipped, error: `${file}: ${e instanceof Error ? e.message : String(e)}` };
  }
  if (!isRecord(parsed) || (parsed.mcpServers !== undefined && !isRecord(parsed.mcpServers))) {
    return { servers, skipped, error: `${file}: expected an object with an "mcpServers" object` };
  }
  for (const [name, value] of Object.entries((parsed.mcpServers ?? {}) as Record<string, unknown>)) {
    if (isRecord(value) && value.command === undefined && value.url === undefined && value.type === undefined) {
      skipped.push(`${name}: an override of a global server, which pi applies only in ${dir} itself`);
      continue;
    }
    if (isRecord(value) && "url" in value && value.auth) {
      skipped.push(`${name}: auth is only allowed in the global mcp.json`);
      continue;
    }
    if (isRecord(value) && isStdioEntry(value)) {
      const cfg: Record<string, unknown> = { ...value };
      const cwd = value.cwd;
      if (cwd === undefined) cfg.cwd = dir;
      else if (typeof cwd === "string" && cwd !== "~" && !cwd.startsWith("~/")) cfg.cwd = path.resolve(dir, cwd);
      servers.push([name, cfg]);
      continue;
    }
    servers.push([name, value]);
  }
  return { servers, skipped };
}

// ---------------------------------------------------------------------------
// The subagent brief guard.
// ---------------------------------------------------------------------------

const nonEmptyString = (v: unknown): v is string => typeof v === "string" && v.trim() !== "";
const present = (o: unknown, key: string): boolean =>
  !!o && typeof o === "object" && !Array.isArray(o) && (o as Record<string, unknown>)[key] !== undefined &&
  (o as Record<string, unknown>)[key] !== null;

// The refusal for a `subagent` call, or null when the call passes. Refused: a
// launch of ONE child — it names an agent and carries none of the shapes that
// are not a single launch (an `action`, a `workflow` or an inline workflow
// script `options.script`, a `tasks` batch, a `chain`) — whose `task` is
// missing, not a string, or whitespace only. Every other shape passes untouched:
// pi-subagents validates its own shapes (an empty `action` string, say, is its
// own error), the guard only closes the one it accepts.
export function subagentLaunchRefusal(input: unknown): string | null {
  if (!input || typeof input !== "object" || Array.isArray(input)) return null;
  const p = input as Record<string, unknown>;
  if (!nonEmptyString(p.agent)) return null;
  if (present(p, "action") || present(p, "workflow") || present(p, "tasks") || present(p, "chain")) return null;
  if (present(p.options, "script")) return null;
  if (nonEmptyString(p.task)) return null;
  const resume = nonEmptyString(p.id) && nonEmptyString(p.message);
  return NO_BRIEF_MSG(p.agent.trim()) + (resume ? RESUME_HINT : "");
}

// ---------------------------------------------------------------------------
// Reaching subagents.
// ---------------------------------------------------------------------------

// This file's absolute path (what pi-subagents is told to load), or null.
export function selfPath(): string | null {
  try {
    return fileURLToPath(import.meta.url);
  } catch {
    return null;
  }
}

// The absolute path of the shipped pi-subagents' registration module, found
// from this file's folder through the wrapper's `version`; null when the
// wrapper, its version or the module is missing. Never a scan of the version
// folders: older ones stay behind after a re-deploy.
export function subagentsApiPath(self: string | null): string | null {
  if (!self) return null;
  try {
    const dir = path.dirname(self);
    const ver = JSON.parse(fs.readFileSync(path.join(dir, SUBAGENTS_WRAPPER), "utf8"))?.version;
    if (typeof ver !== "string" || !/^[0-9A-Za-z][0-9A-Za-z.+-]*$/.test(ver)) return null;
    const api = path.join(dir, path.dirname(SUBAGENTS_WRAPPER), ver, SUBAGENTS_API);
    return isFile(api) ? api : null;
  } catch {
    return null;
  }
}

// ---------------------------------------------------------------------------
// The extension.
// ---------------------------------------------------------------------------

interface ToolCallEvt {
  toolName: string;
  toolCallId: string;
  input: Record<string, unknown>;
}
interface ToolResultEvt extends ToolCallEvt {
  content: unknown[];
  structuredContent?: unknown;
  isError?: boolean;
  parentToolCallId?: string;
}

export default function rsRules(pi: ExtensionAPI) {
  const home = os.homedir();
  let disc: Discovered | null = null;
  const nested = new Map<string, Rule[]>(); // realpath dir -> its rules
  const attached = new Set<string>();
  const readMap = new Map<string, number>(); // realpath -> mtimeMs at the read
  let uiCtx: ExtensionContext | null = null;
  let registration: { dispose(): void } | null = null;
  // The shell's directory (see the header): the session's start directory (real
  // path), where the next command starts, and the calls in flight.
  let startDir = "";
  let cwdNow = "";
  let shellOn = false;
  let seq = 0;
  let appliedSeq = 0;
  const sessionTag = crypto.randomBytes(6).toString("hex");
  const inFlight = new Map<string, { seq: number; file: string; topLevel: boolean; notice: string | null }>();
  const cwdFiles = new Set<string>(); // every per-call file named, removed by name
  const fileNotices = new Map<string, string>(); // toolCallId -> reset notice for a file tool's result

  const hasUI = () => !!(uiCtx && (uiCtx as any).hasUI);
  const parentUI = (): ((line: string) => void) | null => {
    const f = (globalThis as any)[UI_FLAG];
    return typeof f === "function" ? f : null;
  };

  const warn = (msg: string) => {
    const line = `rs-rules: ${msg}`;
    try {
      if (hasUI()) (uiCtx as any).ui.notify(line, "warning");
      else if (parentUI()) (parentUI() as (l: string) => void)(`rs-rules (subagent): ${msg}`);
      else process.stderr.write(line + "\n");
    } catch {
      /* never throw out of a warning */
    }
  };
  // Discovery warnings name the project's rule files (see WARNED): shown and
  // recorded with a UI, skipped when already recorded on the forwarding path;
  // stderr keeps every one, as before.
  let discoveryRoot = "";
  const discoveryWarn = (msg: string) => {
    if (hasUI() || parentUI()) {
      const g = globalThis as any;
      const seen: Set<string> = g[WARNED] instanceof Set ? g[WARNED] : (g[WARNED] = new Set<string>());
      const key = `${discoveryRoot}\0${msg}`;
      if (!hasUI() && seen.has(key)) return;
      seen.add(key);
    }
    warn(msg);
  };

  const guarded = <T>(name: string, fn: () => T): T | undefined => {
    try {
      return fn();
    } catch (e) {
      warn(`${name} failed: ${e instanceof Error ? e.message : String(e)}`);
      return undefined;
    }
  };

  const guardedAsync = async (name: string, fn: () => Promise<void>): Promise<void> => {
    try {
      await fn();
    } catch (e) {
      warn(`${name} failed: ${e instanceof Error ? e.message : String(e)}`);
    }
  };

  // Names this instance registered from a parent folder's `.pi/mcp.json`.
  const mcpRegistered = new Set<string>();

  const syncAncestorMcp = (ctx: ExtensionContext) => {
    const wanted = new Map<string, unknown>();
    // The file walk first: a session with no parent file never consults trust.
    const found = findAncestorMcpFile(ctx.cwd);
    if (found) {
      const sessionTrusted = (ctx as any).isProjectTrusted() === true;
      const t = ancestorTrusted(found.dir, piAgentDir(process.env, home), sessionTrusted);
      if (!t.trusted) {
        warn(`MCP servers of ${found.file} not registered: ${t.reason}`);
      } else {
        const r = ancestorServers(found.file, found.dir);
        if (r.error) warn(`MCP servers not registered: ${r.error}`);
        for (const s of r.skipped) warn(`MCP server ${s} (${found.file}), skipped`);
        for (const [name, cfg] of r.servers) wanted.set(name, cfg);
      }
    }
    for (const name of [...mcpRegistered]) {
      if (wanted.has(name)) continue;
      mcpRegistered.delete(name);
      try {
        pi.unregisterMcpServer(name);
      } catch (e) {
        warn(`MCP server ${name}: unregister failed: ${e instanceof Error ? e.message : String(e)}`);
      }
    }
    for (const [name, cfg] of wanted) {
      try {
        pi.registerMcpServer(name, cfg as any);
        mcpRegistered.add(name);
      } catch (e) {
        warn(`MCP server ${name} (${found?.file}) not registered: ${e instanceof Error ? e.message : String(e)}`);
        // A config this instance registered earlier must not stay connected
        // after the file changed it into one pi refuses.
        if (mcpRegistered.delete(name)) {
          try {
            pi.unregisterMcpServer(name);
          } catch {
            /* already gone */
          }
        }
      }
    }
  };

  const unregister = () => {
    const r = registration;
    registration = null;
    if (r) r.dispose();
  };

  // Register this file as a required child extension for the session, so every
  // subagent it launches loads it. pi-subagents missing (removed by hand) → no
  // registration and, with a UI, one warning; never an error.
  const registerForChildren = async (ctx: ExtensionContext) => {
    unregister();
    const self = selfPath();
    const api = subagentsApiPath(self);
    if (!self || !api) {
      if ((ctx as any).hasUI) warn("pi-subagents not found beside rs-rules: subagents will not load the project rules");
      return;
    }
    const mod = await import(api);
    const sessionId = (ctx.sessionManager as any).getSessionId();
    registration = mod.registerRequiredChildExtensions({ sessionId, extensions: [{ id: CHILD_EXTENSION_ID, path: self }] });
  };

  const rediscover = (ctx: ExtensionContext) => {
    const r = findProjectRoot(ctx.cwd, home);
    discoveryRoot = realpathOrNull(r) ?? r;
    disc = discoverRules(ctx.cwd, home, discoveryWarn);
    nested.clear();
  };

  const mtimeOf = (real: string): number | null => {
    try {
      return fs.statSync(real).mtimeMs;
    } catch {
      return null;
    }
  };

  // Every rule that could govern `target`: root-level scoped rules, user rules,
  // and the nested rules of every directory between the root and the target.
  const candidates = (target: string): Array<{ rule: Rule; rel: string }> => {
    if (!disc) return [];
    const out: Array<{ rule: Rule; rel: string }> = [];
    for (const rule of disc.rules) {
      if (!rule.compiled) continue;
      const rel = relTo(rule.base, target);
      if (rel !== null) out.push({ rule, rel });
    }
    const relRoot = relTo(disc.root, target);
    if (relRoot !== null) {
      const segs = relRoot.split("/").slice(0, -1);
      let dir = disc.root;
      for (const s of segs) {
        dir = path.join(dir, s);
        const real = realpathOrNull(dir) ?? dir;
        if (!nested.has(real)) nested.set(real, discoverNested(dir, disc.root, discoveryWarn));
        for (const rule of nested.get(real) as Rule[]) {
          const rel = relTo(rule.base, target);
          if (rel !== null) out.push({ rule, rel });
        }
      }
    }
    return out;
  };

  const rebuildFromBranch = (ctx: ExtensionContext) => {
    attached.clear();
    readMap.clear();
    const pending = new Map<string, { name: string; args: Record<string, unknown> }>();
    let entries: any[] = [];
    try {
      entries = (ctx.sessionManager as any).getBranch();
    } catch {
      return;
    }
    // Where each recorded call's file is: the absolute target the extension
    // recorded beside the call (from 0.4.0 on). A call recorded without one is
    // resolved the old way, against the session's directory — except in a
    // session branched from another (a fork-context subagent, a /fork): its
    // copied entries were recorded in the PARENT's directory, which the header
    // does not keep (measured: a fork-context child's header carries the child's
    // launch directory), so such a call is not taken as a read (fail-closed: the
    // file must be read again before an edit).
    const resolved = new Map<string, string>(); // toolCallId -> recorded target
    let base: string | null = ctx.cwd;
    try {
      const h = (ctx.sessionManager as any).getHeader?.();
      if (h?.parentSession) base = null;
      else if (typeof h?.cwd === "string" && h.cwd) base = h.cwd;
    } catch {
      /* keep ctx.cwd */
    }
    for (const entry of entries) {
      if (entry?.type === "custom" && entry.customType === ENTRY_TYPE) {
        const data = entry.data ?? {};
        if (data.reset) attached.clear();
        if (typeof data.attached === "string") attached.add(data.attached);
        if (typeof data.read === "string" && typeof data.toolCallId === "string") resolved.set(data.toolCallId, data.read);
        continue;
      }
      if (entry?.type !== "message") continue;
      const msg = entry.message;
      if (msg?.role === "assistant" && Array.isArray(msg.content)) {
        for (const block of msg.content) {
          if (block?.type === "toolCall" && ["read", "edit", "write"].includes(block.name)) {
            pending.set(block.id, { name: block.name, args: block.arguments ?? {} });
          }
        }
      } else if (msg?.role === "toolResult") {
        const call = pending.get(msg.toolCallId);
        if (!call || msg.isError) continue;
        const recorded = resolved.get(msg.toolCallId);
        resolved.delete(msg.toolCallId); // a provider may reuse ids across turns: one record, one call
        if (recorded === undefined && base === null) continue;
        const target = recorded ?? normalizeTarget(String(call.args.path ?? ""), base as string, home);
        const mt = mtimeOf(target);
        if (mt === null) continue;
        // A file changed on disk AFTER that read (entry timestamps are wall
        // clock; a tool's own write precedes its entry) reads as stale: the
        // sentinel never equals a real mtime, so the next edit is refused with
        // Claude's stale-read text until a fresh read.
        // An unparseable timestamp reads as 0: everything is stale until re-read
        // (fail-closed). A `read` gets no slack — nothing legitimately changes a
        // file between the read and its own entry; an edit/write does (the file
        // is written before the entry lands).
        const readAt = Date.parse(String(entry.timestamp ?? "")) || 0;
        const slack = call.name === "read" ? 0 : RESUME_MTIME_SLACK_MS;
        readMap.set(target, mt > readAt + slack ? -1 : mt);
      }
    }
  };

  pi.on("session_start", async (_event, ctx) => {
    guarded("session_start", () => {
      uiCtx = ctx;
      if ((ctx as any).hasUI) {
        const ui = (ctx as any).ui;
        (globalThis as any)[UI_FLAG] = (line: string) => ui.notify(line, "warning");
      }
      rediscover(ctx);
      rebuildFromBranch(ctx);
      resetShell(ctx);
      const launch = disc!.rules.filter((r) => !r.paths).length + (disc!.claudeMd ? 1 : 0) + (disc!.claudeLocal ? 1 : 0);
      const scoped = disc!.rules.filter((r) => !!r.paths).length;
      if ((ctx as any).hasUI && launch + scoped > 0) {
        (ctx as any).ui.notify(`rs-rules: ${launch} launch, ${scoped} path-scoped`, "info");
      }
    });
    // Separately guarded: a failure in one half never stops the other. Before
    // the await below, so the built-in MCP's own session_start finds them.
    guarded("mcp", () => syncAncestorMcp(ctx));
    await guardedAsync("subagent registration", () => registerForChildren(ctx));
  });

  // pi emits this on the old instance before session_start on the new one
  // (/new, resume, fork, reload): the registration is per session id.
  pi.on("session_shutdown", async (_event, _ctx) => {
    guarded("session_shutdown", () => unregister());
    // Per-call files a command wrote after its result came back (a yielded
    // command), and those of calls still in flight: removed by name.
    guarded("session_shutdown", () => {
      for (const f of [...cwdFiles]) removeCwdFile(f);
      inFlight.clear();
    });
  });

  pi.on("before_agent_start", async (event: any, ctx) => {
    return guarded("before_agent_start", () => {
      if (!disc) rediscover(ctx);
      const d = disc as Discovered;
      const already = new Set<string>();
      for (const cf of event.systemPromptOptions?.contextFiles ?? []) {
        const real = realpathOrNull(String(cf?.path ?? ""));
        if (real) already.add(real);
      }
      const parts: string[] = [];
      const push = (r: Rule) => {
        if (r.body.length === 0) return;
        parts.push(`## ${r.label}\n\n${r.body}`);
      };
      for (const r of d.rules) if (r.scope === "user" && !r.paths) push(r);
      if (d.claudeMd && !already.has(d.claudeMd.id)) push(d.claudeMd);
      if (d.claudeLocal && !already.has(d.claudeLocal.id)) push(d.claudeLocal);
      for (const r of d.rules) if (r.scope === "project" && !r.paths) push(r);
      const sections = event.systemPromptOptions.sections;
      if (parts.length === 0) delete sections[RS_RULES_SECTION];
      else sections[RS_RULES_SECTION] = "# Project rules (rs-rules)\n\n" + parts.join("\n\n");
      if (!startDir) resetShell(ctx);
      if (shellOn) {
        sections[SHELL_CWD_SECTION] =
          `The bash tool keeps its working directory between calls inside ${startDir}: a \`cd\` carries over to the next command; ` +
          `a command that ends outside it is followed by "${RESET_NOTICE(startDir)}" and the next command starts there again. ` +
          "Relative paths given to read, edit, write, grep, find and ls resolve against that same current directory.";
      } else delete sections[SHELL_CWD_SECTION];
      return undefined;
    });
  });

  // Forgotten only once removed: a file not there yet (a yielded command still
  // running, a call in flight) stays named for session_shutdown.
  const removeCwdFile = (file: string) => {
    try {
      fs.unlinkSync(file);
      cwdFiles.delete(file);
    } catch {
      /* absent: nothing recorded yet */
    }
  };

  // Start the shell directory over for a session: its start directory, and
  // whether the configured shell can take the prefix.
  const resetShell = (ctx: ExtensionContext) => {
    startDir = realpathOrNull(ctx.cwd) ?? path.resolve(ctx.cwd);
    cwdNow = startDir;
    shellOn = configuredShellPaths(ctx.cwd, home).every(shellIsPosix);
    seq = 0;
    appliedSeq = 0;
  };

  // A bash call: run it from the remembered directory and record where it ends.
  const prepareBash = (ev: ToolCallEvt & { parentToolCallId?: string }) => {
    if (!shellOn || typeof ev.input?.command !== "string") return;
    let notice: string | null = null;
    if (cwdNow !== startDir && !isDir(cwdNow)) {
      cwdNow = startDir; // the directory went away (a removed worktree)
      notice = RESET_NOTICE(startDir);
    }
    const id = crypto.createHash("sha256").update(`${ev.toolCallId}\0${crypto.randomBytes(8).toString("hex")}`).digest("hex").slice(0, 20);
    const file = path.join(os.tmpdir(), `${CWD_FILE_PREFIX}${sessionTag}-${id}`);
    cwdFiles.add(file);
    inFlight.set(ev.toolCallId, { seq: ++seq, file, topLevel: !ev.parentToolCallId, notice });
    ev.input.command = shellPrefix(file, cwdNow, startDir) + ev.input.command;
  };

  // A file tool: a relative path (or, for grep/find/ls, none) follows the shell;
  // a remembered directory that went away falls back to the start directory
  // here too, with the notice on this tool's result.
  const followShell = (ev: ToolCallEvt) => {
    if (!shellOn || cwdNow === startDir || !ev.input) return;
    if (!isDir(cwdNow)) {
      cwdNow = startDir;
      fileNotices.set(ev.toolCallId, RESET_NOTICE(startDir));
      return;
    }
    const raw = ev.input.path;
    if (PATH_TOOLS_OPTIONAL.includes(ev.toolName) && (raw === undefined || raw === null || raw === "")) {
      ev.input.path = cwdNow;
      return;
    }
    if (typeof raw !== "string") return;
    const abs = followPath(raw, cwdNow, home);
    if (abs !== null) ev.input.path = abs;
  };

  pi.on("tool_call", async (event: any, ctx) => {
    return guarded("tool_call", () => {
      const ev = event as ToolCallEvt;
      if (ev.toolName === SUBAGENT_TOOL) {
        const reason = subagentLaunchRefusal(ev.input);
        return reason === null ? undefined : { block: true, reason };
      }
      if (ev.toolName === "bash") {
        prepareBash(ev);
        return undefined;
      }
      if (PATH_TOOLS_REQUIRED.includes(ev.toolName) || PATH_TOOLS_OPTIONAL.includes(ev.toolName)) followShell(ev);
      if (ev.toolName !== "edit" && ev.toolName !== "write") return undefined;
      const target = normalizeTarget(String(ev.input?.path ?? ""), ctx.cwd, home);
      const mt = mtimeOf(target);
      if (mt === null) return undefined; // a new file: never blocked
      const seen = readMap.get(target);
      if (seen === undefined) return blocked(ev, NOT_READ_MSG);
      if (seen !== mt) return blocked(ev, STALE_READ_MSG);
      return undefined;
    });
  });

  // A refusal; a blocked call gets no tool_result, so a pending reset notice for
  // it rides the refusal's reason instead.
  const blocked = (ev: ToolCallEvt, reason: string) => {
    const notice = fileNotices.get(ev.toolCallId);
    if (notice === undefined) return { block: true, reason };
    fileNotices.delete(ev.toolCallId);
    return { block: true, reason: `${reason}\n${notice}` };
  };

  // After a bash call: where it ended decides where the next one starts (only a
  // call the model made itself moves it; the last one in the order the calls
  // were made wins over a sibling that finished later).
  const finishBash = (ev: ToolResultEvt) => {
    const call = inFlight.get(ev.toolCallId);
    if (!call) return undefined;
    inFlight.delete(ev.toolCallId);
    let recorded: string | null = null;
    try {
      recorded = fs.readFileSync(call.file, "utf8");
    } catch {
      recorded = null;
    }
    removeCwdFile(call.file);
    let notice = call.notice;
    if (call.topLevel) {
      const next = cwdAfter(recorded, startDir);
      if (next && call.seq > appliedSeq) {
        appliedSeq = call.seq;
        cwdNow = next.dir;
        if (next.notice) notice = next.notice;
      }
    }
    if (!notice) return undefined;
    const content = Array.isArray(ev.content) ? [...ev.content] : [];
    content.push({ type: "text", text: notice });
    return ev.structuredContent === undefined ? { content } : { content, structuredContent: ev.structuredContent };
  };

  // A file tool's result with the reset notice appended (structured result kept).
  const withNotice = (ev: ToolResultEvt, res: { content: unknown[] } | undefined) => {
    const notice = fileNotices.get(ev.toolCallId);
    if (notice === undefined) return res;
    fileNotices.delete(ev.toolCallId);
    const content = [...(res ? res.content : Array.isArray(ev.content) ? ev.content : [])];
    content.push({ type: "text", text: notice });
    return ev.structuredContent === undefined ? { content } : { content, structuredContent: ev.structuredContent };
  };

  pi.on("tool_result", async (event: any, ctx) => {
    return guarded("tool_result", () => {
      const ev = event as ToolResultEvt;
      if (ev.toolName === "bash") return finishBash(ev);
      return withNotice(ev, fileToolResult(ev, ctx));
    });
  });

  // The rules' half of a file tool's result: the read set, the recorded target,
  // the scoped rules that attach.
  const fileToolResult = (ev: ToolResultEvt, ctx: ExtensionContext): { content: unknown[] } | undefined => {
    {
      if (!["read", "edit", "write"].includes(ev.toolName) || ev.isError) return undefined;
      const target = normalizeTarget(String(ev.input?.path ?? ""), ctx.cwd, home);
      const mt = mtimeOf(target);
      if (mt !== null) {
        readMap.set(target, mt); // a read, or a write/edit we just made: the file is current
        // The absolute target, recorded beside the call so a rebuild never
        // re-resolves the relative path against another directory.
        try {
          pi.appendEntry(ENTRY_TYPE, { read: target, toolCallId: ev.toolCallId });
        } catch {
          /* a session that cannot persist still tracks the read for this run */
        }
      }
      if (!disc) rediscover(ctx);
      const blocks: string[] = [];
      for (const { rule, rel } of candidates(target)) {
        if (attached.has(rule.id) || !rule.compiled) continue;
        if (!matchTarget(rule.compiled, rel)) continue;
        attached.add(rule.id);
        if (rule.body.length === 0) continue; // a comment-only rule has nothing to say
        try {
          pi.appendEntry(ENTRY_TYPE, { attached: rule.id, target });
        } catch {
          /* a session that cannot persist still attaches for this run */
        }
        blocks.push(`[rs-rules] Rule ${rule.label} applies to this file:\n${rule.body}`);
      }
      if (blocks.length === 0) return undefined;
      const content = Array.isArray(ev.content) ? [...ev.content] : [];
      content.push({ type: "text", text: blocks.join("\n\n") });
      return { content };
    }
  };

  pi.on("session_compact", async (_event, _ctx) => {
    guarded("session_compact", () => {
      attached.clear();
      try {
        pi.appendEntry(ENTRY_TYPE, { reset: true });
      } catch {
        /* see above */
      }
    });
  });

  pi.registerCommand("rules", {
    description: "List the Claude-style rules rs-rules discovered (or `/rules reload`)",
    handler: async (args: string, ctx) => {
      guarded("/rules", () => {
        uiCtx = ctx;
        if (String(args ?? "").trim() === "reload") rediscover(ctx);
        if (!disc) rediscover(ctx);
        const d = disc as Discovered;
        const lines: string[] = [`rs-rules v${RS_RULES_VERSION} — root ${d.root}`];
        const state = (r: Rule) => (!r.paths ? "launch" : attached.has(r.id) ? "attached" : `pending on ${r.paths.join(", ")}`);
        if (d.claudeMd) lines.push(`  ${d.claudeMd.label}: launch`);
        if (d.claudeLocal) lines.push(`  ${d.claudeLocal.label}: launch`);
        for (const r of d.rules) lines.push(`  ${r.label}: ${state(r)}`);
        for (const [, rs] of nested) for (const r of rs) lines.push(`  ${r.label}: ${state(r)}`);
        lines.push(`  ${readMap.size} file(s) read this session`);
        const text = lines.join("\n");
        // Outside the TUI (print / json mode) the listing goes to stderr: print
        // mode reserves stdout for the model's answer.
        if ((ctx as any).hasUI) (ctx as any).ui.notify(text, "info");
        else process.stderr.write(text + "\n");
      });
    },
  });
}
