# Delegation

## When to read this

Read this before you hand any unit of work to a subagent, and when a subagent's
result comes back. It says which model gets which unit, how to brief it, how to
check its work, and when to stop delegating and do the unit yourself. The running
skill is the orchestrator for its own run.

## Strategy

1. `models.strategy: single`: every subagent and the run itself use
   `models.single_model`. Skip the classification below.
2. `models.strategy: orchestrate` (default): classify each unit and dispatch it to
   the model for its class.

A unit is one finding, one sub-issue, one verification pass, or one search.

**Implementation is always dispatched.** Every code change goes to an implementer
subagent, even a one-line or one-character fix. This rule takes precedence over
any general operator preference about delegation size (for example "do not
delegate what is smaller than the brief"). Under `single` the subagent runs on
`models.single_model`; the only time you make a change yourself is after
`models.max_tries` (see the retry ladder).

## Classify the unit

Take the first that matches:

1. **Read-only support** (log triage, summarising a thread, sweeping the code for a
   known symbol): `models.triage`. Never code edits.
2. **Judgment-heavy**: the unit touches a path or kind listed under `## Delegation`
   in `standards.md`, or anything security-sensitive (auth, secrets, user input,
   sanitising, escaping): `models.review`.
3. **Everything else**: `models.implement`.

When unsure between 2 and 3, use 2.

## Write a self-contained brief

The subagent inherits nothing: not the issue, not the config, not your reasoning.
Every brief has:

1. **Goal**: what must be true when it is done.
2. **Files**: paths it may touch, and paths to read for context.
3. **Conventions**: the project's commit style, test framework, lint command.
4. **Scope**: "touch nothing outside these paths"; no commits, no pushes, no posts.
5. **Acceptance**: the tests or commands that must pass, and the expected result.
6. On a retry: the previous attempt's failure, quoted from your own check.

Example: "Goal: the last page of `pager.py` includes the final item when the total
is a multiple of the page size. Touch only `pager.py` and `tests/test_pager.py`. Add
a failing test first. Acceptance: the project test command passes and
`test_last_page_full` exists and passes. Do not commit."

## Audit the result yourself

1. Never read the subagent's transcript, and never trust its summary.
2. Read the diff. Check it stayed inside the scope you gave.
3. Run the tests, the lint, and the gate steps that apply yourself (see `gate.md`).
4. Check the result against the acceptance in the brief, not against "looks right".

## Retry ladder

| Try | Who |
|-----|-----|
| 1 | The agent you dispatched. |
| 2 | Resume the same agent with the exact failure from your check. |
| 3 | A fresh agent one tier up (triage to implement, implement to review). |
| after `models.max_tries` | Do the unit yourself in the main model. |

1. A try ends in a real check: tests, lint, build, or the acceptance criteria.
2. A unit already on the top tier retries on the same model with a fresh agent.
3. Doing it yourself is not a failure. Failing to finish it then is a normal
   failure (see `autonomy-and-logging.md`).

## Log every dispatch

```
DELEGATE implement(sonnet) unit="pager last page" try=1
AUDIT pass tests=48/48 lint=clean tries=1
```

One `DELEGATE` line per dispatch and one `AUDIT` line per check, with the model,
the unit, the result, and the try count.

## Never delegate the decision

The orchestrator alone decides:

1. Review verdicts: approve, request changes, or unsure.
2. What to post, where, and in what words.
3. What to refuse, defer, or park.
4. Whether a unit passed its audit.

A subagent may draft text. You read it, own it, and decide whether it goes out.

## Checklist

- [ ] Used `models.single_model` for everything when the strategy was `single`.
- [ ] Dispatched every code change to a subagent, however small.
- [ ] Classified each unit as triage, review, or implement before dispatch.
- [ ] Wrote a self-contained brief with goal, files, conventions, scope, acceptance.
- [ ] Audited by reading the diff and running the checks myself.
- [ ] Followed the retry ladder and took the unit over after `models.max_tries`.
- [ ] Logged a `DELEGATE` and an `AUDIT` line for each dispatch.
- [ ] Kept verdicts, posts, and refusals in the orchestrator.
