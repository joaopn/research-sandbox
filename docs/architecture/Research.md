---
title: Research
parent: Architecture
nav_order: 2
has_children: true
has_toc: false
---
# Research

RS has three research workflows today and one more planned. Each is a project type chosen in the Research group of the New Project window: **researcher**, **companion** and **research (legacy)**. They are independent: none needs another to be used, and the existing one is not changed by the others.

On these pages **the user** is the person working with RS, and a **researcher** is the agent of a lab-agenda workflow.

The newer workflows form the **lab agenda**. They are built in order, because each is made of the one before: the researcher is built (its memory first, then its helpers, checks and work packages, which are switches of the same type), the lab is planned. The existing workflow, `research`, shown as research (legacy), is the benchmark they are measured against, kept until the lab takes its parts.

| Workflow | Status | Agents | Who analyses | What remembers | Independent check | Page |
|---|---|---|---|---|---|---|
| `research` (legacy) | built | a supervisor and worker containers; Claude only | workers, never the supervisor | session logs, written at session end | the supervisor, against its own plan | [Research workflow](Research-workflow.md) |
| `researcher` | built, phases 1 and 2 | one long-lived agent and, by default, its short-lived subagents; Claude or pi | subagents, with context the researcher selects, or the agent itself with subagents off | the agent's own memory, written as it works | by default a re-run and a referee, on every finding; the user accepts | [Researcher: memory and pages](Researcher-workflow.md), [helpers, checks and work packages](Researcher-with-subagents.md) |
| `companion` | built | one long-lived agent for one-off and mixed tasks; Claude or pi | the agent itself, one folder per task | the researcher's memory, with the user's standing preferences in place of a line of inquiry | none; claims only when the user asks, and the user accepts | [The companion](Researcher-workflow.md#the-companion) |
| `lab` | planned, phase 3 | several researchers, staff, a manager | each member, on its own line of inquiry | each member's memory, plus a shared record | a referee, on every claim | [Lab workflow](Lab-workflow.md) |

The `companion` stands beside the lab agenda rather than in it: a researcher kept for one-off and mixed tasks instead of one line of inquiry, described on the [Researcher: memory and pages](Researcher-workflow.md#the-companion) page.

```mermaid
flowchart TD
  bench["research (legacy) (built)<br/>untouched, the benchmark"]
  r["researcher (built)<br/>one agent, one line of inquiry;<br/>delegates analyses, findings refereed (switches)"]
  lab["lab<br/>several researchers, a board, a meeting"]
  page["Lab page<br/>a view over the files the workflows write"]
  r --> lab
  r -.-> page
  lab -.-> page
  bench -.->|"compared on the same question"| r
```

## How they differ

| | `research` (legacy) | `researcher` | `lab` |
|---|---|---|---|
| The user talks to | the supervisor only | the researcher | the manager, or any member |
| Long-lived agents | supervisor, workers | one | every member |
| Containers | supervisor, workers, role services | one | one per member (to be decided) |
| Knowledge lives in | session and topic logs | a record of findings with evidence | each member, and a shared record |
| Parallel work | parallel workers | parallel subagents, or none with subagents off | parallel lines of inquiry |
| Rules enforced by | the worker tool and a Claude hook | a memory command | the memory command and a lab program |
| Cost when idle | none | none | none, by design |

## The lab page

A fourth phase adds a page that shows what the planned workflows write: the state of knowledge, each researcher's front page, the lab's board and meeting minutes, and answers with their notebook or plot in place. Until it exists, the same files are read in the reader and editor tabs.

Later, the user's decisions (accept a finding, approve an assignment, ask a member) move from the chat to that page.

## Design decisions

**The existing research workflow is the benchmark and is not changed.** The planned workflows are added beside it and compared with it on the same multi-session question. The cost is more workflows to explain than one.

**Independent workflows, built in order.** A member of the lab is a researcher, and a researcher's helpers and checks are switches of the one researcher type. Each can be used alone, so a small question never pays for a lab.

**Tools before prose.** A rule that matters is a command that refuses and names the next step. An agent's standing instructions hold its role and judgment; mechanics live in commands.

**The same under Claude and pi.** Nothing in the lab-agenda workflows may depend on a feature only one agent has. The existing workflow runs Claude only, because its supervision check is a Claude hook.

**The user is the only oracle.** Nothing becomes established knowledge without the user's acceptance. A referee advises the user and binds the agent.

**The files are the interface.** Agents write files in known places and know nothing about the page that shows them.
