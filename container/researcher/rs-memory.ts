// rs-memory for pi
//
// RS's research memory kit, the pi side: RS writes this file into the agent
// folder's `extensions/` directory in a researcher project, where pi loads it by
// itself. At the end of a session it hands the journal to a consolidation run
// (`rs-memory close --hook`), which starts one only when there is something new.
//
// - The call is made synchronously and detached (`setsid -f`): pi may already be
//   dying when its window closes, and a spawn from an asynchronous handler never
//   happens then (measured for RS's activity recorder); `setsid -f` returns at
//   once and the run lives on in its own session.
// - A pi-subagents child, and a consolidation run itself, do nothing.
// - Nothing here ever throws into pi.
//
// The functions above `export default` are pure and tested on their own.

import { spawnSync } from "node:child_process";

export const TOOL = "/usr/local/lib/rs/rs-memory";

export function inactive(env: Record<string, string | undefined>): boolean {
	return Boolean(env.PI_SUBAGENT_CHILD) || env.RS_MEMORY_CONSOLIDATING === "1";
}

export function closeArgs(): string[] {
	return ["-f", TOOL, "close", "--hook", "--agent", "pi"];
}

function closeNow(): void {
	try {
		spawnSync("setsid", closeArgs(), { stdio: "ignore" });
	} catch { /* never into pi */ }
}

export default function (pi: any) {
	if (inactive(process.env)) return;
	pi.on("session_shutdown", () => closeNow());
}
