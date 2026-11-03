// settings-heal — RS's one-time edit of pi's own settings, run by the RS pi
// launcher (bin/pi) before pi starts, only while its marker is absent.
//
// What it does: makes pi's agent-dir settings.json name the pi-subagents copy
// shipped in RS's pi download, exactly once per agent dir, for projects whose
// settings were written before RS shipped it (settings are installed no-clobber,
// so a canonical-settings change never reaches them otherwise). A hand-installed
// `npm:pi-subagents` entry is replaced by the shipped one (two identities would
// load the package twice). When the shipped entry is in the file it writes the
// marker; the launcher then never runs this again, so an operator who removes
// the entry later keeps it removed.
//
// Usage (from the launcher): node settings-heal.mjs <marker, relative to the
// agent dir> <absolute path of the shipped wrapper package>
//
// Rules it keeps:
//   - never creates settings.json (every deploy site installs the canonical one
//     only where none exists; a file made here would block that for good);
//   - never touches a file owned by another uid (a root exec must not take it);
//   - takes pi's own settings lock by its protocol — the directory
//     `<settings.json>.lock`, made with mkdir, next to the path as given (pi locks
//     with `realpath: false`) — holds it only around read, parse and write, and
//     never removes a lock it did not make, stale or not (pi clears stale ones);
//   - writes in place, as pi does (owner, mode and a symlinked settings.json are
//     kept) and in pi's format (two-space JSON, no trailing newline);
//   - never writes to stdout (pi's RPC and print modes use it), never throws a
//     stack trace: every failure is one line on stderr, no edit, no marker, and
//     the next pi start tries again.

import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

// pi's settings manager tries its lock 10 times, 20 ms apart, before giving up
// (dist/core/settings-manager.js, acquireLockSyncWithRetry). The heal waits on
// the same budget: pi holds the lock only for a synchronous read and write, so
// a longer wait buys nothing, and a busy lock costs only a retry at the next
// pi start.
const LOCK_ATTEMPTS = 10;
const LOCK_RETRY_MS = 20;
const NPM_SOURCE = "npm:pi-subagents";

const say = (msg) => process.stderr.write(`rs: ${msg}\n`);

// pi's tilde handling for PI_CODING_AGENT_DIR and local package sources
// (utils/paths.js normalizePath): only a bare `~` or a leading `~/`.
function expandTilde(p) {
  if (p === "~") return os.homedir();
  if (p.startsWith("~/")) return path.join(os.homedir(), p.slice(2));
  return p;
}

function agentDir() {
  const env = process.env.PI_CODING_AGENT_DIR;
  return path.resolve(env ? expandTilde(env) : path.join(os.homedir(), ".pi", "agent"));
}

function sleepMs(ms) {
  Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, ms);
}

function sourceOf(entry) {
  if (typeof entry === "string") return entry;
  if (entry && typeof entry === "object" && typeof entry.source === "string") return entry.source;
  return undefined;
}

function isNpmCopy(src) {
  return src === NPM_SOURCE || (typeof src === "string" && src.startsWith(`${NPM_SOURCE}@`));
}

// A local source names the shipped wrapper when it resolves to it the way pi
// resolves a local package source (utils/paths.js resolvePath with trim: a
// trimmed string, a leading `~`, a file:// URL, relative to the agent dir).
function isShipped(src, dir, wrapper) {
  if (typeof src !== "string") return false;
  let s = src.trim();
  if (/^(npm:|git:|https?:)/.test(s)) return false;
  if (/^file:\/\//.test(s)) {
    try {
      s = fileURLToPath(s);
    } catch {
      return false;
    }
  }
  return path.resolve(dir, expandTilde(s)) === wrapper;
}

// The packages list with exactly one shipped entry and no npm copy; null when
// the list cannot be edited safely (not a list).
function healed(packages, dir, wrapper, entry) {
  if (packages === undefined) return { list: [entry], replaced: false };
  if (!Array.isArray(packages)) return null;
  const out = [];
  let placed = false;
  let replaced = false;
  for (const item of packages) {
    const src = sourceOf(item);
    const shipped = isShipped(src, dir, wrapper);
    const npm = isNpmCopy(src);
    if (npm) replaced = true;
    if (shipped || npm) {
      // The first shipped-or-npm entry's slot keeps the shipped copy (an existing
      // shipped entry as written, else the computed one); every later one is
      // dropped, so the package has one identity and loads once.
      if (!placed) {
        out.push(shipped ? item : entry);
        placed = true;
      }
      continue;
    }
    out.push(item);
  }
  if (!placed) out.push(entry);
  return { list: out, replaced };
}

function main() {
  const [markerRel, wrapperArg] = process.argv.slice(2);
  if (!markerRel || !wrapperArg) {
    say("pi settings heal: missing arguments, nothing done");
    return 2;
  }
  const dir = agentDir();
  const wrapper = path.resolve(wrapperArg);
  const settings = path.join(dir, "settings.json");
  const marker = path.join(dir, markerRel);
  const entry = path.relative(dir, wrapper);

  let st;
  try {
    st = fs.statSync(settings); // follows a symlink: the owner that matters is the target's
  } catch (e) {
    if (e && e.code === "ENOENT") return 0; // nothing to heal yet; the deploy installs the canonical file
    say(`pi settings heal: cannot read ${settings} (${e && e.code ? e.code : e}), nothing done`);
    return 1;
  }
  if (typeof process.getuid === "function" && st.uid !== process.getuid()) return 0;

  const lock = `${settings}.lock`;
  let held = false;
  for (let attempt = 1; attempt <= LOCK_ATTEMPTS && !held; attempt++) {
    try {
      fs.mkdirSync(lock);
      held = true;
    } catch (e) {
      if (!e || e.code !== "EEXIST") {
        say(`pi settings heal: cannot lock ${settings} (${e && e.code ? e.code : e}), nothing done`);
        return 1;
      }
      if (attempt < LOCK_ATTEMPTS) sleepMs(LOCK_RETRY_MS);
    }
  }
  if (!held) return 0; // busy: retried at the next pi start, silently

  let outcome;
  try {
    let data;
    try {
      data = JSON.parse(fs.readFileSync(settings, "utf-8").replace(/^\uFEFF/, ""));
    } catch {
      data = undefined;
    }
    if (!data || typeof data !== "object" || Array.isArray(data)) {
      say(`pi settings heal: ${settings} is not a JSON object; left as it is (pi subagents not added)`);
      return 1;
    }
    const result = healed(data.packages, dir, wrapper, entry);
    if (result === null) {
      say(`pi settings heal: "packages" in ${settings} is not a list; left as it is (pi subagents not added)`);
      return 1;
    }
    const before = JSON.stringify(data.packages);
    const after = JSON.stringify(result.list);
    if (before !== after) {
      data.packages = result.list;
      fs.writeFileSync(settings, JSON.stringify(data, null, 2), "utf-8");
      outcome = result.replaced
        ? "replaced the hand-installed npm:pi-subagents in pi's settings with the copy RS ships"
        : "added the pi-subagents copy RS ships to pi's settings";
    }
  } catch (e) {
    say(`pi settings heal: could not update ${settings} (${e && e.code ? e.code : e}); pi subagents not added`);
    return 1;
  } finally {
    try {
      fs.rmdirSync(lock);
    } catch {
      // already gone: nothing of ours left to release
    }
  }

  // Marker only once the shipped entry is really in the file.
  try {
    const check = JSON.parse(fs.readFileSync(settings, "utf-8").replace(/^\uFEFF/, ""));
    const ok = Array.isArray(check.packages)
      && check.packages.some((item) => isShipped(sourceOf(item), dir, wrapper));
    if (!ok) return 1;
    fs.mkdirSync(path.dirname(marker), { recursive: true });
    fs.writeFileSync(marker, `${new Date().toISOString()}\n`, "utf-8");
  } catch (e) {
    say(`pi settings heal: could not record that it ran (${e && e.code ? e.code : e}); it will run again`);
    return 1;
  }
  if (outcome) say(outcome);
  return 0;
}

// Registering a handler replaces the default "terminate at once" action. JS is
// never interrupted mid-statement, so a SIGINT, SIGTERM or SIGHUP that arrives
// during the wait or the hold cannot stop the work before the `finally` that
// releases the lock; the process then exits with its own status (node's signal
// handles do not keep the loop alive, so the queued handler is not dispatched
// once the work is done). Only SIGKILL can leave the lock behind.
for (const sig of ["SIGINT", "SIGTERM", "SIGHUP"]) {
  process.on(sig, () => process.exit(1));
}

let code;
try {
  code = main();
} catch (e) {
  say(`pi settings heal: unexpected error (${e && e.message ? e.message : e}); nothing done`);
  code = 1;
}
process.exitCode = code;
