# Evals

Short evals of research agents, run on demand to catch regressions and to
compare project types and workflows. They spend real model tokens, so they
are never part of the normal checks. Each eval asks one question (is a result
remembered? is a correction kept? is an interrupted task resumed?) and takes a
few minutes per project type.

## What an eval is

One file under `evals/`: a few sessions of what the PI says, then probes (a
question asked of a cold-started agent) or checks (the workspace looked at,
no question asked), and how each is scored:

| Check | Scores | Costs |
|---|---|---|
| `number` | a number in the answer within a tolerance of the key's, written right next to its unit (`unit`, a regular expression; the question asks for the unit) | nothing |
| `contains` | the answer holds one of the given phrases, and none of the others | nothing |
| `files` | the files the eval names are there, or each holds its value from the key | nothing |
| `grader` | a separate run, with no tools and no memory, scores the answer against the key | one model call |

A new regression check or workflow comparison is a new file, not new code.
The data is a small synthetic survey panel written by `generate.py` from a
seed, with the key computed from the same rows, so the key is what a correct
analysis finds.

| Eval | Asks | Sessions | Probes |
|---|---|---|---|
| `recall` | a number found in one session, asked for in the next | 1 | 1 |
| `correction` | a fact only the PI knows (weeks worked a year), corrected, then a value that depends on it | 2 | 2 |
| `interruption` | a task killed half-way, finished in the next session (each wave's file checked against its mean) | 2 | files check |
| `full` (optional) | the long study: five sessions, eight probes, prose graded | 5 | 8 |

`full` runs only when named (`--evals full`). On five project types it takes
hours and a large share of the quota.

## Project types

Each eval runs on each chosen type, each in its own fresh project, destroyed
when the eval is done. By default: `researcher` and `floor` (a plain
`sandbox-dind` project). Opt in with `--arms`: `researcher-pi`,
`researcher-nohook` (no start hook), `research` (the research flow, with its
`/log` at each session's end; the scripted PI approves every plan and staged cycle it asks about).
`researcher-subagents` runs the researcher-subagents type with Claude; each referee run's tokens are read from its verdict file and shown apart in the table, as part of the eval's tokens.

## Running

From the repository root of an RS whose stack is up, with the Claude and pi
downloads and the reader pulled. See what a run will do first; it creates
nothing:

    python3 tests/bench/bench.py --run r1 --dry-run

A real run signs Claude in with a token made once with `claude setup-token`
(`--claude-token-file`) or with an Anthropic API key (`--claude-key-file`; not
for the `research` type, whose workers need the token), and pi with a provider
API key, each in a file under `/workspace`:

    python3 tests/bench/bench.py --run r1 --claude-token-file /workspace/.bench/claude-token
    python3 tests/bench/bench.py --run r2 --evals recall --arms researcher researcher-pi \
        --claude-token-file /workspace/.bench/claude-token \
        --pi-model anthropic/claude-sonnet-4-5 --pi-key-file /workspace/.bench/pi-key

Each eval on each project type has a token cap and a time cap
(`--max-tokens`, `--max-seconds`), grading included; past either it stops
and the table says "OVER BUDGET". The caps are checked after each step, so a
single step can overrun them by up to one turn's timeout. `--standin` runs everything against a stand-in model server
(`standin.py`) at zero tokens, to check the plumbing; its scores mean nothing.

The results go to `results/<run>/` (`results.json`, `table.md`): each probe's
score with its tokens and time, and each eval's total tokens, time and cold
start size per project type. A probe may recompute an answer instead of
remembering it (probes keep their tools and the data); its tokens and time
tell the two apart. An interruption check whose kill came after the task had
finished scores nothing: there was nothing to resume. A turn the model never
answered (an API refusal, a sign-in problem) stops that eval, and the table
marks it FAILED with the error the agent logged. So does a session turn that
does not finish within `--turn-timeout`: a hung session is shown, not scored
around, and the cost shown for it stops at the timeout.
