# OpenKnowledge box

A knowledge-base box — **OpenKnowledge** (a markdown editor and knowledge base
for LLM wikis, specs and notes) with node and the pinned `ok` CLI installed,
started by the box itself in its own tab. The agent is on by default.

## What it is

A disposable container whose knowledge base is the folder `notes/` inside the
box. OpenKnowledge serves that folder as a live editor in a webui tab, and the
agent edits the same notes through OpenKnowledge's own tools — you watch its
edits land while you write beside it. Everything else in the box is its own
housekeeping; only `notes/` is content.

## Using it

Adding the box gives the project an **OpenKnowledge** tab. Open it: the box
starts OpenKnowledge at the address the tab is opened at, which takes about a
second (OpenKnowledge accepts only that exact address, so opening the tab under
another host name moves it there, and an editor still open under the old name
stops saving). After a fresh restart of the box the tab waits about a minute
while the box reinstalls OpenKnowledge.

To work with the agent, open the box's terminal tab, start a second window and
run `cd /workspace/notes && claude`, then log in. Starting the agent in `notes/`
is what gives it OpenKnowledge's tools.

If the tab does not come up, the box's terminal tab says why:
`cat /workspace/.rs-web.log` shows OpenKnowledge's own output, and
`rs-web-restart` restarts it. The tab cannot be removed in ⚙ → Ports; removing
the box removes it.

## Keeping history on Gitea

With **Gitea history** ticked in the box window (shown when this host's Gitea is
enabled), the box's `notes/` is a clone of a private repo `notes/<project>.<box>`
on the shared Gitea, and OpenKnowledge's own sync pushes every edit: the box
turns on **pull and push** sync when OpenKnowledge first starts on the repo
(changing it later in OpenKnowledge's settings is kept). While history is off
for a reason a restart can fix, the box waits rather than start OpenKnowledge
without it. The repo opens in the box's `<box> (GIT)` tab.

- The box has its own Gitea account. It can add to the notes' `main` history
  but never delete or rewrite it: `main` refuses force-pushes and deletion (tags
  and other branches are not protected), and
  the account cannot delete or rename the repo or create repos of its own. It can
  fork its notes repo into its own name; such a copy disappears with the box.
- Nobody else's account can see the repo; the Gitea tab (the site admin) can.
- Removing the box archives the repo (read-only, history kept) and deletes the
  box's account; adding a box with the **same name in the same project** again
  continues that history. Destroying the project does the same for each box.
- The box's terminal message says whether history is on and, if not, why and
  what to do. If the Gitea side itself could not be set up, the box is created
  without history and the add dialog alone says so (remove the box and add it
  again to retry). History needs a box name that Gitea accepts (not `git`, `wiki`,
  `rss`, `atom`, `keys`, `gpg` or `png`, short enough for a 40-character
  account name, and with no two of `-` `.` `_` next to each other or at the end
  of the project's or the box's name), and it does not combine with rs-fetch on
  the same box.

## Notes

- **A fresh restart** (restarting the project, changing the box's folders)
  logs the agent out and reinstalls OpenKnowledge, which then starts again by
  itself. The notes in `notes/` are untouched.
- **Source material:** give the box project folders under **Read-only folders**
  (in the box window, or later in ⚙ → Box folders); they appear in the box under
  `/mnt/<folder>`, read-only, beside the knowledge base.
- OpenKnowledge registers its tools for **Claude Code** only; a box running
  another agent works on the notes as plain files.
- **Leave Browser off** for this box: the browser image carries an older node,
  which the agent's OpenKnowledge tools would pick up.
- The agent's OpenKnowledge tools fetch the newest OpenKnowledge release when
  the agent starts (that is how `ok init` registers them), so the box needs
  network for them.
- Boots credential-free: run `claude` then `/login` inside. Egress follows the
  project's router policy.
