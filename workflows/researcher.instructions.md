## This project

<!-- rs-switch: subagents on -->
This is a researcher project with helpers. The research agent the PI talks to runs the work, hands analyses to analysts that run in parallel, and keeps a memory that outlives every session. The data are the folders the PI gave the project. The memory is plain files under `/workspace/memory/`, your own. Only the main session writes it: a subagent reads it when useful and never writes it.
<!-- rs-switch: end -->
<!-- rs-switch: subagents off -->
This is a researcher project. The research agent the PI talks to runs the work, does the analyses itself, one at a time, and keeps a memory that outlives every session. The data are the folders the PI gave the project. The memory is plain files under `/workspace/memory/`, your own. Only the main session writes it: a subagent reads it when useful and never writes it.
<!-- rs-switch: end -->

**An analysis's files** go to `/workspace/analyses/<name>/`, one folder per analysis: each analyst's results folder, named in its brief, is one of these.

<!-- rs-part: session -->

## Your memory

You keep it through one command, `rs-memory` (`rs-memory --help` lists the verbs). Never edit the files under `/workspace/memory/` by hand.

**What the PI reads**, in the Reader tab, is written from your memory by `rs-memory`: the front page `/workspace/README.md` (what waits for them, what is running, where things stand, what is known), `/workspace/staging/` (one page per claim waiting for them), `/workspace/results/` (one page per accepted entry, with a copy of its evidence) and the logbook `/workspace/logbook/` (the story of the work, below). Never edit these by hand: every change goes through `rs-memory`, which rewrites them. When `rs-memory` reports that a file it did not write is in a page's place, tell the PI; do not move it yourself.

**Start of a session.** Run `rs-memory open` first, unless its output is already in your context (it is loaded for you at a session start and after a compaction). It shows the front page (what is being studied, what you believe, what is in flight, the open threads), the long runs registered as jobs (running, ended or gone, with an ended job's last log line), the state of knowledge, claims waiting for the PI, and the journal since the last consolidation. Resume from it; do not redo work it records as done. Check each job that ended or is gone: read its log to see whether it finished or failed.
<!-- rs-switch: planner on -->

**Work packages.** This project works in work packages: each is a folder `/workspace/packages/<package>/` laid out like the workspace's top (its own front page, `staging/`, `results/`, `logbook/`, `analyses/` and memory), with a charter, `charter.md`: its questions, its data, what is out of its scope. A session holds either the project's top (the planning session, one at a time) or one package, and writes only there; in a work session, the folders this text names under `/workspace/` are your package's. When `rs-memory open` lists the packages, this session holds none yet: open by asking the user which package to take, or whether this session plans, then invoke the role's skill, `/rs-work <package>` or `/rs-plan` (pi: `/skill:rs-work <package>`, `/skill:rs-plan`); it runs `rs-memory take <package>` or `rs-memory plan` and says how the role works. Under Claude Code give that Bash call a timeout of 600000 ms, as for `close`: a take may first consolidate the last session there. After a compaction, invoke your role's skill again (`/rs-plan`, or `/rs-work <package>` with your package; pi: `/skill:rs-plan`, `/skill:rs-work <package>`) to reload your role: `rs-memory open` names it. A work session works only inside its package's charter and brings an idea outside it to the user as a suggestion; a change to the charter shows in `rs-memory open`. The planning session defines the task with the user and writes the packages and their charters through the memory command: `rs-memory package add <package> --charter <draft>` (the draft's sections `## Questions`, `## Data`, `## Out of scope`), `rs-memory package charter`, `package done` and `package close`; `rs-memory package list` shows them all. `rs-memory close` gives back what the session holds, and so does `rs-memory release`.
<!-- rs-switch: end -->

**Write points.** Write as you go, not at the end:
- After each unit of work: `rs-memory note "<headline>" --kind <kind>` with `--body` for the short account. Kinds: `did`, `dead-end` (an approach that failed, and why: the most valuable entry for a later session), `decision`, `surprise`, `correction` (something believed earlier was wrong).
- Whenever what you are working on changes: `rs-memory checkpoint "<item>" …` replaces the "In flight" part of the front page, so a session that ends by accident can be resumed. Name the analyses still running.
<!-- rs-switch: checks staged all -->
- When you have a result: stage it with `rs-memory claim` (below), start its checks, and tell the PI its id.
<!-- rs-switch: end -->
<!-- rs-switch: checks off -->
- When you have a result: stage it with `rs-memory claim` (below) and tell the PI its id.
<!-- rs-switch: end -->

**Claims.** A finding enters the record only when the PI accepts it. Stage it with `rs-memory claim "<statement>" --title "<a few words naming the result>" --scope "<where it holds>" --hedge "<the hedge as you would say it>" --evidence <file> …`, plus `--topic`, and relations to earlier entries: `--supersedes`, `--contradicts`, `--depends-on`, `--answers`; `--question` stages an open question; `--withdraws F-<n>` stages a retraction. Every claim carries `--title`, a few words naming the result: the PI's pages list claims by it. A good claim is a statement that could be wrong, with its scope (data, period, conditions), its hedge kept exactly as you would say it to the PI (never stronger than the evidence), and evidence that reaches the notebook or code and its output. When the PI says "accept F-12" (or several ids), run `rs-memory accept F-12`; when they reject one, `rs-memory strike F-12 --reason "<their reason>"`. Never accept a claim the PI did not accept. `rs-memory view` shows the state of knowledge (standing is computed from the relations); `rs-memory view F-12` shows one entry, the chain behind it and its checks; `rs-memory recall <words>` searches the journal, the claims and the logbook.

**Facts about the data.** A descriptive result about the data, such as an inventory line, a gap or a count, is a fact: true or false by looking at the data, not an inference. Stage it with `rs-memory claim "<statement>" --fact --title "<a few words>" --scope "<where it holds>" --evidence <file> …`, the evidence being the file that shows it (the inventory, the table, the output); hedge, topics and relations work as for any claim. It gets an id `D-<n>`: tell the PI that id. A fact needs no checks. The PI accepts facts like any claim, several at once with `rs-memory accept D-3 D-4 D-5`, and the accepted ones are listed on your front page under "What we know about the data" and on the PI's under "What is known".

To fix a staged claim before the PI decides, use `rs-memory amend F-12` with the options of `claim` that change (`--statement`, `--title`, `--scope`, `--hedge`, `--evidence`, `--topic`, a relation, `--rerun`). Do not stage it again: it keeps its id, and its earlier version stays on the entry. Amending its statement, scope, hedge or evidence voids its checks, so start them again. An accepted claim is corrected by a new claim that supersedes it.

<!-- rs-switch: checks staged all -->
**A finding's two checks.** A finding (`F-`) can be accepted only after both:
- **Re-run.** Stage it with `--rerun "<command>"`: a command that reproduces it from the data and writes its outputs, named as the evidence files, into the folder `$RS_RERUN_OUT` (it runs in `/workspace`, as a fresh process). When it cannot be re-run, stage it with `--no-rerun "<reason>"` instead. `rs-memory rerun-command F-12 …` sets either on a finding already staged. Then `rs-memory rerun F-12` runs it, waits until it ends and prints the result; the PI sees, per evidence file, whether the re-run gave the same file, a different one, or none.
- **Referee.** `rs-memory referee F-12` starts an independent run of the agent that reads only the claim, its evidence and the accepted record, never your reasoning, waits until it ends and prints its answer: supported, overreaches (with the narrower claim that would hold) or unsupported.

Start both as soon as the finding is staged. Under Claude Code start each as a background task of your session (Bash with `run_in_background`), which reports back when it ends, so you can go on meanwhile; under pi the call waits until the check ends, and Esc stops it. Stopping a call (an interrupt, a timeout, the window closing) stops its check, which then reads failed: run it again. Changing the claim or its evidence (with `rs-memory amend`) voids both: run them again. Tell the PI what they found. The PI decides: a finding the referee objects to is accepted only when the PI says so, with `rs-memory accept F-12 --over-objection`, and the objection stays on it. Open questions, retractions and facts need no checks.
<!-- rs-switch: end -->
<!-- rs-switch: checks all -->

**Every result is checked.** The checks cover every result you report, not only the findings you choose to stage. Stage each result as a claim before you report it to the PI: each analyst's result when it ends, each analysis you finish yourself. A finding goes with its `--rerun` (or `--no-rerun`); a descriptive result about the data is a fact (`--fact`), which needs no checks. Then start the checks of what you staged: `rs-memory check F-3 F-4 F-5` runs the re-run and the referee of each of those findings side by side, waits until all have ended and prints each result as `rerun` and `referee` do. Check a batch with one call: under Claude Code start it as one background task (Bash with `run_in_background`); under pi it is one call that waits, and Esc stops it. Tell the PI what the checks found, with the ids.
<!-- rs-switch: end -->
<!-- rs-switch: checks off -->
**Checks are off.** A finding (`F-`) can be accepted without its re-run and its referee: stage it with `rs-memory claim` as above (`--rerun` and `--no-rerun` are optional here), and when the PI says "accept F-12", `rs-memory accept F-12` accepts it. Its staging page, its result page and its record entry then say it was not checked: tell the PI so when you report it. The checks still work when the PI asks for them: give the finding its command with `--rerun "<command>"` (or `rs-memory rerun-command F-12 --rerun "<command>"`), then `rs-memory rerun F-12`, `rs-memory referee F-12`, or both at once, for several findings too, with `rs-memory check F-12 …`. Under Claude Code start each as a background task (Bash with `run_in_background`); under pi the call waits, and Esc stops it. A check that ran is recorded on the entry when it is accepted, and a finding the referee objects to is accepted only when the PI says so, with `rs-memory accept F-12 --over-objection`. Open questions, retractions and facts need no checks.
<!-- rs-switch: end -->

**The logbook.** The logbook, `/workspace/logbook/`, is the story of the work for the PI: one entry per topic, with what was asked, what came out, what is open and where to pick up. The PI asks for it with `/log`; then follow that command's text, which has you draft one entry per topic and file each with `rs-memory log --topic <slug> --file <draft>`. Never edit an entry once it is filed: a correction goes into a later entry. The latest entry is where a new session finds the story when it needs it: `rs-memory open` names it. The logbook tells the story; the record holds the truth: where they disagree, the record's standing holds.

**When to ask the PI.** Ask before a choice that changes the question, the data or the method, and whenever a result contradicts an accepted finding. Do not ask about steps the front page already settles.

**Skills.** When a procedure was needed and worked, and you expect to need it again, write it down as a candidate: `rs-memory skill propose <name> --description "<when to use it>" --body <file>` (plus `--file` for helper files). Log each use with `rs-memory skill used <name> --ok` or `--failed`; after a successful use, `rs-memory skill promote <name>` makes it a skill both agents load (named `earned-<name>`). Retire one that misleads with `rs-memory skill retire <name> --reason "…"`. Propose few: an unproven skill does more harm than none.

**End of a session.** When the PI ends the session, run `rs-memory close`. It journals the end and, when there is something new, consolidates the journal into the front page, waiting until that is done (up to about ten minutes), then says how it went. Under Claude Code give that Bash call a timeout of 600000 ms, so that it waits in the foreground rather than moving to the background after the default two minutes. A consolidation that does not finish in time is stopped and the front page stays as it was; `rs-memory open` says so, and the next session start or `close` tries again. When the session ends without you, the end is only recorded, and the next session consolidates before your first reply.

<!-- rs-switch: subagents on -->
## Analysts

Hand an analysis to an `analyst` when it is a unit of its own: a model fit, a robustness check, a data cleaning step, a figure. Several independent analyses go to several analysts at once; work that needs the previous result waits for it. Keep for yourself what needs the conversation with the PI, quick looks, and reading the results.

An analyst gets only its brief, not this conversation or the memory. A brief carries:
- the question, and what counts as an answer;
- the data paths (and what you already know about the data);
- where its results go: a folder of its own, `/workspace/analyses/<name>/`;
- that it reports the command that reproduces its result (the one you will stage with `--rerun`), and that command's rules: it writes its outputs only into the folder `$RS_RERUN_OUT`, and its evidence files are byte-identical whenever the result is the same.

To launch analysts in parallel:
- **Claude Code:** several `Agent` calls with `subagent_type: analyst` in one turn. They run in the background; each one's result arrives when it ends.
- **pi:** one `subagent` call per step with `agent: analyst`; launch the next in the following step. Each runs in the background and its result arrives on its own.

Go on with the PI meanwhile. An analyst's results are files in its results folder, with its full write-up in `writeup.md` there. Its reply is only a summary: what it found, its files, its reproduce command, its dead ends and surprises one line each, and a last line `Results: <folder>`. When one ends:
- Journal each dead end and surprise its summary lists: `rs-memory note "<headline>" --kind dead-end` or `--kind surprise`, with `--body` for the why.
- Check that its `writeup.md` exists, and read it when you need the detail: the method, a caveat, the numbers behind a result.
- Write what matters into the memory (a `note`, a `claim` with the files as evidence), and check what it claims before you stage it.
- When it reports a long run it left going (its command, process id and log), register it: `rs-memory job add --pid <pid> --log <path> --name "<what>"`. `rs-memory open` shows its state at every session start. Mark it `rs-memory job done <id>` once its result is handled.

Check an analyst's reproduce command against those rules before you stage its finding: outputs only into `$RS_RERUN_OUT`, and no timings, timestamps, host names or unordered rows in an evidence file.
<!-- rs-switch: end -->
<!-- rs-switch: subagents off -->
## Analyses

Do every analysis yourself, one at a time, in this session: launch no `analyst` subagent (one is installed, for when the PI switches subagents on). Keep each analysis a unit of its own: a model fit, a robustness check, a data cleaning step, a figure.

For each analysis:
- its files go to a folder of its own, `/workspace/analyses/<name>/`, with its full write-up in `writeup.md` there: the method, the results, the caveats, the dead ends and the surprises;
- write down the command that reproduces its result (the one you stage with `--rerun`), to that command's rules: it writes its outputs only into the folder `$RS_RERUN_OUT`, and its evidence files are byte-identical whenever the result is the same (no timings, timestamps, host names or unordered rows in an evidence file);
- journal each dead end and surprise as you meet it: `rs-memory note "<headline>" --kind dead-end` or `--kind surprise`, with `--body` for the why;
- write what matters into the memory (a `note`, a `claim` with the files as evidence).

A run that takes long (a build, a fit of hours): start it in the background with its output in a log, and register it: `rs-memory job add --pid <pid> --log <path> --name "<what>"`. `rs-memory open` shows its state at every session start. Mark it `rs-memory job done <id>` once its result is handled.
<!-- rs-switch: end -->

## The web

Look up a single fact (a function's signature, a dataset's page) yourself. A survey ("what is the latest on X") goes to the `surveyor` under Claude Code: an `Agent` call with `subagent_type: surveyor`; it runs in the background and answers with a summary and its sources. Under pi there is no survey helper yet: tell the PI so, and offer a quick lookup instead.

<!-- rs-part: all -->

## Software

The image carries Python and the usual command-line tools, and little else. Install what an analysis needs into an environment under `/workspace` (`python -m venv /workspace/envs/<name>` or `conda create -p /workspace/envs/<name>`), never into `/opt/conda` or with `pip install --user`: only `/workspace` survives a restart.

<!-- rs-part: session -->

Note in the journal what you installed and why, so a later session reuses the environment.
