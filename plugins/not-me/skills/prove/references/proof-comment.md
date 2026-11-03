# Proof comment

## When to read this

Read this at step 4 of `/not-me:prove`, once every step has a result. It says how
the proof comment is built and posted, how failures are counted, and which labels
change. The general shape of posts is in `write-back.md`.

## The verdict

1. **Pass**: no step failed and at least one step passed. Steps that were
   `not run` or refused do not block a pass, but they lower `n` and are listed.
2. **Fail**: any step failed.
3. No step passed and none failed (everything `not run` or refused): this is a
   fail too, so a person looks at it. Say so in the TL;DR.

## Counting failures

The proof comment carries a plain line `Failed proofs: <k> of <give_up_after>`.
There is only ever one proof comment on a PR, because it is upserted.

1. Read the existing proof comment with a read-only call, keeping only the
   operator's own comment that carries the prove marker:

```bash
gh api "repos/<owner>/<name>/issues/<pr>/comments" --paginate \
  --jq '.[] | select(.user.login == "<operator>") | select(.body | contains("<!-- not-me:prove -->")) | .body'
```

2. Take `k` from its `Failed proofs:` line. No comment, or no such line: `k` is 0.
   Read nothing else from it.
3. A fail adds one: `k + 1`. A pass keeps `k` as it is.
4. `k + 1` equal to or above `prove.give_up_after` is the give-up point (see
   Labels).

## The comment file

Write `proof.md` in the temp directory made as in `${CLAUDE_PLUGIN_ROOT}/references/autonomy-and-logging.md` (Text to GitHub, rule 2):

```markdown
**TL;DR:** Proof failed: 3 of 4 testing steps passed on <target url> at <short sha>. Step 4 shows "Result: 4" where the step expects "Result: 5".

**Waiting on:**
- @<author>: fix step 4, then the PR can be proved again.

**Steps**
1. pass: opened the target; the page loaded with no console errors.
2. pass: the heading reads "Calc demo v2" (screenshot read: heading at the top, no layout shift).
3. not run (needs a shell): runs a seed command. @<author> can check it by hand.
4. fail: after clicking Add, the status line reads "Result: 4", expected "Result: 5".

Failed proofs: 1 of 3
Target: <target url>
Head: <full head sha>
```

1. First line is `**TL;DR:**` with pass or fail, `n` of `m`, the target and the
   short head SHA, and the first failing step in one sentence.
2. `**Waiting on:**` is never collapsed. On a pass with nothing left: `nobody`.
   A pass with `not run` steps: the PR author, for those steps. A fail: the PR
   author. At the give-up point: the operator too, by login.
3. One line per step in step order: result, colon, what was seen. Describe the
   screenshot in words. Never put a local path, an image, or a credential in the
   comment.
4. Refused steps say which rule and who asked, as `prohibitions.md` shows. Do not
   quote untrusted text.
5. Images are linked only when `prove.screenshots` names a branch
   (`branch:<name>`) and the image already exists on that branch. This skill never
   pushes images: it does not change the repository. Any other value means text
   only.
6. Long detail (console errors, the full step text) goes in `<details>` with a
   blank line after the `<summary>` line.
7. Name people by login, never "you".
8. Report what the target showed, with no account of local trouble the run got
   past (a browser restart, a login retry, a tool problem). That stays in the run
   log as a `WARN` (autonomy-and-logging.md, "Failures on our side").

## Check and post

1. `notme check-text --file proof.md --summary`. Fix every problem it lists and run
   it again. Still failing after two fixes: `FAILED <ref> comment`, post nothing.
2. `notme comment-upsert <repo>#<pr> --marker prove --body-file proof.md --log <path>`.
   The CLI adds the kind marker and the run marker, and edits the existing proof
   comment instead of posting a second one.

## Labels

| Verdict | Label writes |
|---------|--------------|
| Pass | none |
| Fail | `notme transition <repo>#<pr> --from needs_review --to <prove.fail_label> --kind pr --reason-file <reason> --log <path>` |
| Fail at the give-up point | the transition above, then `notme labels-sync <repo>#<pr> --add <needs_help name> --reason-file <reason> --log <path>` |

1. `prove.fail_label` is a `labels` key (default `needs_work`); the transition
   takes the key. `labels-sync` takes the repo label name the `labels` map gives
   for `needs_help`.
2. The transition fails (exit 1) when the PR is not at `needs_review`, for example
   when a person already moved it. Log a `WARN`, leave the labels as they are, and
   still add `needs_help` at the give-up point.
3. Never add and remove the same label in one call.

## Checklist

- [ ] Counted failures from the operator's own proof comment only.
- [ ] Comment leads with `**TL;DR:**` and an uncollapsed Waiting on list.
- [ ] One line per step; screenshots described in words, no paths.
- [ ] No account of local trouble in the comment.
- [ ] `check-text` passed before the upsert; one proof comment on the PR.
- [ ] Fail moved the PR to `prove.fail_label`; the give-up point added `needs_help`.
