# A work session on one work package

The user asked for this session to work in one work package. The package is: $ARGUMENTS

(If the line above shows a placeholder instead of a package name, the name is in the user's words that follow this text.)

1. If no package was named, run `rs-memory package list`. When it says the project's planner is off, answer as step 2 says for that case, and stop. Otherwise show the user the open packages (and which are being worked on in another session), ask which to take, and stop until they answer.
2. Run `rs-memory take <package>` first. Under Claude Code give that Bash call a timeout of 600000 ms: it may first consolidate the journal of the package's last session. It makes this session the package's work session; one session works in a package at a time.
   - When it says the project's planner is off: tell the user that this project works without work packages (every session works at the project's top, as your instructions say), and stop here. This skill does not apply.
   - When it refuses for any other reason (another live session works in that package, the package is done or closed, this session holds something else): tell the user what it said, and stop here.
3. From now on, for the rest of this session, work as the package's work session, as your instructions' **Work packages** text says (its "work session's work"): read the charter first, work only inside it, in the package's own folders.
