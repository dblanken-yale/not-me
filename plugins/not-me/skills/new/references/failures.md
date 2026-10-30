# Failure handling

## When to read this

Read this whenever `/not-me:new` cannot finish: a stop before work, a hold, a guard
block, or a failure. It says which labels to write, whether to comment, and the
result line. Parking rules are in `parking.md`; logging rules in
`autonomy-and-logging.md`. A failure on our side (its cause is outside the issue:
a `notme` error, a fetch or checkout failure, a missing tool or environment, a
guard block, a dispatch with no fallback) follows "Failures on our side" there:
release the claim, post nothing, add no label, and stop the run.

## Rules for every row

1. Write labels with `notme labels-sync N --add <names> --reason-file <reason> --log <path>` (a reason file is
   required when a state label changes, `needs_human` included) or the state commands. Never add and remove the same label in one call.
2. Comments go through `notme check-text --file <f>`, then
   `notme comment-upsert <tracker>#N --marker new --body-file <f> --log <path>`.
   The body leads with `**Waiting on:**` and a login, never "you".
3. Finish the log with `notme log finish <path> --status "<result>"`. Pass the
   failing commands and their output with `--failures-file`.
4. Return the checkout to the branch it started on and restore any stash.

## Table

| Situation | Labels after | Comment | Result line |
|-----------|--------------|---------|-------------|
| `pick-issue` returns none | unchanged | none | `NONE - idle` |
| `enabled` false | unchanged | none | `STOP - disabled` |
| `PAUSED` file | unchanged | none | `STOP - paused` |
| Another run active | unchanged | none | `STOP - active run` |
| Guard selftest exit 8 | unchanged | none | `FAILED - guard not working` |
| Config exit 4 | unchanged | none | `FAILED - config invalid` |
| Untrusted author, explicit number | unchanged | none | `HELD <ref> untrusted-author` |
| Prohibited request | pick label, `needs_human` | refusal naming the rule and requester | `STOP <ref> refused - <rule>` |
| Explicit issue claimed elsewhere (exit 3) | unchanged | none | `STOP <ref> claimed elsewhere` |
| Declared base missing | `todo`, `needs_human` | names the base, where it was declared, the issue author | `STOP <ref> blocked - base <b> not found` |
| Cannot reproduce | `in_progress`, `question`, `needs_human` | what was run, on which SHA, what was seen | `HELD <ref> cannot-reproduce` |
| Already fixed on the default branch | `in_progress`, `needs_human` | the commit that fixed it | `HELD <ref> already-fixed` |
| Guard block | released to `todo` | none | `FAILED <ref> blocked - <target>`; run stops |
| Other failure on our side before a push | released to `todo` | none | `FAILED <ref> <what>`; run stops |
| Merge conflict at Checkpoint A | release to `todo`, `needs_human` | names the conflicting files | `FAILED <ref> merge-conflict` |
| A check or CI job fails the same way on the base (`verification.md`, "A failure that is also on the base") | released to `todo` before a push; after a push, `in_progress` stays | none | `FAILED <ref> base-red - <check>`; run stops |
| Implementation or gate fails after `models.max_tries` | release to `todo`, `needs_human` | names the failing check | `FAILED <ref> <check>` |
| Push fails after commit | `in_progress` | none | `FAILED <ref> not-pushed` (log says "committed but NOT PUSHED") |
| PR open fails after push | `in_progress` (the branch is pushed) | none; the log names the branch | `FAILED <ref> pr-open`; run stops |
| CI red at `limits.fix_loop` | issue `needs_review`; PR adds `needs_human` | names the failing job and test | `FAILED <ref> ci-red - <test>` |
| CI wait times out | issue `needs_review` | none | `PR <ref> opened - ci pending` with a `WARN` |

"Release" means `notme release-issue N --reason-file <reason> --log <path>` (the reason file holds one line on why the run gave up), then the `needs_human` add.
On a failure on our side, "released" means `notme release-issue N --no-comment
--log <path>` alone: no reason file, no comment, no `needs_human`.
For HELD, the issue keeps `in_progress` so loops skip it; add the extra labels
with `labels-sync` and do not call `release-issue`.

## Guard blocks

1. A block is final. Do not retry with another command shape, tool, or wrapper.
2. Log `notme log question` naming the operator: "@<operator>: push to <repo> was
   blocked; allow it with the allow-repo command if intended." Log the block
   message as a `WARN`.
3. A block is a failure on our side: release the claim, post nothing, add no
   label, end `FAILED <ref> blocked - <target>` and stop the run.

## Never

1. Fake green: skip a test, disable a check, or claim "passes locally" without the
   run.
2. Release a claim silently: every release is logged.
3. Leave the run without a finished log and a result line.

## Checklist

- [ ] Wrote the labels in the row, and `needs_human` whenever the run stopped short because of the work.
- [ ] On a failure on our side: released the claim, posted nothing, added no label, stopped.
- [ ] Posted at most one comment, upserted with the `new` marker.
- [ ] Finished the log with the failing command and output.
- [ ] Printed exactly the result line in the row.
