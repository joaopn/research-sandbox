---
title: After updating RS
parent: User guide
nav_order: 19
---
# After updating RS

An update to RS's own code (a `git pull`) reaches the running system in parts. Each part is brought live by one step:

| Part of RS | Brought live by |
|---|---|
| Host commands (`research.py …`) | Nothing; each command runs the current code |
| The broker | `python research.py broker stop`, then `python research.py broker start`; it loads the code once, when it starts |
| The browser UI | `python research.py webui start --rebuild`; its pages are built into its image |
| What a DIND project receives at each start: RS's instructions, the working-mode and test-policy files, the box tool | Stop and start the project |
| Container images | `python research.py start --rebuild` (or **Rebuild all** in the browser, Sandboxes → Software), then stop and start each project |

When it is unclear which parts an update touched, do all of them. The cost is the rebuild time and one restart per project, which interrupts the work running in it.

A project that has not been restarted since an update keeps working as before. An action that needs the new version there is refused with a message that says to restart the project.

## Once: the update that stops tracking `.env`

`.env` used to be part of the repository. It is now ignored, so your settings stay on your machine. Pulling that update deletes an unchanged `.env`, and the next `python research.py start` writes a fresh one from `.env.example`. A `.env` you edited stops the pull with an error about local changes. In every case, keep a copy and put it back after the pull, in the folder you cloned RS into:

```sh
cp .env .env.keep
git checkout -- .env
git pull
mv .env.keep .env
```

A clone made after this update never needs this.
