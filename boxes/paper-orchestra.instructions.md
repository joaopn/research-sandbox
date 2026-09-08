# PaperOrchestra box

You are an agent in a disposable **PaperOrchestra box** — a confined container
seeded at boot with the [PaperOrchestra](https://github.com/Ar9av/PaperOrchestra)
repo already cloned. The clone lives at `/workspace/PaperOrchestra`; start there.

Read the repo's own `README` / docs first and follow **its** conventions — the box
carries no project-specific framing of its own beyond this note. Set up the
project as the repo describes (install dependencies, create any required config),
then work from the editor or the shell.

If `/workspace/.tools-inventory.md` exists, read it for any project MCP servers
wired into this box.

This box is disposable and credential-free — run `claude` then `/login` inside to
authenticate. There is no artifact-publishing contract; your outputs live in
`/workspace` (and persist on the project volume across box stop/start).

## Your shell has no terminal

<!-- LOCKSTEP (verbatim block, seven homes): container/supervisor/CLAUDE.md and boxes/{byo,data-wrangler,dev,paper-orchestra,websearcher,zotero}.instructions.md carry this section byte-for-byte between these two markers — change one, change all (pytest-pinned). Headless surfaces (the worker template, the role.md files) deliberately do not carry it. -->

Wherever this session is hosted — a byobu tab or the VS Code extension — the commands you run get **no terminal**: stdin is empty (end-of-file), stdout is a file, `[[ -t 1 ]]` is false. Consequences, and the forms that work:

- **Anything that asks a question gets end-of-file, not a wait.** `apt` without `-y` aborts, a REPL exits at once, `ssh` password and host-key prompts fail with "no tty". Supply the answer on the command line (`-y`, `--yes`, `DEBIAN_FRONTEND=noninteractive`, a config file); never plan on answering interactively.
- **A long-running command in the foreground blocks the tool call until it ends or the tool times out**, and you cannot interrupt it. Start servers, watchers and anything past a couple of minutes in the background with output redirected to a file (`nohup … > run.log 2>&1 &`), then poll the file with bounded loops; never `tail -f`, and never a bare `sleep` on its own — a call spent only sleeping learns nothing, so sleep inside a loop that checks the file.
- **Anything that opens the terminal device itself hangs** (a TUI, an editor such as `vim`/`nano`, a curses progress bar, `git` waiting for an editor on a merge or a rebase). Use the non-interactive form: `EDITOR=true`, `git -c core.editor=true`, `--no-edit`, a heredoc into the file.
- **You cannot attach to a byobu or tmux session, and you cannot drive one.** If something genuinely needs a live terminal (a debugger prompt, an interactive login), say so and let the user do it in a byobu tab; do not try from here.
- **Scripts that branch on a tty take their non-interactive path** — no colour, no prompts, no cursor tricks. Pagers already behave as `cat` here; setting `PAGER=cat GIT_PAGER=cat` only makes that explicit.

<!-- /LOCKSTEP -->
