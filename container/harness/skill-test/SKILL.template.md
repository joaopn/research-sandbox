---
name: rs-test
description: Switch this session's test policy — {{RS_TEST_PHRASE}}. Only when the user asks for it.
argument-hint: "{{RS_TEST_HINT}}"
disable-model-invocation: true
---

# Switch the test policy

The user asked to switch this session's test policy. The requested test policy is: $ARGUMENTS

(If the line above shows a placeholder instead of a test policy, the test policy is in the user's words that follow this text.)

- **light** — drop the full test policy: from now on tests are owed only where the user, the approved plan or the project's own instructions ask for them, and a new test need not be seen failing first. Keep verifying every change by running it.
{{RS_TEST_BULLETS}}
If no test policy was given, or it is not one of these {{RS_TEST_COUNT}}, ask the user which one they want.

Finish the unit of work already in progress under the old test policy, then apply the new one from the next unit. The switch lasts for this session only. The test policy new sessions start on is a project setting the user changes in RS; never try to change it yourself. Where the project's own instructions prescribe a different way of working, they still win, as they do for the test policy RS set. A difference that decides something in the work in front of you, and that the project's instructions record no ruling on, goes to the user as a question; until they answer, do everything that does not depend on it and hold the part that does.
