# Benchmarks

Re-runnable measurements that spend real model tokens and run on demand, never
as part of the normal checks. Each folder is one benchmark.

## researcher

Does a research agent keep what it learned across sessions? The same scripted
sessions are played to several project types, then a cold-started agent is
asked probes and an independent grader scores each answer against a key.

- `generate.py` writes a synthetic survey panel from a seed, its effects
  planted first, and the answer key from the same seed (ground truth first:
  the numbers a probe asks about are computed from the drawn rows with the
  analysis the PI asks for, so the key is what a correct analysis finds).
- `script.json` is what the PI says, session by session: a result established
  in session one and not mentioned again, an approach that fails for a
  discoverable reason, a session-one assumption the PI overturns in session
  three (and a belief derived from it), a session killed mid-task, a procedure
  needed twice.
- `probes.json` asks after the last session: recall (as worded and
  paraphrased), negative knowledge, provenance, the correction and the derived
  belief, the artefact, the interruption.
- `bench.py` creates one project per arm (`research`, `researcher` with Claude
  and with pi, `researcher` without its start hook, and a plain `sandbox-dind`
  project as the floor), copies the data in as `/workspace/data`, plays the
  sessions in the agent's own terminal (a turn ends when RS's activity counts
  hand back), asks the probes, grades them, and writes
  `results/<run>/results.json` and `table.md`: the scores per probe and arm,
  the tokens of every session and what each cold start loaded.
- `standin.py` is a stand-in model server: `--standin` runs the whole
  pipeline against it, zero tokens, to check the plumbing (scores mean nothing).

Numeric answers are graded in code against the key; prose answers by a
grader run that has no tools, no memory and only the key, the question and the
answer. Before a real run it grades a hand-written right and wrong answer; a
run whose grader gets either wrong stops there. Paths a provenance answer names
must be files the sessions wrote. Each probe starts from the same memory.

### Running it

From the repository root of an RS whose stack is up, with the Claude and pi
downloads and the reader pulled:

    python3 tests/bench/researcher/bench.py --run dry --seed 7 --standin

A real run signs in with a Claude token made once with `claude setup-token`
and, for the pi arm, a provider API key, each in a file under `/workspace`:

    python3 tests/bench/researcher/bench.py --run r1 --seed 7 \
        --claude-token-file /workspace/.bench/claude-token \
        --pi-model anthropic/claude-sonnet-4-5 --pi-key-file /workspace/.bench/pi-key

Every run spends the account's quota: five arms × five sessions, plus eight
probes and their grading per arm.
