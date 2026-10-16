# OpenKnowledge box

A knowledge-base box — **OpenKnowledge** (a markdown editor and knowledge base
for LLM wikis, specs and notes) with node and the pinned `ok` CLI installed. The
agent is on by default.

## What it is

A disposable container whose knowledge base is the folder `notes/` inside the
box. OpenKnowledge serves that folder as a live editor in a webui tab, and the
agent edits the same notes through OpenKnowledge's own tools — you watch its
edits land while you write beside it. Everything else in the box is its own
housekeeping; only `notes/` is content.

## Starting it

The box's terminal tab prints these steps when it opens:

1. In this project's **⚙ → Ports**, pick the box, add port **8080** as a
   **Web page**, and copy the address shown under it. The box restarts fresh;
   reconnect its tab and wait until `ok --version` answers (OpenKnowledge
   reinstalls, about a minute).
2. `cd /workspace/notes && ok init` — once, the first time.
3. `OK_ALLOW_EXTERNAL=1 ok start -p 8080 --no-open-browser --idle-shutdown off
   --external-url <that address>`, then open the OpenKnowledge tab.
4. In a second window, `cd /workspace/notes && claude` and log in. Starting the
   agent in `notes/` is what gives it OpenKnowledge's tools.

## Keeping history on Gitea

With **Gitea history** ticked in the box window (shown when this host's Gitea is
enabled), the box's `notes/` is a clone of a private repo `notes/<project>.<box>`
on the shared Gitea, and OpenKnowledge's own sync pushes every edit once you turn
on **"pull and push"** sync in its settings (once, after `ok init`). The repo
opens in the box's `<box> (GIT)` tab.

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

- **A fresh restart** (registering a port, restarting the project) stops
  OpenKnowledge and logs the agent out, and OpenKnowledge reinstalls in the
  background. The notes in `notes/` are untouched; wait for `ok --version`, then
  repeat steps 3 and 4.
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
