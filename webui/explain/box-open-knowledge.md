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
