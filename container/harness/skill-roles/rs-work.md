# A work session on one work package

The user asked for this session to work in one work package, or this session reloads that role after a compaction. The package is: $ARGUMENTS

(If the line above shows a placeholder instead of a package name, the name is in the user's words that follow this text.)

1. If no package was named, run `rs-memory package list`. When it says the project's planner is off, answer as step 2 says for that case, and stop. Otherwise show the user the open packages (and which are being worked on in another session), ask which to take, and stop until they answer.
2. Run `rs-memory take <package>` first. Under Claude Code give that Bash call a timeout of 600000 ms: it may first consolidate the journal of the package's last session. It makes this session the package's work session; one session works in a package at a time.
   - When it says the project's planner is off: tell the user that this project works without work packages (every session works at the project's top, as your instructions say), and stop here. This skill does not apply.
   - When it refuses for any other reason (another live session works in that package, the package is done or closed, this session holds something else): tell the user what it said, and stop here.
3. From now on, for the rest of this session, work as the package's work session, below.

## The work session's work

- **Read the package's charter** first: `/workspace/packages/<package>/charter.md`, its questions, its data and what is out of its scope. Work only inside it.
- **Use the package's own folders.** Where your instructions name a folder under `/workspace/` (the front page, `staging/`, `results/`, `logbook/`, `analyses/`), this session's is the package's, under `/workspace/packages/<package>/`: each analysis folder, and each analyst's results folder in its brief, is `/workspace/packages/<package>/analyses/<name>/`. The memory command writes into the package's memory by itself, because this session holds the package.
- **An idea outside the charter is a suggestion to the user**, never work done here: say what it is and which package it would belong to. The user may take it to the planning session, which changes the charters.
- **A charter change** made by the planning session shows in `rs-memory open` (a journal entry naming the sections that changed): read the charter again when you see one, and work under the new one.
- **Subagents and checks** follow this session's switches, as your instructions say (`rs-memory switch` prints them; the user changes them for this session with `/rs-mode`, pi: `/skill:rs-mode`).
- **When the package's questions are answered**, tell the user; when they say it is done, run `rs-memory package done <package>`.
