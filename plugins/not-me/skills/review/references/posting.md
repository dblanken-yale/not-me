# Posting the review

## When to read this

Read this at step 6 of `/not-me:review`. It says how the body and the inline
comments are built, which command posts them, how labels are written, and which
earlier fixed threads get resolved. The general shape of every post is in
`write-back.md`.

## Files

Make one work directory in the temp directory made as in `${CLAUDE_PLUGIN_ROOT}/references/autonomy-and-logging.md` (Text to GitHub, rule 2) and write:

1. `body.md`, in this order:
   - `<!-- not-me:review -->` on the first line. The CLI also adds the run marker.
   - `**TL;DR:**` and one or two sentences: the verdict and the reason.
   - `**Waiting on:**` one line per login with what they owe, or "nobody".
   - `**Findings**`: one line per finding, level first, file and line, one
     sentence. Inline ones are summarised here too.
   - `**Earlier threads**`: fixed, answered, or still open, one line each.
   - `**Checks**`: each command run on the head SHA and its result.
   - `**Follow-ups**`: each follow-up issue as `owner/repo#N`, non-blocking.
   - Long output in `<details>` with a blank line after the `<summary>` line.
2. `comments.json`: a JSON list of `{"path", "line", "body"}`, one per finding
   whose line is on the right side of a diff hunk. Each body starts with the level
   and says what is wrong and what would fix it.
3. `comments.md`: every comment body, one after another, for the text check.

The labels are not written by hand: `--labels-auto` lets the CLI work them out.

Name people by login, never "you". No AI or model mention anywhere.

## Check the text

Run `notme check-text --file body.md` and `notme check-text --file comments.md`.
Fix every problem it lists and run it again. Never post on a failing check.

## Post

One command posts the review and sets the labels:

```
notme review-post <ref> --event <APPROVE|REQUEST_CHANGES|COMMENT> \
  --body-file body.md --comments-file comments.json \
  --labels-auto --log <path>
```

When the labels change a state label, the CLI posts one comment after the review naming
   what was added and removed and why ("a <EVENT> review was posted"); add `--reason-file <f>`
   to give a more specific one-line reason.

1. APPROVE only when `review.auto_approve` is true; otherwise COMMENT with no
   `--labels-auto` (see `verdict.md`).
2. Leave out `--comments-file` when the list is empty.
3. With `--labels-auto` the CLI reads the PR's reviews after posting and sets the
   full label set from them, role.md and the roster: the approve label once every
   required reviewer approved, else `needs_review` plus the pass label of each
   approving role (see `${CLAUDE_PLUGIN_ROOT}/defaults/role.md`). The output's
   `approval` says who is still `waiting`; a warning names a pass label missing
   from the repo, which is skipped.
4. The CLI reads the review and the labels back. Any nonzero exit is a mechanical
   failure (see `failures.md`). Do not post a second review to fix it.

## Resolve fixed threads

After the review is posted, collect the `thread_id` of each earlier thread that
the operator started and that you judged fixed on this head. Write them as a JSON
list to `fixed-threads.json` and run:

```
notme resolve-threads <ref> --ids-file fixed-threads.json --log <path>
```

Never include a thread judged answered or open, or one from another reviewer: only
the fixed threads get resolved. Skip the call when the list is empty.

## Example body

```markdown
<!-- not-me:review -->
**TL;DR:** Changes requested: `mul` squares its first argument instead of
multiplying, so acme/app#12's criterion "mul(2, 3) is 6" fails.

**Waiting on:**
- @octocat: fix `calc/core.py:16`.

**Findings**
- blocker `calc/core.py:16`: returns `a * a`; `mul(2, 3)` gives 4.

**Checks**
- Tests on abc1234: 1 failed (`test_mul`).
```

## Checklist

- [ ] Body leads with the marker and `**TL;DR:**`, people named by login.
- [ ] Every inline comment has a path and a line in the diff.
- [ ] Both text files passed `notme check-text`.
- [ ] Posted one review with `--labels-auto`, read back by the CLI.
- [ ] Resolved only the operator's earlier threads judged fixed.
