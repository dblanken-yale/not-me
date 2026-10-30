# Failure handling

## When to read this

Read this whenever `/not-me:review` cannot finish a PR or the run. It says what is
written, what is left alone, and the result for that PR. Logging rules are in
`autonomy-and-logging.md`; guard rules in `prohibitions.md`.

## Rules for every row

1. A `FAILED` row is a failure on our side (`autonomy-and-logging.md`, "Failures
   on our side"): its cause is outside the PR, so the author cannot fix it. It
   stops the run. Finish that PR's log, remove its worktree, and do not go on to
   the next PR: the same cause will hit it too.
2. Leave the labels of a failed PR as they were. Post nothing: no review, no
   comment, no partial review.
3. Finish the log with `notme log finish <path> --status "<result>"`, passing the
   failing command and output with `--failures-file`.
4. Remove the worktree with `notme git worktree-remove <worktree path>`, the
   `path` that `worktree-add` returned, on every path.

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
| Fetch or worktree-add fails | unchanged | none | `FAILED <ref> worktree`; run stops |
| A check fails because of the change | per verdict | the review | request changes |
| A check cannot start, or fails the same way on the base | unchanged | none | `FAILED <ref> check - <name>`; run stops |
| Subagent dispatch fails | unchanged | none | review it yourself; log `WARN` |
| Guard block | unchanged | none | `FAILED <ref> blocked - <target>`; run stops |
| `check-text` keeps failing | unchanged | none | `FAILED <ref> text-check`; run stops |
| `review-post` fails or reads back wrong | unchanged | none | `FAILED <ref> post`; run stops |
| Any other `notme` error on a PR | unchanged | none | `FAILED <ref> <command>`; run stops |
| `resolve-threads` fails on the fixed threads | as posted | the review stands | `REVIEWED <ref>` with a `WARN` |

## Guard blocks

1. A block is final. Do not retry with another command shape, tool, or wrapper.
2. Log a QUESTION naming the operator: "@<operator>: <action> on <target> was
   blocked during review; allow it if intended." Log the block message as a `WARN`.
3. Post nothing and change no labels. End `FAILED <ref> blocked - <target>` and
   stop the run.

## Never

1. Approve when a check did not run or did not pass.
2. Merge, or post a second review on the same head to repair a failed one.
3. Leave a worktree behind or a log unfinished.

## Checklist

- [ ] Stopped the run on a failure on our side, without going to the next PR.
- [ ] Left labels alone on a failed PR and posted nothing on it.
- [ ] Logged guard blocks as a QUESTION naming the operator and a `WARN`.
- [ ] Removed every worktree and finished every log.
