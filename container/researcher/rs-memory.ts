// rs-memory for pi
//
// RS's research memory kit, the pi side: RS writes this file into the agent
// folder's `extensions/` directory in a researcher project, where pi loads it by
// itself.
//
// - The front page reaches the agent by itself: on the first run after a
//   session starts (startup, /new, resume, fork, reload) and after every
//   compaction, `rs-memory open --hook` is run and its output injected as a
//   message, once (Claude Code gets the same through its SessionStart hook,
//   which fires on every start, resume, clear and compaction). A compaction
//   between turns of a run (agent_start .. agent_end: pi compacts there when
//   the context fills), or one pi says it will retry after (willRetry, an
//   overflow), is answered at once, steered in before the next model call;
//   any other (before a run starts, after its last answer, a /compact) is
//   answered at the next run the PI starts, so it never starts a turn of its
//   own. pi reports itself busy during every compaction, so ctx.isIdle()
//   cannot tell these apart.
// - At the end of a session it hands the journal to a consolidation run
//   (`rs-memory close --hook`), which starts one only when there is something
//   new. The call is made synchronously and detached (`setsid -f`): pi may
//   already be dying when its window closes, and a spawn from an asynchronous
//   handler never happens then (measured for RS's activity recorder); `setsid
//   -f` returns at once and the run lives on in its own session.
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
	return ["open", "--hook"];
}

// The message a run starts with, or undefined when the tool had nothing to say.
export function frontMessage(output: string): { customType: string; content: string; display: boolean } | undefined {
	const text = output.trim();
	if (!text) return undefined;
	return { customType: MESSAGE_TYPE, content: text, display: false };
}

function closeNow(): void {
	try {
		spawnSync("setsid", closeArgs(), { stdio: "ignore" });
	} catch { /* never into pi */ }
}

function openNow(): string {
	try {
		const r = spawnSync(TOOL, openArgs(), { encoding: "utf8", stdio: ["ignore", "pipe", "ignore"] });
		return r.status === 0 && typeof r.stdout === "string" ? r.stdout : "";
	} catch {
		return "";
	}
}

export default function (pi: any) {
	if (inactive(process.env)) return;
	let pending = false;
	let inRun = false;
	pi.on("agent_start", () => { inRun = true; });
	pi.on("agent_end", () => { inRun = false; });
	pi.on("session_start", () => { pending = true; });
	pi.on("session_compact", (e: any) => {
		if (!inRun && e?.willRetry !== true) { pending = true; return; }
		pending = false;
		try {
			const message = frontMessage(openNow());
			if (message) pi.sendMessage(message, { deliverAs: "steer" });
		} catch { /* never into pi */ }
	});
	pi.on("before_agent_start", () => {
		if (!pending) return undefined;
		pending = false;
		try {
			const message = frontMessage(openNow());
			return message ? { message } : undefined;
		} catch {
			return undefined;
		}
	});
	pi.on("session_shutdown", () => closeNow());
}
