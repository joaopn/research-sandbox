---
name: rs-mode
description: Switch this session's subagents and checks — subagents on or off, checks off, staged or all. Only when the user asks for it.
argument-hint: "subagents on|off | checks off|staged|all"
disable-model-invocation: true
---

# Switch this session's subagents and checks

The user asked to switch how this session works. The requested switches are: $ARGUMENTS

(If the line above shows a placeholder instead of switches, they are in the user's words that follow this text.)

This research project has two switches:
- **subagents** — `on`: hand analyses to `analyst` subagents that run in parallel; `off`: do every analysis yourself, one at a time, and launch no analyst.
- **checks** — `staged`: a finding you stage needs its re-run and its independent referee before the PI can accept it; `all`: the same, and you stage every result you report and start its checks; `off`: a finding can be accepted without its checks, and its pages say it was not checked.

If no switch was given, or a value is not one of these, run `rs-memory switch` (it prints the switches this session works under) and ask the user what they want.

Otherwise:
1. Record them for this session: `rs-memory switch` with `--subagents on|off` and `--checks off|staged|all`, only the ones the user named. It prints the switches this session now works under; when it refuses, tell the user what it says and stop here.
2. Finish the unit of work already in progress under the old switches. From the next unit, follow the text below for each switch as it now stands: it replaces the part of your instructions that covers the same thing. The text for a switch the user did not name stays as it was.

The switch lasts for this session only. The switches new sessions start with are a project setting the user changes in RS; never try to change them yourself.

## subagents on

{{RS_SWITCH_SUBAGENTS_ON}}

## subagents off

{{RS_SWITCH_SUBAGENTS_OFF}}

## checks staged

{{RS_SWITCH_CHECKS_STAGED}}

## checks all

{{RS_SWITCH_CHECKS_ALL}}

## checks off

{{RS_SWITCH_CHECKS_OFF}}
