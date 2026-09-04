# Dev Environment

You are running inside a sandboxed dev container. All your work must go through git.

## Instruction precedence

Your instructions come in three layers. Highest to lowest:

1. **The project's own instructions** — the repo's `CLAUDE.md` and anything else the
   maintainer wrote into the project. These are the maintainer's standing directives for
   this codebase. Where they conflict with this file, they win.
2. **This file** — the dev-container ground rules.
3. **Your agent runtime's built-in defaults** — generic cautions the tooling ships with
   (e.g. "do not spawn subagents or run workflows unless the user requested it").

Both instruction layers outrank the built-in defaults: the maintainer wrote them, so
anything they direct you to do IS the user requesting it. When the project's workflow tells
you to spawn checker subagents, work in worktrees, or act autonomously, follow it — do not
treat a built-in caution as a conflict, and do not ask for permission the project's
instructions already gave.

**If two sections of THIS file disagree, the rule marked as a hard rule wins.** Fix the
offending section in this file and tell the maintainer, so the template it was staged from
gets fixed too — these files drift as they grow.

## Git Remotes

You have two remotes:
- **`origin`** — your fork on Gitea (read-write). This is your workspace. You own it.
- **`upstream`** — the mirror of the real GitHub repo (read-only). The maintainer's source of truth.

Do not add other remotes.

## Git Workflow

### Your base branch

Your base branch is **`{{BASE_BRANCH}}`**. That is the branch you sync, the branch you start
new work from, and the branch your pull requests target. The maintainer can point you at a
different one at any time — if they ask, start that work with `rs-wt new <name> --base
origin/<that branch>`, sync it inside the worktree, and target your pull requests at it; the
primary clone stays on `{{BASE_BRANCH}}`.

### Syncing the base branch

Sync it from **both** remotes before starting new work and before landing. Other sessions may
have landed PRs on your fork (`origin`); the maintainer may have collected your work or
pushed changes to GitHub (`upstream`).

This flow runs **in the primary clone**, which is always checked out on `{{BASE_BRANCH}}` —
it is the only git operation you run there yourself. Your own work never happens there (see
"Worktrees (rs-wt) — every unit of work" below).

```bash
git checkout {{BASE_BRANCH}}
git fetch origin
git rebase origin/{{BASE_BRANCH}}        # catch up with landings on your fork; also repairs a sync that did not finish
git fetch upstream
git rebase upstream/{{BASE_BRANCH}}      # your base is always a straight line on top of the mirror
git push --force-with-lease --force-if-includes origin {{BASE_BRANCH}}
```

Read the outcome before you carry on:

- **Up to date** — nothing moved; the rebases and the push do nothing.
- **Catching up with your fork** — other sessions landed work, or a previous sync did not
  finish: the first rebase brings you level.
- **Your collected copies dropped** — the maintainer collected your landed work, so the
  mirror carries it under new commit ids; the second rebase drops your copies and replays
  only what is not collected yet. This is the normal steady state after every collection,
  not a fault — say so in one line in your report.
- **A conflict at the first rebase** (onto your fork): `git rebase --skip`. Everything it can
  skip already exists on your fork or on the mirror, and the second rebase re-attaches you to
  the real commits (a conflict may then reappear there).
- **A conflict at the second rebase** (onto the mirror): the maintainer changed something you
  also touched. Keep the maintainer's version (during a rebase `--ours` is the mirror's side),
  re-apply yours on top, `git rebase --continue`. When keeping the maintainer's version leaves
  nothing of yours to re-apply, the commit was already collected (squashed together with
  others) — `git rebase --skip` it.
- **The push refused** — someone landed work while you were syncing: run the block again from
  the top.

### Your base branch is a straight line on top of the mirror

The mirror (`upstream`) is the maintainer's repo and it is sacred: you cannot push to it, and
you never resolve a conflict by changing what the maintainer has. Your fork's base branch is
yours to rewrite, and the rebases above are how it moves — it must always follow from the
mirror's head with no merge commits. **Never merge the mirror into your base branch**: a merge
commit on the base breaks the maintainer's per-commit collection, a content-free one included.

After a sync rewrote your base, bring every open branch onto it, now: in its worktree
(`rs-wt reopen <name>` if you had closed it) run `git fetch origin && git rebase
origin/{{BASE_BRANCH}}`, then force-push the feature branch — otherwise an open PR shows
commits that are no longer on the base and rs-land refuses to land it. Merged PRs and the
archive tags keep pointing at your pre-collection commits; that is expected — the tags are the
record of what landed, not part of the live line.

**STOP and ask** when a conflict cannot be resolved without altering the maintainer's content:
`git rebase --abort`, comment on the issue saying what you saw, and wait. Do not `reset --hard`
to something else, do not merge the mirror in, do not guess. Waiting costs nothing; guessing
can lose commits.

### Branches

- **Use the `agent/` prefix** for new feature branches: `agent/add-auth`, `agent/fix-parser`.
  This is a naming convention that keeps your in-progress work easy to spot — not a
  restriction.
- **Never move the primary clone off `{{BASE_BRANCH}}`** — hard rule, see "Worktrees" below.
  To continue an existing `agent/` branch, `rs-wt reopen <name>`; to build on any other
  existing branch, start a new `agent/` branch from it with `rs-wt new <name> --base
  origin/<branch>`.
- **Keep every branch with an open PR rebased onto the current `{{BASE_BRANCH}}`** as part
  of normal work — when something else lands on the base branch, rebase your open PR
  branches onto it and force-push them (the base branch moves only through the sync above),
  so review always looks at
  work sitting on the current base.
- **Land your finished work with `rs-land <pr-number>`** once the maintainer approves the
  pull request. One command does the whole landing: it merges fast-forward-only, tags what
  landed as `<pr>-<feature>` (the branch name without its `agent/` prefix — the durable
  record of what landed), and deletes the branch. It refuses rather than repairs — if it
  refuses because the base branch moved, rebase your branch onto `{{BASE_BRANCH}}`,
  force-push the feature branch (the base branch moves only through the sync above), and run
  it again; if that rebase
  had conflicts, describe the resolution in a PR comment and ask for a fresh review before
  landing. If it keeps refusing for a reason you cannot fix, report the refusal text in a
  PR comment and wait — do not work around it.
- **Commit often locally**, with small commits and clear messages.
- **No attribution lines.** Never add a co-author trailer, an AI-generated footer, or a
  session link to a commit message or a PR description — the maintainer's repository carries
  no agent attribution, and a session link is a private URL. Your runtime is configured not
  to add them; if one appears anyway, remove it before pushing.
- **Push when you finish a logical chunk of work** — a completed task or milestone, before a
  risky operation, or when you want the maintainer to review. The maintainer collects your
  work commit by commit, so a clean, linear sequence of well-scoped commits matters — not
  just the final diff.

## Worktrees (rs-wt) — every unit of work

Every unit of work — a feature, a fix, a review follow-up, whether you are the only session
or one of several — happens in its own git worktree with its own `agent/` branch. Worktrees
are mandatory for every unit of work, not only when sessions run in parallel. The primary
clone at `/workspace/<repo>` is the integration tree: it stays checked out on
`{{BASE_BRANCH}}`, the base-branch sync runs there, and nothing else does. Two things ARE fine
in the primary clone, because they never move its HEAD: editing files git ignores (a
project's own notes and plans usually live there and never reach a worktree — `git worktree
add` checks out tracked files only), and running project-level commands that pin the clone's
path (a compose stack, a data directory) — build from the worktree, run from the clone.

- **Never change the branch checked out in the primary clone.** The only checkout that ever
  runs there is `git checkout {{BASE_BRANCH}}` — the sync's first line, or putting the clone
  back. No `git checkout <other branch>`, no `git switch`, no `git checkout -b` there — not to
  preview a PR, not to run branch code, not because another section seems to allow it.
  Path-level checkouts while resolving a rebase (`git checkout --ours -- <file>`) restore
  files and do not move the branch — those are fine. Other sessions share that one HEAD, and
  `rs-wt` refuses to start work while it is off `{{BASE_BRANCH}}`. If you find it on another
  branch, put it back and say so.
- **Start:** `rs-wt new <name>` (short, feature-shaped name). It creates branch
  `agent/<name>` from `origin/{{BASE_BRANCH}}` — the base comes from the project's wiring;
  pass `--base <ref>` only when the maintainer named a different branch — and prints your
  worktree path (`/workspace/wt/<name>`). `cd` there; ALL your work happens under that path.
- **Own your tree only.** Never edit files in the primary clone or another session's
  worktree, and never check out the base branch — it stays checked out in the primary clone.
- **Sync inside a worktree:** `git fetch origin`, then `git rebase origin/{{BASE_BRANCH}}` on
  your own branch. The base-branch sync flow above is primary-clone-only.
- **Overview:** `rs-wt list` shows every tree (branch, last commit, dirty state) plus
  branches whose tree was removed.
- **Finish:** push your branch, open the PR as usual, then `rs-wt done <name>` — it removes
  the worktree and always keeps the branch. Resume finished work later with
  `rs-wt reopen <name>` (a fresh `rs-wt new` under an old name refuses and points you there).
- **Shared git state:** every worktree shares the primary clone's `.git` — config, the stash
  stack, remotes and hooks are ONE set for every session, so a change in one tree is a change
  in all of them; coordinate rather than assume you are alone. Keep `origin` pointing at your
  fork and `upstream` at the mirror: `rs-wt` gates on `origin` being the shared Gitea and
  branches from `origin/{{BASE_BRANCH}}`, the sync block above fetches both by name, and
  `checkout.defaultRemote` resolves ambiguous branch names against `origin`. An open PR's head
  branch stays until `rs-land` retires it after the landing — deleting it orphans the PR
  (Gitea marks it branch-deleted and there is no head left to merge). `rs-wt reopen` and
  `rs-wt list` need the local `agent/<name>` branch, so delete one only when you are finished
  with that name. Packages installed into the container filesystem (`pip install --user`,
  `npm -g`, apt) vanish at a container recreate; anything under `/workspace` — a venv there,
  for instance — survives.
- **Servers and ports:** don't leave servers running unattended. Bind only the port the
  maintainer names when they ask for a demo, and stop it afterwards.
- **Reference clones:** if you need another repo just to read it, clone it under your
  worktree or your home directory — never top-level under `/workspace` (rs-wt needs the
  single top-level clone to stay unambiguous).

## Verification

- Run tests before pushing. If tests exist, run them.
- If you add a feature, add or update tests for it.
- Run linters/formatters if the project configures them.
- Do not push code you haven't verified.

When fixing a bug, write a reproduction script first (exits non-zero while the bug exists,
zero when fixed), and verify it on both the base branch and your branch before pushing.

## What You Have Access To

- This workspace (the cloned repo at `/workspace/<repo>`)
- **`/workspace` is the only filesystem that survives.** The container's own filesystem — your
  home, `/opt`, `/usr`, anything a package manager writes — is rebuilt at every container
  recreate (a box re-run, a project stop + start). Anything you need afterwards — a venv, a
  tool install, a cache, notes — goes under `/workspace`. `pip install --user`, `npm -g` and
  apt are yours to run, and gone after the next recreate; a venv under `/workspace` is not.
- Internet access for API calls and package installation (subject to the project's egress policy)
- Git push/pull to Gitea (`origin` for push, `upstream` for fetch)

## What You Do NOT Have Access To

- The user's real GitHub repo (no credentials for it)
- The host filesystem (this is an isolated container)
- The local network (LAN access is blocked)

## Working Style

- Read existing code before making changes. Understand patterns before modifying.
- Prefer editing existing files over creating new ones.
- Keep changes focused. One branch per task.
- If unsure about an approach, create the branch, push what you have, and note the
  uncertainty in the commit message. The maintainer will review.
- **Ask questions in plain text**, in your reply, with the options and your recommendation —
  never through the runtime's question-popup tool. The maintainer reads this tab
  intermittently; a popup times out unanswered and the work stalls.
- **Never write the runtime's memory files** (`~/.claude/projects/<…>/memory/`, `MEMORY.md`).
  On a dev box that directory is discarded at every re-run; on a supervisor it survives, but
  nobody reads it. Durable notes go where the maintainer reads them: the project's own
  instruction and plan files.

## Gitea API

Your Gitea API token is in `$GITEA_TOKEN`, the base URL is `$GITEA_URL/api/v1`,
and your repo path is `$GITEA_USER/$REPO_NAME`. On a dev box these are container
env (always set). On a dev project's supervisor they are set only inside
repo-watch-launched sessions; in other sessions read the wiring from
`/workspace/.orchestrator/dev-gitea.json` (`agent_user` is your user, its
`token_file` names your token under `~/.dev-tokens/`; the base URL is
`http://rs-gitea:3000/api/v1`). Git push/pull authenticate via the credential
store and need no env.

## Delivering work — the final comment

When you open a pull request (and when you close out a task issue after opening
its PR), END your comment with the exact fetch command the maintainer runs to
pull your work, in a fenced code block on its own:

```
rs-fetch <repo-name> --pr <N>
```

Use the real repo name and the PR number Gitea returned when you opened the PR.
The code block matters: Gitea renders it with a copy button, so the maintainer
copies it in one click. Never invent a different command shape.

**Mirror your plans and bug records to Gitea.** The maintainer reads the Gitea board, not
your container's filesystem. Every plan you write for them and every bug you record in the
project's ledger is duplicated as a Gitea issue: title = the plan's name, or the bug's stable
code plus its one-line symptom; body = the file's text verbatim. The file in the repo is
authoritative and the issue is its mirror — re-sync the body when the entry changes, and
close the issue naming the PR when the work lands. Never keep only one of the two.

**Never hard-wrap prose in issues, PR bodies or comments.** Gitea renders a single newline as
a line break, so a paragraph wrapped at 80 columns is shredded on the board. One paragraph is
one line; separate paragraphs with a blank line (lists, tables and fenced code keep their own
lines).
