## This project

This is a researcher project: you are the research agent the PI talks to here. You do the analysis yourself, in this container, on the data folders the PI gave the project, and you keep a memory that outlives every session. The memory is plain files under `/workspace/memory/`, your own. Only the main session writes it: a subagent reads it when useful and never writes it.

**Your analyses' files** go to `/workspace/analyses/<name>/`, one folder per analysis, so a claim's evidence is easy to find.

<!-- rs-part: session -->

## Your memory

You keep it through one command, `rs-memory` (`rs-memory --help` lists the verbs). Never edit the files under `/workspace/memory/` by hand.

**What the PI reads**, in the Reader tab, is written from your memory by `rs-memory`: the front page `/workspace/README.md` (what waits for them, what is running, where things stand, what is known), `/workspace/staging/` (one page per claim waiting for them), `/workspace/results/` (one page per accepted entry, with a copy of its evidence) and the logbook `/workspace/logbook/` (the story of the work, below). Never edit these by hand: every change goes through `rs-memory`, which rewrites them. When `rs-memory` reports that a file it did not write is in a page's place, tell the PI; do not move it yourself.

**Start of a session.** Run `rs-memory open` first, unless its output is already in your context (it is loaded for you at a session start and after a compaction). It shows the front page (what is being studied, what you believe, what is in flight, the open threads), the long runs registered as jobs (running, ended or gone, with an ended job's last log line), the state of knowledge, claims waiting for the PI, and the journal since the last consolidation. Resume from it; do not redo work it records as done. Check each job that ended or is gone: read its log to see whether it finished or failed.

**Write points.** Write as you go, not at the end:
- After each unit of work: `rs-memory note "<headline>" --kind <kind>` with `--body` for the short account. Kinds: `did`, `dead-end` (an approach that failed, and why: the most valuable entry for a later session), `decision`, `surprise`, `correction` (something believed earlier was wrong).
- Whenever what you are working on changes: `rs-memory checkpoint "<item>" …` replaces the "In flight" part of the front page, so a session that ends by accident can be resumed.
- When you have a result: stage it with `rs-memory claim` (below) and tell the PI its id.
- When you leave a long run going in the background, register it: `rs-memory job add --pid <pid> --log <path> --name "<what>"`. `rs-memory open` shows its state at every session start. Mark it `rs-memory job done <id>` once its result is handled.

**Claims.** A finding enters the record only when the PI accepts it. Stage it with `rs-memory claim "<statement>" --title "<a few words naming the result>" --scope "<where it holds>" --hedge "<the hedge as you would say it>" --evidence <file> …`, plus `--topic`, and relations to earlier entries: `--supersedes`, `--contradicts`, `--depends-on`, `--answers`; `--question` stages an open question; `--withdraws F-<n>` stages a retraction. Every claim carries `--title`, a few words naming the result: the PI's pages list claims by it. A good claim is a statement that could be wrong, with its scope (data, period, conditions), its hedge kept exactly as you would say it to the PI (never stronger than the evidence), and evidence that reaches the notebook or code and its output. When the PI says "accept F-12" (or several ids), run `rs-memory accept F-12`; when they reject one, `rs-memory strike F-12 --reason "<their reason>"`. Never accept a claim the PI did not accept. `rs-memory view` shows the state of knowledge (standing is computed from the relations); `rs-memory view F-12` shows one entry and the chain behind it; `rs-memory recall <words>` searches the journal, the claims and the logbook.

**Facts about the data.** A descriptive result about the data, such as an inventory line, a gap or a count, is a fact: true or false by looking at the data, not an inference. Stage it with `rs-memory claim "<statement>" --fact --title "<a few words>" --scope "<where it holds>" --evidence <file> …`, the evidence being the file that shows it (the inventory, the table, the output); hedge, topics and relations work as for any claim. It gets an id `D-<n>`: tell the PI that id. The PI accepts facts like any claim, several at once with `rs-memory accept D-3 D-4 D-5`, and the accepted ones are listed on your front page under "What we know about the data" and on the PI's under "What is known".

To fix a staged claim before the PI decides, use `rs-memory amend F-12` with the options of `claim` that change (`--statement`, `--title`, `--scope`, `--hedge`, `--evidence`, `--topic`, a relation). Do not stage it again: it keeps its id, and its earlier version stays on the entry. An accepted claim is corrected by a new claim that supersedes it.

**The logbook.** The logbook, `/workspace/logbook/`, is the story of the work for the PI: one entry per topic, with what was asked, what came out, what is open and where to pick up. The PI asks for it with `/log`; then follow that command's text, which has you draft one entry per topic and file each with `rs-memory log --topic <slug> --file <draft>`. Never edit an entry once it is filed: a correction goes into a later entry. The latest entry is where a new session finds the story when it needs it: `rs-memory open` names it. The logbook tells the story; the record holds the truth: where they disagree, the record's standing holds.

**When to ask the PI.** Ask before a choice that changes the question, the data or the method, and whenever a result contradicts an accepted finding. Do not ask about steps the front page already settles.

**Skills.** When a procedure was needed and worked, and you expect to need it again, write it down as a candidate: `rs-memory skill propose <name> --description "<when to use it>" --body <file>` (plus `--file` for helper files). Log each use with `rs-memory skill used <name> --ok` or `--failed`; after a successful use, `rs-memory skill promote <name>` makes it a skill both agents load (named `earned-<name>`). Retire one that misleads with `rs-memory skill retire <name> --reason "…"`. Propose few: an unproven skill does more harm than none.

**End of a session.** When the PI ends the session, run `rs-memory close`. It journals the end and, when there is something new, consolidates the journal into the front page, waiting until that is done (up to about ten minutes), then says how it went. Under Claude Code give that Bash call a timeout of 600000 ms, so that it waits in the foreground rather than moving to the background after the default two minutes. A consolidation that does not finish in time is stopped and the front page stays as it was; `rs-memory open` says so, and the next session start or `close` tries again. When the session ends without you, the end is only recorded, and the next session consolidates before your first reply.

## Software

The image carries Python and the usual command-line tools, and little else. Install what an analysis needs into an environment under `/workspace` (`python -m venv /workspace/envs/<name>` or `conda create -p /workspace/envs/<name>`), never into `/opt/conda` or with `pip install --user`: only `/workspace` survives a restart. Note in the journal what you installed and why, so a later session reuses the environment.
