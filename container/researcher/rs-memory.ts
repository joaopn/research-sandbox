// rs-memory for pi
//
// RS's research memory kit, the pi side: RS writes this file into the agent
// folder's `extensions/` directory in a researcher project, where pi loads it by
// itself.
//
// - The front page reaches the agent by itself: when a session starts
//   (startup, /new, resume, fork, reload) `rs-memory open --hook --agent pi`
//   runs at once, its line for the user (with the planner on: the package
//   the start took, the free ones, or that there is nothing to work on) is
//   shown with ctx.ui.notify before any input, and its text for the model
//   is injected as a message at the session's first run; after every
//   compaction it runs again and its text is injected, once (Claude Code
//   gets the same through its SessionStart hook, which fires on every
//   start, resume, clear and compaction, and shows the line too). The
//   command prints one JSON object, {"line": <line or null>, "text":
//   <text>}; output that is not one is given to the model as it is. A compaction
//   between turns of a run (agent_start .. agent_end: pi compacts there when
//   the context fills), or one pi says it will retry after (willRetry, an
//   overflow), is answered at once, steered in before the next model call;
//   any other (before a run starts, after its last answer, a /compact) is
//   answered at the next run the PI starts, so it never starts a turn of its
//   own. pi reports itself busy during every compaction, so ctx.isIdle()
//   cannot tell these apart. `open` is passed `--agent pi` (pi does not set
//   PI_SESSION_ID in this extension's process, so rs-memory cannot tell the
//   agent itself): when the last session ended without its consolidation,
//   open runs it first, as pi, and waits for it.
// - Every call is given the session's id as PI_SESSION_ID, read from the
//   handler's context (ctx.sessionManager.getSessionId(), the value pi's bash
//   tool puts in PI_SESSION_ID for the session's own commands), so `open`
//   names the switches this session works under (a researcher
//   session switched with /skill:rs-mode keeps them after a compaction) and
//   the end entry names the session. No id: the variable is left out.
// - At the end of a session it journals the end (`rs-memory close --hook`),
//   which starts nothing: the next session start, or an explicit
//   `rs-memory close`, consolidates. The call is made synchronously and
//   detached (`setsid -f`): pi may already be dying when its window closes,
//   and a spawn from an asynchronous handler never happens then (measured for
//   RS's activity recorder); `setsid -f` returns at once.
// - A pi-subagents child, and a consolidation run itself, do nothing.
// - Nothing here ever throws into pi.
//
// The functions above `export default` are pure and tested on their own.

import { spawnSync } from "node:child_process";

export const TOOL = "/usr/local/lib/rs/rs-memory";
export const MESSAGE_TYPE = "rs-memory";

export function inactive(env: Record<string, string | undefined>): boolean {
	return Boolean(env.PI_SUBAGENT_CHILD) || env.RS_MEMORY_CONSOLIDATING === "1";
}

export function closeArgs(): string[] {
	return ["-f", TOOL, "close", "--hook", "--agent", "pi"];
}

export function openArgs(): string[] {
	return ["open", "--hook", "--agent", "pi"];
}

// The session's id from a handler's context; "" when pi gives none.
export function sessionIdOf(ctx: any): string {
	try {
		const id = ctx?.sessionManager?.getSessionId?.();
		return typeof id === "string" ? id : "";
	} catch {
		return "";
	}
}

// The tool's environment: this process's, with PI_SESSION_ID set to the
// session's id (an inherited one, which would name another session, is
// dropped), or left out when there is none.
export function toolEnv(env: Record<string, string | undefined>, sessionId: string): Record<string, string | undefined> {
	const out: Record<string, string | undefined> = { ...env };
	delete out.PI_SESSION_ID;
	if (sessionId) out.PI_SESSION_ID = sessionId;
	return out;
}

// The open command's output read: its line for the user ("" when none) and
// its text for the model. Output that is not the command's JSON object is
// all text, as the command printed it.
export function openParts(output: string): { line: string; text: string } {
	try {
		const o = JSON.parse(output);
		if (o && typeof o === "object" && !Array.isArray(o) && typeof o.text === "string") {
			return { line: typeof o.line === "string" ? o.line : "", text: o.text };
		}
	} catch { /* not the JSON object: all text */ }
	return { line: "", text: output };
}

// The message a run starts with, or undefined when the tool had nothing to say.
export function frontMessage(output: string): { customType: string; content: string; display: boolean } | undefined {
	const text = output.trim();
	if (!text) return undefined;
	return { customType: MESSAGE_TYPE, content: text, display: false };
}

function closeNow(sessionId: string): void {
	try {
		spawnSync("setsid", closeArgs(), { stdio: "ignore", env: toolEnv(process.env, sessionId) });
	} catch { /* never into pi */ }
}

function openNow(sessionId: string): string {
	try {
		const r = spawnSync(TOOL, openArgs(), { encoding: "utf8", stdio: ["ignore", "pipe", "ignore"],
			env: toolEnv(process.env, sessionId) });
		return r.status === 0 && typeof r.stdout === "string" ? r.stdout : "";
	} catch {
		return "";
	}
}

export default function (pi: any) {
	if (inactive(process.env)) return;
	let pending = false;
	let inRun = false;
	// The session's id as last seen, for a handler whose context gives none.
	let lastId = "";
	const idOf = (ctx: any): string => {
		const id = sessionIdOf(ctx);
		if (id) lastId = id;
		return id || lastId;
	};
	pi.on("agent_start", () => { inRun = true; });
	pi.on("agent_end", () => { inRun = false; });
	// The start's text, read at session_start and given at the first run;
	// undefined: the next run reads it afresh (after a compaction).
	let startText: string | undefined = undefined;
	pi.on("session_start", (_e: any, ctx: any) => {
		const id = idOf(ctx);
		pending = true;
		try {
			const parts = openParts(openNow(id));
			startText = parts.text;
			if (parts.line) ctx?.ui?.notify?.(parts.line, "info");
		} catch { /* never into pi */ }
	});
	pi.on("session_compact", (e: any, ctx: any) => {
		const id = idOf(ctx);
		startText = undefined;
		if (!inRun && e?.willRetry !== true) { pending = true; return; }
		pending = false;
		try {
			const message = frontMessage(openParts(openNow(id)).text);
			if (message) pi.sendMessage(message, { deliverAs: "steer" });
		} catch { /* never into pi */ }
	});
	pi.on("before_agent_start", (_e: any, ctx: any) => {
		const id = idOf(ctx);
		if (!pending) return undefined;
		pending = false;
		try {
			const text = startText !== undefined ? startText : openParts(openNow(id)).text;
			startText = undefined;
			const message = frontMessage(text);
			return message ? { message } : undefined;
		} catch {
			return undefined;
		}
	});
	pi.on("session_shutdown", (_e: any, ctx: any) => closeNow(idOf(ctx)));
}
