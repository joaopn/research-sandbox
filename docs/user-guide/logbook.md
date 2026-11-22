---
title: The two-stream logbook
parent: User guide
nav_order: 13
---
# The two-stream logbook

Two simultaneous logs at session end serve two readers:

```
logbook/supervisor/<YYYY-MM-DD>-<HHMM>.md      one per /log invocation
  - chronological, detailed
  - the supervisor's cold-resume memory
  - read by the supervisor next session
  - immutable after write

logbook/pi/<YYYY-MM-DD>-<slug>.md              one per coherent topic this session
  - executive, what the PI reads
  - cross-references the supervisor log via **Source:**
  - links downward to results/<name>/<NNN>_<slug>/ and outputs/<slug>/
  - immutable after write
```

The discipline: never edit a past logbook entry. Corrections become new entries in the next `/log`. This is what makes the log corpus trustworthy as the project's canonical memory.

How many PI topic logs per session?

| Situation | Topic logs |
|---|---|
| Related workers (one PI question decomposed) | 1, covering the synthesis |
| Independent workstreams | 1 per topic |
| Mix | Group by coherent topic; 1 per group |
