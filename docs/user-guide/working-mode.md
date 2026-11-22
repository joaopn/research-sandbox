---
title: Working mode and test policy
parent: User guide
nav_order: 18
---
# Working mode and test policy

A dev project (`--workflow dev`) and each dev box carry two switches that set how their agent works. Both are chosen when the project or box is created and changed afterwards in its ⚙ panel in the browser; Claude Code and pi behave alike.

**Working mode** — who does the work and who reviews it:

| Mode | Code | How the agent works |
|---|---|---|
| `none` | 0 | Follows only the project's own instructions |
| `direct` | 1 | Shows the plan, waits for your go, implements and verifies itself |
| `doer-checker` | 2 | As `direct`, plus RS's checker subagent reviews the plan before any code and the change before it is committed |
| `checker-doer` | 2b | Plans with you; RS's doer subagent implements each unit, the agent reviews each change itself |
| `planner-doer-checker` | 3 | Plans with you; the doer subagent implements each unit and the checker subagent reviews it |

**Test policy** — how much testing RS asks for:

| Policy | What the agent does | Meant for |
|---|---|---|
| `light` (default) | Verifies each change by running it, runs the tests the project already has, adds tests only where you or the approved plan ask for them | A young project |
| `full` | Every feature ships its tests, every bug fix a reproduction, and a new or changed test counts only once it was seen failing without the change | A mature project |

The two are independent: the test policy holds in every working mode, `none` included. The project's own instructions win over both.

The setting is what every new session starts with. Inside a running session, switch that session only:

| | Claude Code | pi |
|---|---|---|
| Working mode | `/rs-mode <mode>` | `/skill:rs-mode <mode>` |
| Test policy | `/rs-test <policy>` | `/skill:rs-test <policy>` |

`/rs-mode` also takes the mode's code (`/rs-mode 2b`). A project's change in the ⚙ panel needs no restart and reaches new sessions; a dev box restarts when its working mode or test policy changes.

From the command line, `project create` and `project update` take `--harness-mode <mode>` and `--test-policy <policy>`.
