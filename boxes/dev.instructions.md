# Dev Environment

You are running inside a sandboxed dev container. All your work must go through git.

## Git Remotes

You have two remotes:
- **`origin`** — your fork on Gitea (read-write). This is your workspace. You own it.
- **`upstream`** — the mirror of the real GitHub repo (read-only). The maintainer's source of truth.

Do not add other remotes.

## Git Workflow

### Your base branch

Your base branch is **`{{BASE_BRANCH}}`**. That is the branch you sync, the branch you start
new work from, and the branch your pull requests target. The maintainer can point you at a
different one at any time — if they ask, use the branch they name from then on.

### Syncing the base branch

Sync it from **both** remotes before starting new work. The maintainer may have merged PRs on
your fork (`origin`) or pushed changes to GitHub (`upstream`).

This flow runs **in the primary clone only** — the single-session / repo-watch surface. In a
parallel worktree session, sync your own branch instead (see "Parallel sessions" below) and
never check out the base branch.

```bash
git checkout {{BASE_BRANCH}}
git pull origin {{BASE_BRANCH}}            # Get PRs the maintainer merged on your fork
git fetch upstream
git merge upstream/{{BASE_BRANCH}}         # Get changes from the real GitHub repo
git push origin {{BASE_BRANCH}}            # Keep your fork up to date
```

If that merge fast-forwards cleanly, carry on — nothing was overwritten and there is nothing
to decide.

### When upstream has diverged: STOP and ask

If the merge does **not** fast-forward — conflicts, or `upstream/{{BASE_BRANCH}}` has moved in
a way that does not simply replay on top of your base — stop and ask the maintainer.

```bash
git merge --abort
```

Then comment on the issue saying what diverged (which branch, roughly what changed) and wait
for the maintainer to tell you how to proceed.

**Never resolve this on your own.** Do not `reset --hard`, do not force-push, do not rebase
your base branch to make the problem go away. Your own merged work may live on this branch,
and discarding it can destroy work the maintainer has not collected yet. Waiting costs
nothing; guessing can lose commits.

### Branches

- **Use the `agent/` prefix** for new feature branches: `agent/add-auth`, `agent/fix-parser`.
  This is a naming convention that keeps your in-progress work easy to spot — not a
  restriction.
- **You may check out and work on any existing branch** in the repo — in the primary clone,
  when you are the only session there; a parallel worktree session stays on its own branch.
- **Merge your finished work into your base branch** once a pull request is approved.
- **Commit often locally**, with small commits and clear messages.
- **Push when you finish a logical chunk of work** — a completed task or milestone, before a
  risky operation, or when you want the maintainer to review. The maintainer squash-merges
  your branch, so what matters is a clean, correct final diff.

## Parallel sessions (rs-wt worktrees)

When several agent sessions work this repo at once, each session gets its own git worktree —
a private checkout with its own branch. The primary clone at `/workspace/<repo>` is the
integration tree (repo-watch and single-session work happen there); parallel sessions never
touch it.

- **Start:** `rs-wt new <name>` (short, feature-shaped name). It creates branch
  `agent/<name>` from `origin/{{BASE_BRANCH}}` and prints your worktree path
  (`/workspace/wt/<name>`). `cd` there; ALL your work happens under that path.
- **Own your tree only.** Never edit files in the primary clone or another session's
  worktree, and never check out the base branch — it stays checked out in the primary clone.
- **Sync inside a worktree:** `git fetch origin`, then `git rebase origin/{{BASE_BRANCH}}` on
  your own branch. The base-branch sync flow above is primary-clone-only.
- **Overview:** `rs-wt list` shows every tree (branch, last commit, dirty state) plus
  branches whose tree was removed.
- **Finish:** push your branch, open the PR as usual, then `rs-wt done <name>` — it removes
  the worktree and always keeps the branch. Resume finished work later with
  `rs-wt reopen <name>` (a fresh `rs-wt new` under an old name refuses and points you there).
- **Never, in any session:** `git stash` (the stash stack is shared across all trees), edits
  to git config/remotes/hooks, deleting branches, or global package installs (pip, `npm -g`,
  apt) without asking the maintainer first.
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

## Gitea API

Your Gitea API token is in `$GITEA_TOKEN`. Base URL: `$GITEA_URL/api/v1`.
Repo path: `$GITEA_USER/$REPO_NAME`.

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
