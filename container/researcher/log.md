---
description: Write the logbook for the PI, one entry per topic worked on since the last entry
---

The PI asks for the logbook: the story of the work since the last logbook entry, one entry per topic, for a reader who was not in the conversation. Write the entries now. Shut nothing down, and start no new work.

1. **What to cover.** Everything you worked on since the last logbook entry. `rs-memory open` names that entry (read it) and how many journal entries came after it; the journal is in `/workspace/memory/journal/` (read it, never edit it). Group the work into coherent topics: several analyses feeding one question are one topic, independent questions are one topic each. When nothing came after the last entry, tell the PI so and write nothing.

2. **One draft per topic**, in a file outside `/workspace` (for instance `/tmp/log-<topic>.md`), in exactly this shape:

   ```markdown
   # <a natural sentence reflecting the question>

   ## The question

   ## Results

   ## Open threads

   ## Where to pick up
   ```

   - **The question:** what was asked, in the PI's words or a close paraphrase, with enough context for a reader who missed the conversation.
   - **Results:** three to six sentences, with concrete numbers. Name the claims by id (F-3, D-2, Q-1). Link an accepted entry's page, relative to the entry: `[F-3](../results/F-3/F-3.md)`; an analysis's files likewise, `../analyses/<name>/<file>`. A claim still waiting for the PI is named by its id alone: its page moves when the PI decides. Never link an absolute path: the entry is filed in `/workspace/logbook/`, and every link in it is relative to it.
   - **Open threads:** what was deferred, anomalies not chased, decisions postponed. When nothing is open, say so in a sentence.
   - **Where to pick up:** the obvious first move for a fresh session that starts from this entry.

3. **File each draft:** `rs-memory log --topic <slug> --file <draft>`, the slug a few kebab-case words naming the topic (`station-gaps`). rs-memory adds the date and the journal entries the entry covers, files it read-only as `/workspace/logbook/<date>-<slug>.md`, and prints its path. When it refuses a draft, fix the draft and file it again. Never edit an entry once it is filed: a correction goes into a later entry.

4. **Tell the PI** which entries you wrote (their paths), and what is still running: jobs, analyses, checks.
