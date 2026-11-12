// rs-statusbar for pi
//
// RS's status bar, the pi twin of rs-statusbar.py (Claude Code's status line).
// RS writes this file into the agent folder's `extensions/` directory, where pi
// loads every extension by itself; removing the file turns the bar off. It
// replaces pi's footer with the same three lines pi draws (folder and branch,
// the stats line, other extensions' statuses), the stats line being RS's bar:
//
//   claude-opus-5.5 · high │ ● cache: 4m (94% cached) · $3.07 (R: 67%, W: 21%, O: 12%) │ ctx 161k/1M (7 compactions)
//
// - model, then its thinking level when the model reasons.
// - cache: time left on the prompt cache, counted from the last request or
//   cache refresh and the model's declared cache lifetime for the active
//   retention tier (amber in the last fifth, "cold" once expired), then the
//   share of all input tokens read from the cache. Shown only for a model that
//   declares a cache lifetime.
// - $: the session's cost, main conversation, cache refreshes and subagents,
//   with the share spent on cache reads (R), cache writes and uncached input
//   (W) and output (O). The main conversation's split is pi's own per-reply
//   cost; subagents report only a total, so their share of the split is an
//   estimate from their token counts at the current model's prices.
// - ctx: current context, grey / yellow from 50% / red from 80% of the window,
//   then "(N compactions)" once the conversation has been compacted.
//
// The functions above `export default` are pure and tested on their own.

import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";
import { truncateToWidth, visibleWidth } from "@earendil-works/pi-tui";

export interface Rates { input: number; output: number; cacheRead: number; cacheWrite: number }

export interface Stats {
	read: number; write: number; input: number; output: number;   // tokens
	r: number; w: number; o: number;                                // dollars split into shares
	total: number;                                                  // dollars, as pi reports them
	estimated: boolean;                                             // part of the split is an estimate
	compactions: number;
	lastCacheAt: number;                                            // ms; 0 when nothing was sent yet
}

export interface Paint {
	dim(s: string): string; bold(s: string): string; accent(s: string): string;
	ok(s: string): string; warn(s: string): string; err(s: string): string;
}

const num = (v: unknown): number => (typeof v === "number" && Number.isFinite(v) ? v : 0);

export function emptyStats(): Stats {
	return { read: 0, write: 0, input: 0, output: 0, r: 0, w: 0, o: 0, total: 0,
		estimated: false, compactions: 0, lastCacheAt: 0 };
}

/** Add one usage record to the running stats. A record that carries its cost
 * split (pi's own replies and cache refreshes) adds that split; one that
 * carries only a total (a subagent's summed usage) is split by its token
 * counts weighted with `rates`. */
export function addUsage(s: Stats, usage: any, rates: Rates | undefined): void {
	if (!usage || typeof usage !== "object") return;
	const read = num(usage.cacheRead), write = num(usage.cacheWrite);
	const input = num(usage.input), output = num(usage.output);
	s.read += read; s.write += write; s.input += input; s.output += output;
	const c = usage.cost && typeof usage.cost === "object" ? usage.cost : {};
	const total = num(c.total);
	s.total += total;
	const cr = num(c.cacheRead), cw = num(c.cacheWrite) + num(c.input), co = num(c.output);
	if (cr + cw + co > 0) {
		s.r += cr; s.w += cw; s.o += co;
		return;
	}
	if (total <= 0 || !rates) return;
	const wr = read * num(rates.cacheRead);
	const ww = write * num(rates.cacheWrite) + input * num(rates.input);
	const wo = output * num(rates.output);
	const sum = wr + ww + wo;
	if (sum <= 0) return;
	s.r += total * wr / sum; s.w += total * ww / sum; s.o += total * wo / sum;
	s.estimated = true;
}

const stamp = (v: unknown): number => {
	if (typeof v === "number" && Number.isFinite(v)) return v;
	if (typeof v === "string") { const t = Date.parse(v); return Number.isFinite(t) ? t : 0; }
	return 0;
};

/** The session's stats from its entries (every entry, as pi's own footer
 * totals them); compactions are counted on `branch`, the current path. */
export function collect(entries: readonly any[], branch: readonly any[], rates: Rates | undefined): Stats {
	const s = emptyStats();
	for (const e of entries) {
		if (!e || typeof e !== "object") continue;
		const m = e.message;
		if (e.type === "message" && m && m.role === "assistant" && m.usage) {
			addUsage(s, m.usage, rates);
			if (num(m.usage.input) + num(m.usage.cacheRead) + num(m.usage.cacheWrite) > 0)
				s.lastCacheAt = Math.max(s.lastCacheAt, stamp(m.timestamp) || stamp(e.timestamp));
		} else if (e.type === "message" && m && m.role === "toolResult" && m.usage) {
			addUsage(s, m.usage, rates);
		} else if (e.type === "usage" && e.usage) {
			addUsage(s, e.usage, rates);
			if (e.kind === "cache_warm") s.lastCacheAt = Math.max(s.lastCacheAt, stamp(e.timestamp));
		} else if ((e.type === "compaction" || e.type === "branch_summary") && e.usage) {
			addUsage(s, e.usage, rates);
		}
	}
	for (const e of branch) if (e && e.type === "compaction") s.compactions += 1;
	return s;
}

export function human(n: number): string {
	if (n >= 1e6) return `${(n / 1e6).toFixed(1).replace(/\.0$/, "")}M`;
	if (n >= 1e3) return `${Math.floor(n / 1e3)}k`;
	return `${Math.floor(n)}`;
}

export function money(x: number): string {
	return x >= 100 ? `$${Math.round(x).toLocaleString("en-US")}` : `$${x.toFixed(2)}`;
}

export function timeLeft(sec: number): string {
	if (sec >= 3600) return `${Math.floor(sec / 3600)}h${Math.floor((sec % 3600) / 60)}m`;
	if (sec >= 60) return `${Math.floor(sec / 60)}m`;
	return `${Math.floor(sec)}s`;
}

export interface BarInput {
	model?: { id?: string; name?: string; reasoning?: boolean } | undefined;
	thinking?: string | undefined;
	lifetime?: number | undefined;     // seconds the cache lives for the active tier, if declared
	now: number;                        // ms
	stats: Stats;
	ctx?: { tokens: number | null; contextWindow: number; percent: number | null } | undefined;
}

/** The bar's one line, segments joined like the Claude script's. */
export function bar(b: BarInput, p: Paint): string {
	const out: string[] = [];
	const s = b.stats;
	let seg = p.bold(p.accent(b.model?.id || b.model?.name || "no model"));
	if (b.model?.reasoning) seg += p.dim(` · ${b.thinking || "off"}`);
	out.push(seg);
	const mid: string[] = [];
	if (b.lifetime && b.lifetime > 0) {
		let c: string;
		const left = s.lastCacheAt ? Math.floor((s.lastCacheAt + b.lifetime * 1000 - b.now) / 1000) : 0;
		if (left > 0) {
			const paint = left < b.lifetime / 5 ? p.warn : p.ok;
			c = paint(`● cache: ${timeLeft(left)}`);
		} else {
			c = p.err("○ cache: cold");
		}
		const inTotal = s.read + s.write + s.input;
		if (inTotal > 0) c += " " + p.dim(`(${Math.round(s.read * 100 / inTotal)}% cached)`);
		mid.push(c);
	}
	if (s.total > 0 || s.read + s.write + s.input + s.output > 0) {
		let c = p.dim(money(s.total));
		const tot = s.r + s.w + s.o;
		if (tot > 0) {
			const pct = (v: number) => Math.round(v * 100 / tot);
			c += " " + p.dim(`(R: ${pct(s.r)}%, W: ${pct(s.w)}%, O: ${pct(s.o)}%)`);
		}
		mid.push(c);
	}
	if (mid.length) out.push(mid.join(` ${p.dim("·")} `));
	if (b.ctx && b.ctx.contextWindow > 0) {
		const pct = b.ctx.percent;
		const paint = pct === null ? p.dim : pct >= 80 ? p.err : pct >= 50 ? p.warn : p.dim;
		const used = b.ctx.tokens === null ? "?" : human(b.ctx.tokens);
		seg = paint(`ctx ${used}/${human(b.ctx.contextWindow)}`);
		if (s.compactions) seg += " " + p.dim(`(${s.compactions} compaction${s.compactions === 1 ? "" : "s"})`);
		out.push(seg);
	}
	return out.join(` ${p.dim("│")} `);
}

/** The active retention tier's cache lifetime the model declares, in seconds. */
export function cacheLifetime(model: any, env: Record<string, string | undefined>): number | undefined {
	const tier = env.PI_CACHE_RETENTION === "long" ? "long" : "short";
	const v = model && model.promptCache ? model.promptCache[tier] : undefined;
	return typeof v === "number" && v > 0 ? v : undefined;
}

function cwdLabel(cwd: string, home: string | undefined): string {
	if (home && (cwd === home || cwd.startsWith(home.endsWith("/") ? home : home + "/")))
		return "~" + cwd.slice(home.replace(/\/$/, "").length);
	return cwd;
}

const sanitize = (t: string) => t.replace(/[\r\n\t]/g, " ").replace(/ +/g, " ").trim();

// How often the bar redraws on its own, so the cache countdown moves while the
// agent is idle: the countdown shows whole minutes above a minute, so a redraw
// every 15 s keeps it at most a quarter of its last digit behind.
const REDRAW_MS = 15_000;

export default function (pi: ExtensionAPI) {
	pi.on("session_start", (_event, ctx: ExtensionContext) => {
		if (!ctx.hasUI) return;
		ctx.ui.setFooter((tui, theme, footerData) => {
			const paint: Paint = {
				dim: (s) => theme.fg("dim", s), bold: (s) => theme.bold(s),
				accent: (s) => theme.fg("accent", s), ok: (s) => theme.fg("success", s),
				warn: (s) => theme.fg("warning", s), err: (s) => theme.fg("error", s),
			};
			let cacheKey = "";
			let cached: Stats = emptyStats();
			const stats = (): Stats => {
				const sm = ctx.sessionManager;
				const key = `${sm.getSessionId()}:${sm.getLeafId()}:${ctx.model?.provider}/${ctx.model?.id}`;
				if (key !== cacheKey) {
					cached = collect(sm.getEntries(), sm.getBranch(), ctx.model?.cost as Rates | undefined);
					cacheKey = key;
				}
				return cached;
			};
			const unsub = footerData.onBranchChange(() => tui.requestRender());
			const timer = setInterval(() => tui.requestRender(), REDRAW_MS);
			(timer as any).unref?.();
			return {
				dispose() { clearInterval(timer); unsub(); },
				invalidate() {},
				render(width: number): string[] {
					let where = cwdLabel(ctx.sessionManager.getCwd(), process.env.HOME);
					const branch = footerData.getGitBranch();
					if (branch) where += ` (${branch})`;
					const name = ctx.sessionManager.getSessionName();
					if (name) where += ` • ${name}`;
					const line = bar({
						model: ctx.model, thinking: ctx.thinkingLevel,
						lifetime: cacheLifetime(ctx.model, process.env), now: Date.now(),
						stats: stats(), ctx: ctx.getContextUsage(),
					}, paint);
					const lines = [truncateToWidth(theme.fg("dim", where), width, theme.fg("dim", "...")),
						visibleWidth(line) > width ? truncateToWidth(line, width, theme.fg("dim", "...")) : line];
					const st = footerData.getExtensionStatuses();
					if (st.size > 0) {
						const text = [...st.entries()].sort(([a], [b]) => a.localeCompare(b))
							.map(([, t]) => sanitize(t)).join(" ");
						lines.push(truncateToWidth(text, width, theme.fg("dim", "...")));
					}
					return lines;
				},
			};
		});
	});
}
