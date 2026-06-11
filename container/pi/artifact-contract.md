# Publishing your work — research-sandbox artifact contract

This applies to every research-sandbox agent, on top of whatever your role or
repo already tells you. Two directories under your workspace decide what the
rest of the project can see:

- `published/` — your finished, shareable deliverables. The supervisor reads
  this to know what you have produced.
- `internal/` — scratch, drafts, exploration, anything that is not (yet) a
  deliverable. Private to you; nobody else reads it.

When you produce a deliverable, put the file in `published/` and record a
one-line description of it:

```
manifest describe <file> "<one line: what this file is>"
```

The description is identification only — a filename and a one-liner, for any
file type (text, data, figure, notebook, …). It is **not** a schema or a
summary; the detail stays in the file itself, which a reader opens when the
one-liner isn't enough.

You **cannot end your session while a file in `published/` has no
description.** When that happens you will be told which files; for each one,
either run the `manifest describe` command above, or move it to `internal/`
if it isn't really a deliverable.
