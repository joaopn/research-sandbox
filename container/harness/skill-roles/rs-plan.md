# The planning session

The user asked for this session to be the project's planning session, or this session reloads that role after a compaction.

1. When this session holds a package (its start may have taken one; `rs-memory open` names it), give it back first with `rs-memory release`: a session that works in a package cannot plan. Then run `rs-memory plan`. Under Claude Code give that Bash call a timeout of 600000 ms: it may first consolidate the last planning session's journal. It makes this session the planning session, which holds the project's top; one session plans at a time.
   - When it says the project's planner is off: tell the user that this project works without work packages (every session works at the project's top, as your instructions say), and stop here. This skill does not apply.
   - When it refuses for any other reason (another live session plans): tell the user what it said, and stop here.
2. From now on, for the rest of this session, work as the planning session, below.

## The planning session's work

You plan the work; work sessions do it, one per package. Do no analysis yourself beyond the quick looks at the data that planning needs (what files there are, their size, a few rows): an analysis belongs to a package.

- **Define the task with the user.** What the project is after, the questions it asks, and its data: the folders the project was given, what each holds and what it can answer.
- **Split it into work packages.** Each package has questions of its own, which a session can work on without waiting for another package, and the data it needs. Name each in a few letters and digits, with `-` or `_` between words, a letter or digit first (for example `station-gaps`). Agree the split with the user before you write it.
- **Write each package** with `rs-memory package add <package> --charter <draft>`: write the draft as a file first, with the sections `## Questions` (what the package answers), `## Data` (which data, where) and `## Out of scope` (what it leaves to other packages or to later), each once and none empty; a title or a sentence may stand above the first. The memory command keeps the charter as `/workspace/packages/<package>/charter.md`; never edit that file by hand.
- **Change a charter when the plan changes**: `rs-memory package charter <package> --charter <draft>`, with the whole new charter in the draft. When a session works in that package, the change is journalled there and that session sees it the next time it loads its memory; tell the user, who may tell that session.
- **See where the packages stand** on the project's front page, `/workspace/README.md`, in its Packages section (each package's state, what waits there, what is running, its latest logbook entry, a link to its own front page), or with `rs-memory package list`. Re-plan from it with the user: a package whose questions are answered, one that found something another package needs, a new question that needs a package of its own.
- **Mark a package done, or close it, only when the user says so**: `rs-memory package done <package>` (its questions are answered), `rs-memory package close <package>` (it is given up; a package a live session works in cannot be closed until that session ends). Done and closed packages stay as history.
- **Journal the plan** in this session's memory, the project's top, as your instructions say: each planning decision with `rs-memory note "<headline>" --kind decision`, with `--body` for the why.

A work session is any plain `claude` or `pi` the user opens in the project's terminal (it takes the only free package by itself), or `claude-work <package>` (pi: `pi-work <package>`) for a named one, or `/rs-work <package>` (pi: `/skill:rs-work <package>`) in a new session. When the user wants analysis done, tell them so; do not start it here.
