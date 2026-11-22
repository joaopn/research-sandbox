---
title: Slugs, finalize, accept
parent: User guide
nav_order: 12
---
# Slugs, finalize, accept

Each cycle has a **slug** — a kebab-case identifier naming the facet that cycle answered. Rules:

- Descriptive of the *facet*, not the topic. `stats-per-language`, not `basic-stats`.
- Unique within a worker — `accept` refuses a slug already in `.workers/<name>.json::cycles`.
- Don't repeat the worker name in the slug. `stats/per-language`, not `stats/stats-per-language`.

The cycle gating sequence has two gates — the supervisor's quality gate, then the PI's approval gate:

```mermaid
flowchart TD
  W["<b>Worker</b><br/>reaches WAITING"]
  SR["<b>Supervisor</b><br/>reads research_log.md + outputs/&lt;slug&gt;/<br/><i>compares against plan's Verification</i>"]
  D1{<b>Supervisor</b><br/>meets brief?}
  SM1["<b>Supervisor</b> → <b>Worker</b><br/>rs-worker message &lt;name&gt; '...correction...'"]
  SF["<b>Supervisor</b><br/>rs-worker finalize --slug &lt;slug&gt;<br/><i>creates staging symlink, asks PI</i>"]
  PIR["<b>PI</b><br/>reviews staging/&lt;name&gt;/ in editor"]
  D2{<b>PI</b><br/>approve?}
  SA["<b>Supervisor</b><br/>rs-worker accept --slug &lt;slug&gt;<br/><i>promotes to results/, snapshots plan, registers cycle</i>"]
  SU["<b>Supervisor</b> → <b>Worker</b><br/>rs-worker unstage + message<br/><i>worker iterates with new slug</i>"]

  W --> SR --> D1
  D1 -->|no| SM1 --> W
  D1 -->|yes| SF --> PIR --> D2
  D2 -->|yes| SA
  D2 -->|no| SU --> W
```

`accept` refuses on:
- worker not terminal (`done` or `waiting`)
- empty `outputs/<slug>/`
- `research_log.md` unchanged from skeleton (first cycle only)
- denied files present (`__pycache__`, `.ipynb_checkpoints`, `*.pyc`, `*.tmp`)
- slug already in the registry

`accept --waived "<reason>"` bypasses the shape gate; the reason is persisted in the cycle entry.
