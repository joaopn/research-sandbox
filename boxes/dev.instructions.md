# Dev Environment

You are running inside a sandboxed dev container. All your work must go through git.

## Git Remotes

You have two remotes:
- **`origin`** — your fork on Gitea (read-write). This is your workspace. You own it.
- **`upstream`** — the mirror of the real GitHub repo (read-only). The maintainer's source of truth.

Do not add other remotes.

## Git Workflow

### Syncing the base branch

Sync the repo's default branch (the base branch) from **both** remotes before starting new
work. The maintainer may have merged PRs on your fork (`origin`) or pushed changes to GitHub
(`upstream`).

```bash
git checkout <base-branch>
git pull origin <base-branch>            # Get PRs the maintainer merged on your fork
git fetch upstream
git merge upstream/<base-branch>         # Get changes from the real GitHub repo
git push origin <base-branch>            # Keep your fork up to date
```

### Conflict resolution: origin vs upstream

If merging upstream produces conflicts, **upstream wins** — it mirrors the real GitHub repo
and is the maintainer's final word.

```bash
git merge --abort
git reset --hard upstream/<base-branch>
git push origin <base-branch> --force
```

This is safe because your work lives on `agent/*` branches, never on the base branch; the
only thing lost is the fork's base-branch pointer, not any branch or commit. After resetting,
continue normally: `git checkout -b agent/my-feature`.

### Branches

- **Use the `agent/` prefix** for feature branches: `agent/add-auth`, `agent/fix-parser`.
- **Do not create branches without the `agent/` prefix** (except the base branch).
- **Commit often locally**, with small commits and clear messages.
- **Push when you finish a logical chunk of work** — a completed task or milestone, before a
  risky operation, or when you want the maintainer to review. The maintainer squash-merges
  your branch, so what matters is a clean, correct final diff.

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
