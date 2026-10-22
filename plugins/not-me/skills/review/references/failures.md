# Failure handling

## When to read this

Read this whenever `/not-me:review` cannot finish a PR or the run. It says what is
written, what is left alone, and the result for that PR. Logging rules are in
`autonomy-and-logging.md`; guard rules in `prohibitions.md`.

## Rules for every row

1. A failure on one PR never stops the queue. Finish that PR's log, remove its
   worktree, and go to the next PR.
2. Leave the labels of a failed PR as they were. Never post a partial review.
3. Finish the log with `notme log finish <path> --status "<result>"`, passing the
   failing command and output with `--failures-file`.
4. Remove the worktree with `notme git worktree-remove <path>` on every path.

## Table

| Situation | Labels | Post | Result for the PR or run |
|-----------|--------|------|--------------------------|
| `pick-reviews` returns none | unchanged | none | `NONE - idle` |
| `enabled` false | unchanged | none | `STOP - disabled` |
| `PAUSED` file | unchanged | none | `STOP - paused` |
| Another run active | unchanged | none | `STOP - active run` |
| Guard selftest exit 8 | unchanged | none | `FAILED - guard not working` |
| Config exit 4 | unchanged | none | `FAILED - config invalid` |
| PR closed, draft, `paused`, author not on the roster, or own | unchanged | none | skipped, logged |
| Fetch or worktree-add fails | unchanged | none | `FAILED <ref> worktree` |
| A check fails because of the change | per verdict | the review | request changes |
| A check cannot run | per verdict | the review or none | a finding; unsure when the cause is outside the PR |
| Subagent dispatch fails | unchanged | none | review it yourself; log `WARN` |
| Guard block | `question` added | none | asked; QUESTION naming the operator |
| `check-text` keeps failing | unchanged | none | `FAILED <ref> text-check` |
| `review-post` fails or reads back wrong | unchanged | none | `FAILED <ref> post` |
| `resolve-threads` fails on the fixed threads | as posted | the review stands | `REVIEWED <ref>` with a `WARN` |
| Every PR in the queue failed | per row | per row | `FAILED - <first reason>` |

## Guard blocks

1. A block is final. Do not retry with another command shape, tool, or wrapper.
2. Log a QUESTION naming the operator: "@<operator>: <action> on <target> was
   blocked during review; allow it if intended."
3. Post nothing for that PR. Continue with the next PR.

## Never

1. Approve when a check did not run or did not pass.
2. Merge, or post a second review on the same head to repair a failed one.
3. Leave a worktree behind or a log unfinished.

## Checklist

- [ ] Kept going with the next PR after a failure on one.
- [ ] Left labels alone on a failed PR and posted nothing partial.
- [ ] Logged guard blocks as a QUESTION naming the operator.
- [ ] Removed every worktree and finished every log.
