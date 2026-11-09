# Dev Environment

You are running inside a sandboxed dev container. All your work must go through git.

## Instruction precedence

Your instructions come in three layers. Highest to lowest:

1. **The project's own instructions** — the maintainer's directives recorded under the
   `## Maintainer directives` heading of `/workspace/.claude/CLAUDE.md` (see "Project
   memory" below), any `CLAUDE.md` the repo itself tracks for its contributors, and anything
   else the maintainer wrote into the project. These are the maintainer's standing directives
   for this codebase. Where they conflict with this file, they win. Your own notes, lessons,
   workflow write-ups and area rules in the project memory are guidance below this file:
   they never override it, nor your runtime's built-in defaults.
2. **This file** — the dev-container ground rules.
3. **Your agent runtime's built-in defaults** — generic cautions the tooling ships with
   (e.g. "do not spawn subagents or run workflows unless the user requested it").

Both instruction layers outrank the built-in defaults: the maintainer wrote them, so
anything they direct you to do IS the user requesting it. When the project's workflow tells
you to spawn checker subagents, work in worktrees, or act autonomously, follow it — do not
treat a built-in caution as a conflict, and do not ask for permission the project's
instructions already gave. A workflow you wrote down yourself carries that weight only where
the maintainer's directives record it. Permission to work autonomously is never a go for a
particular piece of work (see "The maintainer's go" below); only the project's instructions
saying in so many words that work may start without one lift that rule.

A checker or reviewer subagent the workflow asks for is a FRESH child: it starts without your
conversation, so it judges the work and not your reasoning about it. Never fork your own
session for it. Unless the project's instructions name another agent, launch the ones RS
ships, under the same names in Claude Code and pi:

- **`checker`** — a read-only reviewer. It reads and searches but cannot edit files or run
  commands, so its brief carries the diff, or the changed files with their line anchors.
- **`doer`** — implementation help: one unit of work from a self-contained brief that
  quotes the maintainer's go.
- **Any other runtime** — its general-purpose agent, under whatever name its docs give it.

Where no agent by those names exists (a box inside a research project, or a project not
restarted since RS gained them), use the general-purpose agent in Claude Code or the
`delegate` agent in pi.

Put the whole brief in the task, self-contained: the child knows nothing you do not write
there, and pi refuses a single-agent launch whose task is empty.

When the workflow asks for a review of your diff, the last review must have seen the code you
push: a code change made after it — a one-line tidy-up included — needs a fresh review of the
full diff. Code is anything that runs or is read as behaviour: source (its strings included —
messages, help text), configuration, test assertions and fixtures, and text shipped for an
agent to follow (instructions, prompts) — for that text, an edit counts as code when it
changes the meaning. Non-code changes need no fresh review: comments, docstrings, whitespace
and re-wrapping that no program, tool or test reads; docs written for people, such as a
README; an edit to agent text that leaves its meaning unchanged, such as a typo fix; commit
messages and PR descriptions. A doc written for people counts as agent text when the
project's instructions tell agents to follow it, such as a CONTRIBUTING file. A rebase that
applies cleanly is not a code change; resolving its conflicts is. Say in the PR when non-code
edits came after the last review. When you cannot tell whether an edit changes behaviour (or,
for agent text, its meaning), treat it as code.

**If two sections of THIS file disagree, the rule marked as a hard rule wins.** Fix the
offending section in this file and tell the maintainer, so the template it was staged from
gets fixed too — these files drift as they grow.

## The maintainer's go — before any work starts

**No work starts without the maintainer's go — hard rule.** Work means `rs-wt new`, any edit
to a tracked file, a test or harness, a demo stack, a commit, a PR. The wiki is not work in
this sense: like the project memory, its pages and their commits need no go (see "The wiki").
Before any work, the maintainer has seen the plan for this piece of work — what you will
build, the questions it raises, your recommendations — and has then told you, in the session,
to build it: "go", "go, but change X", "write the PR", "implement it". A go that carries
changes is still a go: fold the changes into the plan and build that.

Nothing else is a go, however much it reads like one:

- **An answer to a question** — "yes", "agreed", "sounds good", "yes, turn that off", "I'd go
  with your recommendations". An answer settles what to build; it does not say to build it.
- **A task given before there is a plan** — "work on #N", "work on the next issue", "start on
  X", "we need X", "fix X". It opens the conversation: read, measure, write the plan and its
  questions, and ask.
- **Anything on Gitea** — an issue, a plan posted there, a card in Now or Next, a label. Gitea
  is where you publish for the maintainer to read; the maintainer's decisions come in the
  session.
- **A go for something else** — another issue, or an earlier version of this plan that has
  since changed in a way the maintainer would notice.

**Quote the go before you start.** In the reply where you run `rs-wt new`, quote the
maintainer's message word for word and name the plan it approves. If you cannot quote one, you
do not have a go: stop and ask for it.

**Until the go, the work is talking:** reading code, measuring (a probe or reproduction kept
outside the repo or in a path git ignores included; after the go it can become the branch's
test), writing the plan and its questions, having it checked when the project asks for that,
posting it on the issue, keeping the project memory. End every reply that leaves a plan waiting
with the ask: "say go to start, or tell me what to change". "Proceed under the recommendation"
applies to choices inside work that already has its go, never to the decision to start. A
message that might be a go but does not clearly tell you to build is not one: ask. A question
costs the maintainer a minute; work built on a misreading costs a day and a review of something
nobody asked for.

**Scope.** A direct instruction from the maintainer, in the session, naming one operation on
work already under way — "land #N", "rebase your PR", "sync the base", "stop the demo", a
change asked for in review of an open PR — is carried out as asked; a review comment on Gitea
is information until the maintainer says it in the session. Building anything new is never such
an operation. Work that already started under a go needs no new one when the maintainer asks
you, in the session, to pick it up ("continue #N"): `rs-wt reopen` its branch. A branch or open
PR you come across is not a reason to continue building on your own; bringing your own open PRs
onto a rewritten base, as the Git Workflow says, is upkeep and needs no ask. A child agent
doing work (a `doer`, or the agent used in its place) works under the go its parent quotes in
its brief, and never starts work of its own.

## Git Remotes

You have two remotes:
- **`origin`** — your fork on Gitea (read-write). This is your workspace. Its open PRs are
  not all yours: each belongs to the agent that opened it (see "Rebase only the open PRs that
  are yours" below).
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
apart from fetching and reading refs (see "Worktrees" below), it is the only git operation
you run there yourself. Your own work never happens there (see
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

After a sync rewrote your base, bring the open PR branches you are working on onto it, now:
each in its worktree (`rs-wt reopen <name>` if you had closed it) run `git fetch origin &&
git rebase origin/{{BASE_BRANCH}}`, then force-push the feature branch — otherwise an open PR
shows commits that are no longer on the base and rs-land refuses to land it. This is never
every open PR on the fork (the hard rule below); a PR you were only told to merge is brought
level when you land it. Merged PRs and the archive tags keep pointing at your pre-collection
commits; that is expected — the tags are the record of what landed, not part of the live line.

**Rebase only the open PRs that are yours — hard rule.** An open PR is yours to rebase in
exactly two cases: you are actively working on it (you opened it and are still carrying it,
or the maintainer told you to take it over), or the maintainer told you to merge it. Every
other open PR on the fork belongs to the agent that opened it — every agent in this container
files under the same Gitea user and shares this clone, so "open on the fork" never means
"yours". Never rebase, force-push or resolve the conflicts of an open PR outside those two
cases, not even to bring it level after a sync: the agent that opened it has the context to
resolve it, and a rebase and force-push move its branch under it, on Gitea and in this shared
clone. A PR you were told to merge but are not working on (you are not carrying it and were
not told to take it over) may be rebased only when the rebase applies cleanly; if it
conflicts, `git rebase --abort`, say so on the PR and leave it to the agent that opened it or
to the maintainer. If any PR you would rebase — one you were told to merge or to take over —
has its branch checked out in another session's worktree, do not rebase it: tell the
maintainer — never work in another session's tree. When you cannot tell whether a PR is
yours, it is not.

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
- **Keep the branch of each open PR you are working on rebased onto the current
  `{{BASE_BRANCH}}`** as part of normal work — when something else lands on the base branch,
  rebase that branch onto it and force-push it, so review always looks at work sitting on the
  current base. Only the PRs you are working on (the hard rule above); a PR you were only told
  to merge is brought level when you land it (next bullet).
- **Land your finished work with `rs-land <pr-number>`** once the maintainer approves the
  pull request. One command does the whole landing: it merges fast-forward-only, tags what
  landed as `<pr>-<feature>` (the branch name without its `agent/` prefix — the durable
  record of what landed), and deletes the branch. It refuses rather than repairs — if it
  refuses because the base branch moved, rebase your branch onto `{{BASE_BRANCH}}`,
  force-push the feature branch (the base branch moves only through the sync above), and run
  it again; if that rebase
  had conflicts, describe the resolution in a PR comment and ask for a fresh review before
  landing (on a PR you are working on; on one you were only told to merge, the hard rule
  above applies — a conflicting rebase is aborted and left to its agent). If it keeps
  refusing for a reason you cannot fix, report the refusal text in a PR comment and wait — do
  not work around it.
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
`{{BASE_BRANCH}}`, the base-branch sync runs there, and nothing else does. Three things ARE
fine in the primary clone, because they never move its HEAD: editing files git ignores (they
never reach a worktree — `git worktree add` checks out tracked files only), running
project-level commands that pin the clone's path (a compose stack, a data directory) — build
from the worktree, run from the clone — and fetching `origin` to read its refs
(`git -C /workspace/<repo> fetch origin`,
`git -C /workspace/<repo> show origin/<branch>:<path>`), which changes no checked-out file.

- **Never change the branch checked out in the primary clone.** The only checkout that ever
  runs there is `git checkout {{BASE_BRANCH}}` — the sync's first line, or putting the clone
  back. No `git checkout <other branch>`, no `git switch`, no `git checkout -b` there — not to
  preview a PR, not to run branch code, not because another section seems to allow it.
  Path-level checkouts while resolving a rebase (`git checkout --ours -- <file>`) restore
  files and do not move the branch — those are fine. Other sessions share that one HEAD, and
  `rs-wt` refuses to start work while it is off `{{BASE_BRANCH}}`. If you find it on another
  branch, put it back and say so.
- **Start** (only with the maintainer's go — see "The maintainer's go"): `rs-wt new <name>`
  (short, feature-shaped name). It creates branch `agent/<name>` from
  `origin/{{BASE_BRANCH}}` — the base comes from the project's wiring;
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

## Project memory — under `/workspace`, never committed

What you learn about this project outlives your session only if you write it down, and the
maintainer reads it only where they look. Every dev project keeps the same memory, in the same
place, with the same habits; set it up in your first session if it is not there yet.

**Hard rule: the project memory is never committed.** It lives in `/workspace`, beside the
repo and outside any git repository, so no commit can pick it up; never make any of it a git
repository either (`rs-wt` needs exactly one top-level clone). Never copy, move or link any
of it into the primary clone or a worktree (git commits a symlink like any other file), and
never paste it into, or name its files in, a commit message or a PR body — they are dead
links to anyone reading the history. A `CLAUDE.md` or `.claude/` the repo itself
tracks belongs to the maintainer: change it only through a PR, like any other file, and read
it in your worktree before your first edit — a session started in `/workspace` loads it at
best once a file under it is read, and some agents never load a `.claude/CLAUDE.md` below
their start directory.

| Path | What it holds |
|---|---|
| `/workspace/.claude/CLAUDE.md` | The project's instruction file, which you keep. The maintainer's standing directives go under a `## Maintainer directives` heading, each recorded when given, verbatim and dated; only that section outranks this file. Everything else in it is yours: your write-up of the project's workflow, lessons about how to work, and an index table of the area rule files. |
| `/workspace/.claude/rules/<area>.md` | One file per area of the code: the invariants and traps learned there, so a later session does not rediscover them the hard way. Each starts with `paths:` frontmatter naming the files it covers, and loads only when one of them is read. |
| `/workspace/PLAN/` | One file per plan: the design, the cut, the questions and the maintainer's answers. `PLAN/README.md` indexes them with their status. |
| `/workspace/PLAN/DONE/` | Shipped plans, moved here and never deleted. `PLAN/DONE/MANIFEST.md` gets one row per move: the PR number and the commit subject that shipped it — never a commit id, which the maintainer's collection rewrites. |
| `/workspace/PLAN/BUG_BUCKET.md` | Bugs found along the way that are not the current work: `B<n>` entries, never renumbered or deleted, each with the symptom, a reproducer and a fix sketch. A fix lands in its own PR, never inside unrelated work. |
| `/workspace/PLAN/IMPROVEMENTS.md` | The same for improvements: `I<n>` entries. |

**Area rule paths are matched from `/workspace`**, so write them to match the primary clone and
every worktree alike — `**/src/parser/**`, not `src/parser/**`:

```yaml
---
paths:
  - "**/src/parser/**"
---
```

**Start every session in `/workspace`.** The project memory and the area rules are found from
the directory a session starts in, and your work happens in worktrees under `/workspace/wt/`,
outside the primary clone. Started in `/workspace`, all of it loads and every area rule fires
for the primary clone and every worktree alike. Started inside the clone or a worktree, some of
it fails to load or never fires, depending on the agent; started outside `/workspace`, none of
it loads, this file included. Your shell starts in the session's start directory: if that is
not `/workspace`, say so in your first reply and suggest restarting there. The exception is a
session `rs-repo-watch` launched: it starts in the primary clone by design, so
skip that notice there.

**After every landing** (`rs-land` succeeded):

1. Move each plan the PR shipped to `PLAN/DONE/`, add its `MANIFEST.md` row, and update its
   status in `PLAN/README.md`.
2. Mark each ledger entry the PR resolved `Fixed in: PR #<n> <subject>` and leave it in
   place. Find them by searching, not from memory: run
   `git -C /workspace/<repo> fetch origin`, read the entries in `PLAN/BUG_BUCKET.md` and
   `PLAN/IMPROVEMENTS.md` not yet marked fixed or superseded, and search them for the files
   and functions the PR changed. For each one whose code the PR touched, check whether it
   still holds on the branch the PR landed on, read through
   `git -C /workspace/<repo> show origin/<branch>:<path>` (`{{BASE_BRANCH}}` unless the
   maintainer pointed the PR elsewhere) and never from the primary clone's files, which have
   not synced since the landing; check by reading the code, or with a probe kept outside the
   repo. Fixing one that still holds is new work and waits for the maintainer's go. When the
   PR removed what an entry is about or changed its premise, mark it
   `Superseded by: PR #<n> <subject>` instead; when an entry stays open but its fix sketch
   no longer fits, correct the sketch in place. If another session has started the entry's
   issue (the issue's latest `Working on it: branch …` comment, with no `Stopped`, `Landed`
   or `Closed` comment after it, names a branch that is not yours), keep the issue open:
   comment on it naming the PR that resolved it, and name it in your report instead of
   closing it in step 4. Say how many you found, zero included, in the reply that reports
   the landing.
3. If the work taught something a later session needs — an invariant the code depends on, a
   trap, a reusable pattern — add it to that area's rule file (a lesson about how to work
   goes in `/workspace/.claude/CLAUDE.md`). Most landings teach nothing worth a line.
4. Close the mirrored issues naming the PR — `rs-board done <issue> --pr <N>`, which also
   removes each card — and re-sync any whose file changed.
5. Check the wiki against what the PR changed and update the pages it touched (see "The
   wiki" below); most landings change nothing there.

## The wiki — how the repository works, for the maintainer

Your fork on Gitea has a wiki. It holds the repository's functional structure, written for
the maintainer: how things are done, what connects to what, and the core design decisions
and why they were taken. Its job is to keep the maintainer in the loop of how the system
works, so write it the way you write to them: what happens for the person using the system
first, mechanism only where it helps them follow it. It is not your project memory (the
traps and invariants an agent needs live there), not a copy of the plans or the ledgers, and
not code documentation. Keeping it needs no go (see "The maintainer's go"), and you say which
pages you changed in the reply that reports the landing or the pass.

- **Shape.** A `Home` page is the map: one paragraph on what the repository does, then a
  table with one row per other page — a link to it and one line on what it covers. Each
  other page covers one part of the system as the user meets it: what it does, what it
  connects to (a diagram), the decisions that shaped it and why (a short paragraph each),
  and a table wherever the subject is a matrix. Prefer a diagram or a table to a paragraph
  that lists things.
- **Diagrams are Mermaid blocks** (a fenced code block marked `mermaid`): Gitea draws them
  in the page and they stay text you can edit. Use an image file only where Mermaid cannot
  draw the thing.
- **Pages go through the API**, as your fork's owner: `<owner>/<repo>` is the
  `$GITEA_USER/$REPO_NAME` path of the Gitea API section — on a dev project's own agent, the
  `agent_user` and `repo` of the wiring row whose `agent_user` is not empty, never its
  `user`. If that repository's `has_wiki` is `false` (`GET /repos/<owner>/<repo>`), its wiki
  is switched off: tell the maintainer instead of working around it.
  - `POST /repos/<owner>/<repo>/wiki/new` with `title` and `content_base64` creates a
    page; the wiki comes into existence with its first page.
  - `GET /repos/<owner>/<repo>/wiki/pages` lists the pages with their `title` and `sub_url`,
    30 to a page: add `?page=2`, `?page=3` … until you have the `X-Total-Count` it reports;
    before the first page exists it answers 404.
  - `GET /repos/<owner>/<repo>/wiki/page/<sub_url>` reads one; `PATCH` on the same path
    replaces it and needs both the `title` that read returned and the new `content_base64`.
    Read a page just before you replace it — another session may have changed it.
  - Address a page by its `sub_url`, never by its title (a title with a dash in it is stored
    under an escaped name), and link pages the same way: `[Dev lane](Dev-lane)`.
- **An image goes in through the wiki's git repository**, once the wiki has its first page
  (before that the clone is refused). Its URL is your `origin` URL with the trailing `.git`
  replaced by `.wiki.git`. Clone it under your home directory, as `~/<repo>.wiki` (never
  top-level under `/workspace`, which must hold exactly one clone), or `git pull` the clone
  already there, and give it your own identity before the first commit — otherwise the
  commit fails, or goes out under someone else's name:

  ```
  git -C ~/<repo>.wiki config user.name "$(git -C /workspace/<repo> config user.name)"
  git -C ~/<repo>.wiki config user.email "$(git -C /workspace/<repo> config user.email)"
  ```

  Every session in the container shares that clone: pull before you add to it, and push
  right after you commit. Then embed the image in a page by its file name:
  `![what it shows](diagram.png)`.
- **When.** At every landing (step 5 of "After every landing"), check whether the PR changed
  how something works, what connects to what, or a core decision, and update the pages it
  touched; most landings change nothing there. A part with no page yet gets one, and a row
  on `Home`. When the maintainer asks for the wiki ("write the wiki", "update the wiki for
  X"), do a full pass over what they named: read the code, compare it with the pages, and
  rewrite what is out of date.
- **Where it lives.** The wiki stays on Gitea: collection takes the code repository only, so
  it does not reach GitHub. Archiving your fork (a destroyed project or box) archives the wiki
  with it; removing the repository from the dev lane, or purging your identity, deletes it.
  It is its own repository, outside the worktree rule, with no review step — edit it
  directly, no worktree, no branch, no PR — and the maintainer and every fetch tool on the
  dev lane can read it, so never put a token, a password or anything else secret in it.

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
  Nor is a conda environment: `conda create -n <name>` puts it (and conda's package cache)
  under `/workspace/.conda/`; conda's base install is root-owned and reset at every recreate.
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
- If unsure about an approach before the go, make it a question in the plan. After the go,
  push what you have and note the uncertainty in the commit message; the maintainer will
  review.
- **Ask questions in plain text**, in your reply, with the options and your recommendation —
  never through the runtime's question-popup tool. The maintainer reads this tab
  intermittently; a popup times out unanswered and the work stalls.
- **Never write the runtime's memory files** (Claude Code's `~/.claude/projects/<…>/memory/`, any `MEMORY.md`).
  On a dev box that directory is discarded at every re-run; on a supervisor it survives, but
  nobody reads it. Durable notes go into the project memory under `/workspace` (see "Project
  memory"), never into the repo.

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
your container's filesystem. Every plan you write for them and every bug or improvement you
record in the project's ledgers is duplicated as a Gitea issue: title = the plan's name, or
the entry's stable code (`B<n>`, `I<n>`) plus its one-line summary; body = the plan file's
text, or the entry's text, verbatim. The file under `/workspace/PLAN/` is authoritative and
the issue is its mirror — re-sync the body when the entry changes, and close the issue
naming the PR when the work lands (an issue another session has started stays open — "After
every landing", step 2). Never keep only one of the two.

**Never hard-wrap prose in issues, PR bodies or comments.** Gitea renders a single newline as
a line break, so a paragraph wrapped at 80 columns is shredded on the board. One paragraph is
one line; separate paragraphs with a blank line (lists, tables and fenced code keep their own
lines).

## The area boards

The maintainer stacks the repository's open issues on Gitea project boards: one board per
`area:*` label, titled by the bare area (`webui`, `dev-lane`, …); a repository with no area
label yet has one board titled after the repository. Every board has five columns, Inbox the
default:

| Column | Meaning |
|---|---|
| **Now** | an agent is on it, or its PR is open and waits for review |
| **Next** | the maintainer's stack, top first |
| **Decide** | a blocking question waits for the maintainer |
| **Inbox** | filed, not placed yet |
| **Backlog** | the maintainer's parking lot |

Every open issue is a card on its area board; closing an issue removes its card; pull
requests are never cards (a card shows the PR that references its issue). The card's text is
the issue title, its tags are the other labels. Never drive the boards by hand: every move
goes through `rs-board`, on PATH in every dev container (`rs-board --dry-run <verb>` prints
the writes and makes none).

- **Session start: `rs-board reconcile`.** It creates a missing board or label, adds a card for
  every open issue without one, removes the cards of closed issues, re-homes a card whose area
  changed, moves a card carrying `decision:blocking` or `decision:open` from Inbox to Decide,
  and drops either label from a card the maintainer dragged out of Decide (the column wins
  over the label). It never touches Now, Next or Backlog otherwise. Read its `report:` lines.
- **"Work on the next issue" = `rs-board next [--area X]`:** a card the maintainer dragged
  into Now first, then the top of Next in the maintainer's order (`priority:high` first across
  boards; a Next card still carrying `decision:blocking` is skipped and named); when several
  boards tie it lists them and you ask which. Inbox and Backlog are never sources unless the
  maintainer names the issue. The card it gives you is the next one to plan; building it
  waits for the maintainer's go.
- **Start = `rs-board start N --branch agent/<name>`**, only with the maintainer's go (card
  to Now, the branch commented on the issue; refuses a card another branch is on).
  **Ask = `rs-board ask N`** after posting a blocking question (label on, card to Decide);
  **`rs-board answered N`** when the answer is read (label off, card back to Now). **Stop
  early = `rs-board stop N --reason …`** (card to the bottom of Next). **Landing =
  `rs-board done N --pr M`**: closes the issue naming the PR and removes its card
  (idempotent). Put `closes #N` in every PR body so the card shows the PR; Gitea closes the
  issue only when the PR targets the default branch.
- **Filing: name the area board in the create call** (`"projects": [<board id>]` alongside
  `labels`; the ids come from `GET /repos/<owner>/<repo>/projects`), so the card lands in
  Inbox in the same call; a forgotten one is caught by the next reconcile. Keep titles short:
  the card is the title.

## Your shell has no terminal

<!-- LOCKSTEP (verbatim block, nine homes): container/supervisor/CLAUDE.md, boxes/{byo,data-wrangler,dev,open-knowledge,paper-orchestra,websearcher,zotero}.instructions.md and workflows/open-knowledge.instructions.md carry this section byte-for-byte between these two markers — change one, change all (pytest-pinned). Headless surfaces (the worker template, the role.md files) deliberately do not carry it. -->

Wherever this session is hosted — a byobu tab or the VS Code extension — the commands you run get **no terminal**: stdin is empty (end-of-file), stdout is a file, `[[ -t 1 ]]` is false. Consequences, and the forms that work:

- **Anything that asks a question gets end-of-file, not a wait.** `apt` without `-y` aborts, a REPL exits at once, `ssh` password and host-key prompts fail with "no tty". Supply the answer on the command line (`-y`, `--yes`, `DEBIAN_FRONTEND=noninteractive`, a config file); never plan on answering interactively.
- **A long-running command in the foreground blocks the tool call until it ends or the tool times out**, and you cannot interrupt it. Start servers, watchers and anything past a couple of minutes in the background with output redirected to a file (`nohup … > run.log 2>&1 &`), then poll the file with bounded loops; never `tail -f`, and never a bare `sleep` on its own — a call spent only sleeping learns nothing, so sleep inside a loop that checks the file.
- **Anything that opens the terminal device itself hangs** (a TUI, an editor such as `vim`/`nano`, a curses progress bar, `git` waiting for an editor on a merge or a rebase). Use the non-interactive form: `EDITOR=true`, `git -c core.editor=true`, `--no-edit`, a heredoc into the file.
- **You cannot attach to a byobu or tmux session, and you cannot drive one.** If something genuinely needs a live terminal (a debugger prompt, an interactive login), say so and let the user do it in a byobu tab; do not try from here.
- **Scripts that branch on a tty take their non-interactive path** — no colour, no prompts, no cursor tricks. Pagers already behave as `cat` here; setting `PAGER=cat GIT_PAGER=cat` only makes that explicit.

<!-- /LOCKSTEP -->
