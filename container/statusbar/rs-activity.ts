// rs-activity for pi
//
// RS's activity recorder, the pi side: RS writes this file into the agent
// folder's `extensions/` directory, where pi loads every extension by itself.
// It reports this session's turns and the subagents it launches to
// /usr/local/lib/rs/rs-activity.py, which keeps the container's counts for the
// webui's sidebar (sessions working, sessions waiting, subagents running).
//
// - Only an interactive session counts: a pi without a UI (`pi -p`, a
//   background subagent's runner) and any pi-subagents child do nothing.
// - Every count is kept by the session that launched the work: a foreground
//   `subagent` tool call counts while it runs (one per task when it lists
//   `tasks`), a background run from its `subagent:async-started` to its
//   `subagent:async-complete` (none for a workflow, whose agents announce
//   themselves; the recorder still keeps the session working while it runs).
//   Management calls (an `action`) run nothing and count nothing.
// - Calls reach the recorder in order, one at a time, and never throw into pi.
//
// The functions above `export default` are pure and tested on their own.

import { spawn, spawnSync } from "node:child_process";

export const RECORDER = "/usr/local/lib/rs/rs-activity.py";

export type Call = [event: string, payload: Record<string, unknown>];

export function isChild(env: Record<string, string | undefined>): boolean {
	return Boolean(env.PI_SUBAGENT_CHILD);
}

export function toolStart(e: any): Call | null {
	if (!e || e.toolName !== "subagent" || typeof e.toolCallId !== "string") return null;
	const args = e.args ?? {};
	const tasks = Array.isArray(args.tasks) ? args.tasks.length
		: Array.isArray(args.args?.tasks) ? args.args.tasks.length : 1;
	return ["tool_start", { id: e.toolCallId, action: args.action !== undefined, tasks }];
}

export function toolEnd(e: any): Call | null {
	if (!e || e.toolName !== "subagent" || typeof e.toolCallId !== "string") return null;
	return ["tool_end", { id: e.toolCallId }];
}

export function asyncStarted(e: any): Call | null {
	if (!e || typeof e.id !== "string") return null;
	const groups = Array.isArray(e.parallelGroups)
		? e.parallelGroups.map((g: any) => ({ count: typeof g?.count === "number" ? g.count : 1 }))
		: undefined;
	return ["async_started", {
		id: e.id, mode: typeof e.mode === "string" ? e.mode : undefined,
		toolCallId: typeof e.toolCallId === "string" ? e.toolCallId : undefined,
		parallelGroups: groups,
	}];
}

export function asyncComplete(e: any): Call | null {
	const id = typeof e?.id === "string" ? e.id : typeof e?.runId === "string" ? e.runId : null;
	return id ? ["async_complete", { id }] : null;
}

export function agentEnd(ctx: any): Call {
	return ["agent_end", { aborted: ctx?.signal?.aborted === true }];
}

// One promise chain: each call waits for the one before it. Once closed (the
// session's last call was made), calls still queued and new ones are dropped.
export function makeQueue(run: (c: Call) => Promise<void>) {
	let tail: Promise<void> = Promise.resolve();
	let closed = false;
	const send = (c: Call | null) => {
		if (!c || closed) return tail;
		tail = tail.then(() => (closed ? undefined : run(c))).catch(() => {});
		return tail;
	};
	return { send, close: () => { closed = true; } };
}

function runRecorder(c: Call): Promise<void> {
	return new Promise((resolve) => {
		try {
			// its own session: when pi leads the terminal's session and exits
			// (a closed window), the kernel's hang-up to the terminal's
			// process group must not kill the recorder before it writes
			const p = spawn(RECORDER, ["pi", c[0], String(process.pid)],
				{ stdio: ["pipe", "ignore", "ignore"], detached: true });
			p.on("error", () => resolve());
			p.on("close", () => resolve());
			p.stdin.on("error", () => {});
			p.stdin.end(JSON.stringify(c[1]));
		} catch {
			resolve();
		}
	});
}

// The session's last call, made synchronously: a closed window ends pi while
// its shutdown handlers run, before any queued or asynchronous call is made
// (measured: an async handler's spawn never happened). The queue is closed
// first, so no call still queued behind it lands after the drop.
function runRecorderNow(c: Call): void {
	try {
		spawnSync(RECORDER, ["pi", c[0], String(process.pid)],
			{ input: JSON.stringify(c[1]), stdio: ["pipe", "ignore", "ignore"] });
	} catch { /* never into pi */ }
}

export default function (pi: any) {
	if (isChild(process.env)) return;
	let on = false;
	const q = makeQueue(runRecorder);
	// fire and forget: pi never waits for the recorder
	const guard = (f: () => Call | null): void => {
		try { if (on) q.send(f()); } catch { /* never into pi */ }
	};
	pi.on("session_start", (e: any, ctx: any) => {
		try {
			on = ctx?.hasUI === true;
			if (on) q.send(["session_start", { reason: e?.reason }]);
		} catch { /* never into pi */ }
	});
	pi.on("agent_start", () => guard(() => ["agent_start", {}]));
	pi.on("agent_end", (_e: any, ctx: any) => guard(() => agentEnd(ctx)));
	pi.on("tool_execution_start", (e: any) => guard(() => toolStart(e)));
	pi.on("tool_execution_end", (e: any) => guard(() => toolEnd(e)));
	pi.on("session_shutdown", () => {
		if (!on) return;
		on = false;
		q.close();                 // nothing queued may land after the drop
		runRecorderNow(["session_shutdown", {}]);
	});
	try {
		pi.events?.on?.("subagent:async-started", (e: any) => guard(() => asyncStarted(e)));
		pi.events?.on?.("subagent:async-complete", (e: any) => guard(() => asyncComplete(e)));
	} catch { /* an older pi-subagents: background runs are not counted */ }
}
