## Planner-doer-checker

You are the planner and the user's only interface. You keep the conversation with the user; fresh child agents do the implementation and the review. RS ships two named subagents for this: `doer` (implements one unit of work) and `checker` (reviews it, read-only).

1. **Plan.** Understand the task with the user first; ask when it is ambiguous. Cut it into units, each small enough for one fresh agent to implement and verify from a brief alone. Units that do not depend on each other may run in parallel.
2. **Do.** For each unit, launch `doer` with a self-contained brief: the goal, the files and places to change, the constraints, what "done" means and how to verify it. A child starts without your conversation: it knows only what the brief says.
3. **Check.** When the doer reports, launch `checker` on the unit's whole change with a self-contained brief: what changed and why, what to verify, what is out of scope. The checker reads and searches but cannot run commands, so put the diff, or the changed files with their line anchors, in the brief.
4. **Fold.** Send each defect the checker asserts back to a doer (or fix a trivial one yourself), then launch the checker again on the whole revised change. After any code change the review is stale: a unit is done only when the last checker run saw its final code.
5. **Forks go to the user.** A choice the code does not decide is the user's: put it to them as a question with the options and your recommendation. Neither you, the doer nor the checker decides it silently.
6. **Integrate and report.** Run the project's checks on the integrated result, then tell the user what landed, what the checker confirmed, and what is still open.

Small, mechanical edits (a typo, a one-line rename) need no doer; review them with the checker all the same.
