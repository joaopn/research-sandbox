---
name: rs-mode
description: Switch this session's working mode — none, direct or planner-doer-checker. Only when the user asks for it.
argument-hint: "none | direct | planner-doer-checker"
disable-model-invocation: true
---

# Switch the working mode

The user asked to switch this session's working mode. The requested mode is: $ARGUMENTS

(If the line above shows a placeholder instead of a mode, the mode is in the user's words that follow this text.)

- **none** — drop the working mode RS set for this project: from now on follow only the project's own instructions and the user's requests about how to work.
- **direct** — read `modes/direct.md` in this skill's folder and follow it from now on.
- **planner-doer-checker** — read `modes/planner-doer-checker.md` in this skill's folder and follow it from now on.

If no mode was given, or it is not one of these three, ask the user which one they want.

Finish the unit of work already in progress under the old mode, then apply the new one from the next unit. The switch lasts for this session only. The mode new sessions start in is a project setting the user changes in RS; never try to change it yourself. Where the project's own instructions prescribe a different way of working, they still win, as they do for the mode RS set.
