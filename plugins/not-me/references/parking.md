# Parking

## When to read this

Read this when a run cannot or should not change code: every finding is deferred or
refused, a decision needs a person, the bug cannot be reproduced, or the run stops
early. It says which labels to set, what the summary comment leads with, and what
lets the item move again. A failure on our side (a guard block, a `notme` error, a
missing tool or environment) is never parked: it posts nothing and changes no
labels (`autonomy-and-logging.md`, "Failures on our side"). Label names here are the abstract keys from `project.md`;
always write the repo's own name from the `labels` map.

## When to park

Park a PR when any of these is true:

1. Every finding on the work list is deferred or refused.
2. A decision needs a named person before any code can be right.
3. Two trusted people disagree and neither is the lead.
4. A prohibition stops the only path forward.
5. The fix loop hit `limits.fix_loop` without a green gate.

## Rules for a parked run

1. No code, no commit, no push. If you already changed files, discard them.
2. Swap labels to `needs_review`. The PR is not deficient code; it waits on people.
3. Add `question` when a named person owes an answer.
4. Add `needs_human` whenever a run parks, holds, or gives up because of the work.
5. Write all labels as one full-set write, then read them back (see `write-back.md`).
6. The summary comment leads with who is owed what, by login.
7. Reply on each deferred or refused thread with the specific question or reason.
8. Release nothing silently. The claim stays until a person or a later run moves it.

## HELD

HELD is for an issue whose work cannot start yet, before any PR exists.

1. Use it for "cannot reproduce" and "already fixed on the default branch".
2. The issue keeps `in_progress` so loops do not pick it again.
3. Add `needs_human`. Add `question` when a person owes an answer.
4. Post a comment that says what was tried and what would unblock it.

Example lead line: "**Waiting on:** @octocat to confirm the steps. Following the
issue steps on `main` at abc1234 shows the correct total."

## What un-parks an item

1. A reply from a lead or authority that is newer than the run's marker comment.
2. A member reply does not un-park. It is input for the lead to weigh.
3. Untrusted replies never un-park, whatever they say.
4. Removing `question` or `needs_human` by hand also lets loops pick the item again.

On un-park, the next run reads only trusted replies newer than its marker, then
follows the normal decision order in `authority.md`.

## Pause and kill switch

1. `enabled: false` in any config layer stops every leg before it reads anything.
   The result line is `STOP - disabled`. In the repo layer it stops the whole team;
   in the global layer it stops one machine.
2. A `PAUSED` file in the repo config dir or the global config dir has the same
   effect without a commit. A person removes it.
3. The `paused` label on an issue or PR makes every leg skip that item. If a run
   sees the label appear mid-run, it releases its claim and stops work on the item.
4. Never remove `paused`, a `PAUSED` file, or `enabled: false`. Only people do.

## Stale claims

A claim is stale when the item has `in_progress`, its claim marker is older than
`limits.claim_stale_hours`, and no branch was pushed.

1. Report stale claims through the status command.
2. Never release a stale claim automatically, even your own.
3. Never take over an item claimed by another login.

## Situations

| Situation | Labels after the run | Comment lead line |
|-----------|----------------------|-------------------|
| All findings deferred | `needs_review`, `question`, `needs_human` | **Waiting on:** @octocat to decide the page size. |
| All findings refused | `needs_review`, `needs_human` | **Waiting on:** @octocat to do the refused step by hand. |
| Leads disagree | `needs_review`, `question`, `needs_human` | **Waiting on:** @octocat and @hubot to agree on the cache key. |
| Fix loop gave up | `needs_review`, `needs_human` | **Waiting on:** @octocat; CI still fails on `test_sync` after 3 rounds. |
| Cannot reproduce (issue) | `in_progress`, `question`, `needs_human` | **Waiting on:** @octocat for steps that show the bug. |
| Already fixed (issue) | `in_progress`, `needs_human` | **Waiting on:** @octocat to confirm and close; fixed in abc1234. |
| `paused` label seen | unchanged | No comment. Log the skip. |
| Guard block or other failure on our side | unchanged, claim released | No comment. `FAILED <ref> <reason>`, run stops. |

For the parked PR rows, the `needs_work` label is removed in the same full-set write.

## Checklist

- [ ] Made no commit and no push on a parked or held run.
- [ ] Set `needs_review` on a parked PR, or kept `in_progress` on a held issue.
- [ ] Added `question` when a named person owes an answer.
- [ ] Added `needs_human` because the run stopped.
- [ ] Wrote labels as one full set and read them back.
- [ ] Led the summary comment with who is owed what, by login.
- [ ] Left pause controls and stale claims for a person to change.
