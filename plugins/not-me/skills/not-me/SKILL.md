---
name: not-me
description: "Router: pick the most valuable work on its own by running the configured legs in order (default update, then new) and stop at the first leg that does something. Use when the operator runs /not-me with no arguments, with --dry-run or --bg, with --parallel N to start several new-issue runs at once, or on a schedule such as /loop 2h /not-me --bg. NOT for a known PR (use /not-me:update), NOT for a known issue (use /not-me:new), NOT for reviewing a PR (use /not-me:review)."
argument-hint: "[--dry-run] [--bg] [--parallel N]"
---

# /not-me

Thin router. It does no work itself and posts nothing (it owns no marker, so
`<!-- not-me:not-me -->` never appears on GitHub). One unit of work per run,
except with `--parallel N` (Parallel runs, below).

Arguments: `$ARGUMENTS`. Only `--dry-run`, `--bg` and `--parallel N` are accepted.

## Ground rules

- `${CLAUDE_PLUGIN_ROOT}` below is substituted at load time with this plugin's
  directory. Read the files it names with the Read tool. It is not a shell variable.
- Call the CLI as `notme`. If the shell cannot find it, call
  `"${CLAUDE_PLUGIN_ROOT}/bin/notme"`. Every call prints one JSON object.
- A bare number in the arguments is ambiguous: refuse it and say which skill to
  use, `/not-me:update <pr>` for a PR or `/not-me:new <issue>` for an issue.

## Step 1: Gate

1. `notme status`. Read `enabled`, `paused` and `active_run`.
2. `enabled` false: print `STOP - disabled` and stop.
3. Either `paused` flag true: print `STOP - paused` and stop.
4. An `active_run` that started within `limits.claim_stale_hours`: print
   `STOP - run in progress` and stop. Never force it.

## Step 2: Choose the legs

1. `notme config` and read `router`. Default `[update, new]`.
2. Legs are `update`, `new` and `review`. An unknown name is skipped with a note.

## Step 3: Run the legs in order

For each leg, start `/not-me:<leg>` with the Skill tool and forward `$ARGUMENTS`
unchanged.

1. With `--bg`, read `${CLAUDE_PLUGIN_ROOT}/references/bg-mode.md`. Each leg runs
   as its own foreground subagent and returns one result line. Print that line
   exactly as returned.
2. Without `--bg`, the leg runs in this session and ends with its own result line.
3. The line starts with a VERB:
   `NONE | PR | PUSHED | PARKED | HELD | REVIEWED | PROVED | FAILED | STOP`.
4. Stop at the first result whose VERB is not `NONE`. A `FAILED` or `STOP` leg also
   ends the run. Later legs never run.
5. Every leg returned `NONE`: print `NONE - nothing to do` and finish. That is a
   success, not a failure.

## Parallel runs

`--parallel N` starts up to N `/not-me:new` runs at once, one issue each, each in
its own worktree. After Step 1 it runs instead of Steps 2 to 4.

1. Refuse it with `--bg` (a `--bg` result is one line) and when N is not a whole
   number of 1 or more. Lower N to `limits.parallel_runs` (default 3) with a note.
2. `workspace.new` must be `worktree` in `notme config`; otherwise every run would
   switch the one main checkout. Print `STOP - parallel needs workspace.new: worktree`.
3. `notme pick-issue`. `{"none": true}`: print `NONE - nothing to do`. Otherwise
   choose up to N distinct issues from `issues` by the `picking` rules, as
   `/not-me:new` would; never a `held_back` one. Fewer ready issues: fewer runs.
4. Dispatch one subagent per issue, all in one message so they run together.
   Start each in this checkout without worktree isolation (the session harness
   refuses `notme git` calls from an isolated worktree) and have it run
   `/not-me:new <issue>`, plus `--dry-run` when given, and return its result line.
5. Each run claims its own issue (compare-and-set), so two runs never take one
   issue: a lost claim ends that run with `STOP <ref> claimed elsewhere` and the
   others go on. Never retry a run or reuse an issue another run took.
6. Print each run's result line exactly, in issue order. Skip the retro;
   `/not-me:retro <log>` on each log afterwards is the operator's call.

## Step 4: Retro

After a leg that did work (any VERB other than `NONE`, `STOP`), run `/not-me:retro`
when available. If that skill is not installed, skip it silently.

## Checklist

- [ ] Refused a bare number and named the right skill.
- [ ] Checked `notme status` before any leg: disabled, paused, run in progress.
- [ ] Read `router` from the config and ran only those legs, in order.
- [ ] Forwarded `$ARGUMENTS`; under `--bg` each leg was its own subagent.
- [ ] Stopped at the first non-`NONE` result and printed it exactly.
- [ ] Printed `NONE - nothing to do` only when every leg was `NONE`.
- [ ] `--parallel N`: capped at `limits.parallel_runs`, needed `workspace.new: worktree`,
  one unisolated subagent per distinct issue, printed every run's line.
