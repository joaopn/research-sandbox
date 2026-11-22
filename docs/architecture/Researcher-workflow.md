---
title: Researcher workflow
parent: Research
grand_parent: Architecture
nav_order: 2
---
# Researcher workflow

**Built (phase 1 of the lab agenda).** See [Research](Research.md) for the other workflows.

A `researcher` project holds one agent that pursues one line of inquiry for weeks. The user talks to it directly, and it does the analysis itself, in the project's own container. It has no workers and no subagents.

Its purpose is memory. A finding from the first week is still there in the fourth week without anyone carrying it over. A dead end is remembered as a dead end, and a wiped context costs minutes.

## Creating one

**Researcher** is a project type in the New Project window. The project runs Claude, pi or both, and needs at least one of them. The Reader tab is always on, because the memory is read there. The container is the sandbox image with its own inner Docker. The agent installs the analysis software it needs under `/workspace`.

## Memory

```mermaid
flowchart TD
  work["Work"]
  journal[("Journal<br/>everything, in order")]
  front["Front page<br/>loaded at every start"]
  record[("Record<br/>findings with evidence")]
  skills["Skills<br/>earned by the agent"]
  work -->|"note, checkpoint"| journal
  work -->|"claim"| record
  journal -->|"consolidation at session end"| front
  journal -->|"proposes"| skills
  front --> work
  record -->|"views on demand"| work
  skills -->|"when a task matches"| work
```

| Part | Holds | Changes | Reaches the agent |
|---|---|---|---|
| Front page | current work, beliefs as pointers, work in flight, open threads | rewritten at each consolidation | at every session start and after every compaction |
| Record | findings with scope, evidence and relations; open questions | append-only | as views, on demand |
| Journal | what was done and why, dead ends included | append-only | searched on demand |
| Skills | procedures worth keeping | promoted after a successful use, retired when stale | when a task matches |

Everything lives under `/workspace/memory/`, as plain files the user reads in the Reader tab.

## The memory command

The agent keeps its memory through one command, `rs-memory`. Its refusals name the next step.

| Verb | Does |
|---|---|
| `note`, `checkpoint` | add to the journal; a checkpoint before long work lets an interruption resume |
| `claim` | stage a finding with its evidence |
| `accept`, `strike` | enter a staged finding in the record, or drop it, after the user says so |
| `view`, `recall` | show the state of knowledge or one entry with its chain; search the journal and claims as text |
| `skill propose`, `used`, `promote`, `retire` | turn a working procedure into a skill for both agents |
| `open`, `close`, `consolidate` | start a session; end one and rewrite the front page |

A finding's standing (current, superseded, contested, withdrawn) is computed from its relations each time it is shown. Entries are never edited, so a correction is a new entry.

## A session

At start, a hook gives the agent its front page: Claude's start hook, or pi's extension. The agent then works and writes at named moments: a finished unit of work, a dead end, a surprise, a correction.

When the session ends, the end hook runs `close`. A headless run of the same agent reads the journal since the last consolidation and writes a new front page. The user's terminal is not held up. The old page is kept, and a run cut short is reported at the next start.

The user accepts a finding by saying so in the chat, for example "accept F-3".

## Design decisions

**Memory is four things, kept apart.** Each part changes differently and is trusted differently, so each has its own place and rule for writing. The cost is four places to keep consistent.

**The record is immutable and the user accepts every entry.** Established knowledge cannot drift, and the user decides what is known. The cost is one decision per finding.

**One command carries the rules.** A rule that matters is a refusal of the memory command, not a sentence in the instructions. The instructions hold the agent's role and judgment.

**The front page comes through the agent's own hooks.** Both agents receive it at start and after a compaction, so a wiped context recovers without the user. Claude's built-in memory is off in these projects, so the agent has only one memory.

**Consolidation runs in the background at session end.** A headless run of the same agent writes the new page, using the user's model quota. A session killed with its container skips it; the next one consolidates everything since.

**Skills are earned.** A procedure becomes a skill only after a successful use. It goes into both agents' skills folders, so Claude and pi share it.

**Plain search first.** The journal is searched as text. An index is added only if measurement shows plain search missing things.

**Discipline, not security.** The agent can write the memory files by hand. The command is the easy path, and the user's accept is a chat message the agent acts on.
