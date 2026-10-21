---
name: not-me
description: "Router: pick the most valuable work on its own by running the configured legs in order (default update, then new) and stop at the first leg that does something. Use when the operator runs /not-me with no arguments, with --dry-run or --bg, or on a schedule such as /loop 2h /not-me --bg. NOT for a known PR (use /not-me:update), NOT for a known issue (use /not-me:new), NOT for reviewing a PR (use /not-me:review)."
argument-hint: "[--dry-run] [--bg]"
---

# /not-me

Thin router. It does no work itself and posts nothing (it owns no marker, so
`<!-- not-me:not-me -->` never appears on GitHub). One unit of work per run.

Arguments: `$ARGUMENTS`. Only `--dry-run` and `--bg` are accepted.

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
