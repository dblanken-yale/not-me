# Failure handling

## When to read this

Read this whenever `/not-me:update` cannot finish: a stop before work, a park, a
guard block, or a failure. It says which labels to write, whether to comment, and
the result line. Parking rules are in `parking.md`; logging rules in
`autonomy-and-logging.md`.

## Rules for every row

1. Write labels with `notme transition ... --reason-file <reason>` (one line on why) or `notme labels-sync <ref> --add <names>
   --reason-file <reason> --log <path>`. Never add and remove the same label in one call.
2. Comments go through `notme check-text --file <f>`, then
   `notme comment-upsert <ref> --marker update --body-file <f> --log <path>`. The
   body leads with `**Waiting on:**` and a login, never "you".
3. Finish the log with `notme log finish <path> --status "<result>"`. Pass the
   failing commands and their output with `--failures-file`.
4. Return the checkout to the branch it started on and restore any stash.

## Table

| Situation | Labels after | Comment | Result line |
|-----------|--------------|---------|-------------|
| Nothing to pick | unchanged | none | `NONE - idle` |
| `enabled` false | unchanged | none | `STOP - disabled` |
| `PAUSED` file | unchanged | none | `STOP - paused` |
| Another run active | unchanged | none | `STOP - active run` |
| Guard selftest exit 8 | unchanged | none | `FAILED - guard not working` |
| Config exit 4 | unchanged | none | `FAILED - config invalid` |
| Explicit PR claimed elsewhere (exit 3) | unchanged | none | `STOP <ref> claimed elsewhere` |
| PR author untrusted, or a fork | unchanged, claim released | none | `STOP <ref> untrusted-author` or `STOP <ref> fork` |
| Head branch is protected | unchanged, claim released | none | `STOP <ref> protected-head` |
| Every finding deferred or refused | `findings.md` park labels | summary and thread replies | `PARKED <ref> needs_review - waiting on @<login>` |
| Guard block, other work can go on | outcome labels plus `needs_human` | names the operator and the blocked target | the normal result, note `blocked <target>` |
| Guard block that stops the item | `needs_review`, `needs_human` | names the operator and the blocked target | `FAILED <ref> blocked - <target>` |
| Merge conflict at Checkpoint A | `needs_work`, `needs_human`, claim released | names the conflicting files | `FAILED <ref> merge-conflict` |
| Implementation or gate fails after `models.max_tries` on every finding | `needs_work`, `needs_human`, claim released | names the failing check | `FAILED <ref> <check>` |
| Push fails after commit | unchanged | none | `FAILED <ref> not-pushed` (log says "committed but NOT PUSHED") |
| CI red at `limits.fix_loop` | still needs work: `needs_work` stays, add `needs_human` | summary naming the failing job and test | `FAILED <ref> ci-red - <test>` |
| CI wait times out | outcome labels | summary says CI is pending | the normal result, note `ci pending`, with a `WARN` |

"Release" means `notme release-pr <ref> --log <path>`, then the `needs_human` add.
A mechanical failure (a command that errors, a checkout that will not switch)
before any push is released this way and ends `FAILED <ref> <what>`.

## Guard blocks

1. A block is final. Do not retry with another command shape, tool, or wrapper.
2. Log `notme log question` naming the operator: "@<operator>: push to <repo> was
   blocked; allow it with the allow-repo command if intended."
3. Continue with any part of the work that does not need the blocked action.
   When nothing is left, stop with the row above.

## Never

1. Fake green: skip a test, disable a check, or claim "passes locally" without the
   run.
2. Release a claim silently: every release is logged.
3. Leave the run without a finished log and a result line.

## Checklist

- [ ] Wrote the labels in the row, and `needs_human` whenever a person must act.
- [ ] Posted at most one summary, upserted with the `update` marker.
- [ ] Finished the log with the failing command and output.
- [ ] Printed exactly the result line in the row.
