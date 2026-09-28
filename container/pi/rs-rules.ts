// rs-rules v0.1.0
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
// Not reproduced, by decision: `claudeMdExcludes`, `@path` imports,
// `--setting-sources`.
//
// Every handler is wrapped: a failure warns and returns nothing (a thrown
// tool_call handler would BLOCK the tool — the catch is load-bearing).

import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";

export const RS_RULES_VERSION = "0.1.0";

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

export function normalizeTarget(raw: string, cwd: string, home: string): string {
  let p = String(raw ?? "").trim();
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
  const abs = path.resolve(cwd, p);
  return realpathOrNull(abs) ?? path.normalize(abs);
}

function relTo(base: string, target: string): string | null {
  const rel = path.relative(base, target).replace(/\\/g, "/");
  if (rel === "" || rel.startsWith("../") || rel === ".." || path.isAbsolute(rel)) return null;
  return rel;
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
  isError?: boolean;
}

export default function rsRules(pi: ExtensionAPI) {
  const home = os.homedir();
  let disc: Discovered | null = null;
  const nested = new Map<string, Rule[]>(); // realpath dir -> its rules
  const attached = new Set<string>();
  const readMap = new Map<string, number>(); // realpath -> mtimeMs at the read
  let uiCtx: ExtensionContext | null = null;

  const warn = (msg: string) => {
    const line = `rs-rules: ${msg}`;
    try {
      if (uiCtx && (uiCtx as any).hasUI) (uiCtx as any).ui.notify(line, "warning");
      else process.stderr.write(line + "\n");
    } catch {
      /* never throw out of a warning */
    }
  };

  const guarded = <T>(name: string, fn: () => T): T | undefined => {
    try {
      return fn();
    } catch (e) {
      warn(`${name} failed: ${e instanceof Error ? e.message : String(e)}`);
      return undefined;
    }
  };

  const rediscover = (ctx: ExtensionContext) => {
    disc = discoverRules(ctx.cwd, home, warn);
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
        if (!nested.has(real)) nested.set(real, discoverNested(dir, disc.root, warn));
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
    for (const entry of entries) {
      if (entry?.type === "custom" && entry.customType === ENTRY_TYPE) {
        const data = entry.data ?? {};
        if (data.reset) attached.clear();
        if (typeof data.attached === "string") attached.add(data.attached);
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
        const target = normalizeTarget(String(call.args.path ?? ""), ctx.cwd, home);
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
      rediscover(ctx);
      rebuildFromBranch(ctx);
      const launch = disc!.rules.filter((r) => !r.paths).length + (disc!.claudeMd ? 1 : 0) + (disc!.claudeLocal ? 1 : 0);
      const scoped = disc!.rules.filter((r) => !!r.paths).length;
      if ((ctx as any).hasUI && launch + scoped > 0) {
        (ctx as any).ui.notify(`rs-rules: ${launch} launch, ${scoped} path-scoped`, "info");
      }
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
      if (parts.length === 0) return undefined;
      const section = "# Project rules (rs-rules)\n\n" + parts.join("\n\n");
      return { systemPrompt: `${event.systemPrompt}\n\n${section}` };
    });
  });

  pi.on("tool_call", async (event: any, ctx) => {
    return guarded("tool_call", () => {
      const ev = event as ToolCallEvt;
      if (ev.toolName !== "edit" && ev.toolName !== "write") return undefined;
      const target = normalizeTarget(String(ev.input?.path ?? ""), ctx.cwd, home);
      const mt = mtimeOf(target);
      if (mt === null) return undefined; // a new file: never blocked
      const seen = readMap.get(target);
      if (seen === undefined) return { block: true, reason: NOT_READ_MSG };
      if (seen !== mt) return { block: true, reason: STALE_READ_MSG };
      return undefined;
    });
  });

  pi.on("tool_result", async (event: any, ctx) => {
    return guarded("tool_result", () => {
      const ev = event as ToolResultEvt;
      if (!["read", "edit", "write"].includes(ev.toolName) || ev.isError) return undefined;
      const target = normalizeTarget(String(ev.input?.path ?? ""), ctx.cwd, home);
      const mt = mtimeOf(target);
      if (mt !== null) readMap.set(target, mt); // a read, or a write/edit we just made: the file is current
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
    });
  });

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
