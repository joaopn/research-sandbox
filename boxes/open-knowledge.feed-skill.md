---
name: ok
description: Feed this agent's work into the project's OpenKnowledge knowledge base by writing knowledge-base notes into this agent's own mailbox. Run only when the user types it; never on your own.
disable-model-invocation: true
argument-hint: "[what to feed, optional]"
---

# /ok — feed the knowledge base (Claude Code: `/ok`; pi: `/skill:ok`)

Your feed folder is `{{MAILBOX_PATH}}ok/`, inside your mailbox. The project's OpenKnowledge box reads every agent's feed folder when its user runs `/ok-ingest` there; nothing you write here reaches the knowledge base before that. Nothing of this runs unasked: the user types `/ok`, you do what is below, and you stop.

## First run: write the charter

If `{{MAILBOX_PATH}}ok/CHARTER.md` does not exist, this is the first run, and you write no notes yet. Settle with the user, in conversation, what from this agent's work belongs in the knowledge base: which kinds of results (findings, datasets, decisions, methods), how much, in what shape (one note per finding, per dataset, per decision), and what never goes in (raw data, credentials, half-finished work, anything the user names). Write the answers to `{{MAILBOX_PATH}}ok/CHARTER.md` as short markdown, tell the user the charter is in place and that `/ok` writes notes from now on, and stop.

## Later runs: write notes

1. Read the charter. Read `{{MAILBOX_PATH}}ok/LOG.md` if it exists: one line per note written so far, so you do not repeat one.
2. Decide what is new and belongs in the base, following the charter. The text after the command is the user's hint about what to feed (`$ARGUMENTS`); without one, feed what is new since the last log line.
3. For each item write one self-contained markdown note into `{{MAILBOX_PATH}}ok/`, named `YYYY-MM-DD-<slug>.md`: a title line, the content, and a `Source:` line naming the file(s) or the conversation it comes from. A note must make sense on its own to a reader who has none of your context.
4. Append one line per note to `{{MAILBOX_PATH}}ok/LOG.md`: `- YYYY-MM-DD <file name> — <one line>`.
5. Tell the user what you wrote, and stop.

Rules: never edit or delete an earlier note (a correction is a new note that names the one it corrects); write nothing outside `{{MAILBOX_PATH}}ok/`; never run this on your own initiative.
