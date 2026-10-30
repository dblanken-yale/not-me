# Background mode

## When to read this

Read this when a skill is invoked with `--bg`, and when you are the subagent such a
run dispatched. It fixes how the work is handed to a subagent, what that subagent
may and may not do, and the one line it returns. The meaning of each result verb is
also in `write-back.md`; this file owns the contract around it.

## The parent's job

1. Strip `--bg` from the arguments. Keep every other argument as given.
2. Dispatch one foreground subagent on `models.bg`. Pass it the full skill text and
   the remaining arguments. Wait for it.
3. Run it in place, in the current checkout. Use a worktree only when the skill
   itself says to (for example `workspace` is `worktree` for that work kind).
4. Print the subagent's result line exactly as returned. Then stop. No summary, no
   follow-up work, no second dispatch.

## The subagent's job

1. Run the skill from step 0 as written, with the arguments given.
2. `AskUserQuestion` is not available. There is no question window. For every
   choice, take the safe default and log QUESTION, DECISION, UNDO (see
   `autonomy-and-logging.md`).
3. Run every subagent you dispatch (implementers, reviewers, fix rounds) in the
   foreground as `delegation.md` says. Never resume one with `SendMessage`.
4. Do the work, finish the log, and return exactly one line as the final message.
   Nothing before it, nothing after it.

## The result line

```
<VERB> <ref|-> <status>[ - note]  log=<path>
```

1. `<ref>` is `owner/repo#N`, or `-` when there is no item.
2. The separator before the note is a plain hyphen with one space on each side.
3. Two spaces before `log=`. The path is the run log the CLI created.
4. VERB is one of: NONE, PR, PUSHED, PARKED, HELD, REVIEWED, PROVED, FAILED, STOP.
5. NONE means only "nothing to do". A run that tried and went wrong is FAILED. A
   run stopped by the kill switch, a pause, or a failed guard selftest is STOP.
   A failure on our side (`autonomy-and-logging.md`) is `FAILED <ref> <short
   reason>`, with the exact error in the log and nothing posted.
6. In `mode: dry-run`, say so in the note: `PR acme/app#88 opened - dry-run`.

Examples:

```
NONE - idle  log=<path>
PR acme/app#88 opened  log=<path>
PUSHED acme/app#57 needs_review - 2 of 3 findings fixed  log=<path>
PARKED acme/app#57 needs_review - waiting on @octocat  log=<path>
HELD acme/app#90 cannot-reproduce  log=<path>
FAILED acme/app#88 ci-red - test_last_page_full after 3 rounds  log=<path>
STOP - paused  log=<path>
```

## The router

The `/not-me` router runs its legs in `router` order, each as its own run.

1. The first leg that returns anything other than NONE wins. Print its line and
   stop; later legs do not run.
2. Every leg NONE is a success, and the router's result is NONE.
3. A FAILED or STOP leg also ends the router. Do not try the next leg.
4. One unit of work per router run.

## Spawn depth

With `models.strategy` on `orchestrate`, a `--bg` run nests three deep: the parent,
the skill subagent, and its implementer subagents.

1. That needs `CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH` of at least 3. The doctor
   skill checks it.
2. If a dispatch fails for depth, do the unit in the current model and log a `WARN`
   naming the setting. Do not fail the run for it.

## Scheduling

The typical schedule is a loop in a Claude Code session:

```
/loop 2h /not-me --bg
```

Each tick is one router run and prints one line. Do not start or extend a loop from
inside a run.

## Checklist

- [ ] Stripped `--bg` and dispatched one foreground subagent on `models.bg`.
- [ ] Ran in place unless the skill asked for a worktree.
- [ ] Asked no questions; logged QUESTION, DECISION, UNDO for each default taken.
- [ ] Returned exactly one result line in the grammar above.
- [ ] Used NONE only for "nothing to do" and FAILED for a run that went wrong.
- [ ] The parent printed the line and stopped.
