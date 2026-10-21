# Write-back

## When to read this

Read this before anything is posted to GitHub: a summary comment, a thread reply, a
PR body, a commit message, a label change, a review request, or the final result
line of a `--bg` run. It fixes the shape of every post so that reruns update instead
of duplicate and readers find the point first. All posts go through the not-me CLI,
with text passed in a file, never as a shell argument.

## Markers and upsert

1. Every comment not-me owns carries a kind marker, `<!-- not-me:<kind> -->`, where
   kind is `new`, `update`, `review`, `prove`, or `claim`.
2. It also carries the run marker, `<!-- not-me:run=<id> -->`, for the current run.
3. Upsert: if a comment by the operator with the same kind marker exists, edit it.
   Otherwise post a new one. Strings in `markers.legacy` count as own markers too.
4. Never post a second summary of the same kind on the same item.

## Summary comment layout

1. First line: `**TL;DR:**` and one or two sentences on what changed and what is left.
2. Next: a `**Waiting on:**` list, one person per line, by login, with what they owe.
   Never collapse it. Write "nobody" when nothing is owed.
3. Then short sections: what was fixed, what was deferred or refused, how it was
   verified.
4. Long sections (logs, full test output, per-finding detail) go in `<details>`.
   Leave a blank line after the `<summary>` line or the markdown inside will not render.
5. Name people by login. Never address a decision to "you" (see `authority.md`).

## Thread replies

1. Reply on each finding thread through the REST reply endpoint for review comments.
   One reply per thread per run.
2. Say what was done and where: commit SHA, file and line, test name. Or the reason
   for a deferral, refusal, or disagreement.
3. Never resolve a thread from the new or update legs. The reviewer resolves.
4. The review leg may resolve only threads it judged fixed on the current head.

## PR body and testing steps

1. Keep the PR body's testing steps checklist current with the latest push.
2. Never collapse the testing steps. They stay in plain view.
3. Tick a step only when the run actually ran it and saw it pass.

## Review requests and labels

1. After a push, re-request review from each author whose findings were addressed.
2. Labels are written as one full-set write: compute the final set, write it once,
   then read it back. Never add and remove in separate calls or in one edit command.
3. A read-back mismatch is a failure. Log it and stop the write-back.

## References

1. Write issue and PR references as `owner/repo#N`, for example `acme/app#42`.
2. Use a bare `#N` only when the tracker is the same repo as the PR.
3. Closing keywords follow the same rule: `Closes acme/tracker#42`.

## Attribution

The `attribution` key in `project.md` sets one policy.

| Value | Effect |
|-------|--------|
| `inherit` | Add nothing. The harness decides. |
| `none` | No attribution line at all, even one the harness would add. |
| any other text | Add that literal text as the last line. |

1. Attribution goes on PR bodies and commit messages only. Never on comments.
2. Nowhere else may a post mention an AI, a model, an assistant, or a model name.

## Screenshots

1. Screenshots stay out of PR bodies and comments. Keep them in the run log.
2. The exception: `prove.md` configures a screenshots branch. Then link images from
   that branch.

## Result line for --bg runs

A `--bg` run ends with exactly one line:

```
<VERB> <ref|-> <status>[ <sep> note]  log=<path>
```

`<sep>` is a plain hyphen with one space on each side (` - `). VERB is one of:

| VERB | Meaning |
|------|---------|
| NONE | Nothing to do. Only this. A run that went wrong is FAILED. |
| PR | Opened a pull request. |
| PUSHED | Pushed commits to an existing PR. |
| PARKED | Parked a PR for a person (see `parking.md`). |
| HELD | Held an issue: cannot reproduce or already fixed. |
| REVIEWED | Posted a review. |
| PROVED | Posted a proof comment. |
| FAILED | Tried and failed. The note says why. |
| STOP | Stopped before work: disabled, paused, or guard selftest failed. |

Examples: `PR acme/app#57 opened  log=<path>`, `NONE - idle  log=<path>`.

## Example summary comment

```markdown
<!-- not-me:update -->
<!-- not-me:run=20261021-101500-a1b2c3 -->
**TL;DR:** Fixed 2 of 3 findings from @octocat. One question for @hubot on page size.

**Waiting on:**
- @hubot: should the default page size be 50 or 100? (thread on `pager.py:12`)
- @octocat: re-review the two fixes.

**Fixed**
- Off-by-one on the last page, `pager.py:42`, in abc1234. Test `test_last_page_full`.
- Missing empty-state message, `list.html:8`, in def5678.

**Deferred**
- Page size default: needs a product call from @hubot.

<details>
<summary>Verification</summary>

- Unit tests: 48 passed.
- Lint: clean.
- Manual: steps 1 to 3 in the PR testing steps, all pass.
</details>
```

## Checklist

- [ ] Every owned comment carries its kind marker and the run marker, and was upserted.
- [ ] Summary starts with `**TL;DR:**` and an uncollapsed Waiting on list.
- [ ] Long sections are in `<details>` with a blank line after the `<summary>` line.
- [ ] Replied on each finding thread through the REST endpoint and resolved none.
- [ ] Testing steps are current and not collapsed.
- [ ] Re-requested review from the finding authors.
- [ ] Wrote labels as one full set and read them back.
- [ ] References are `owner/repo#N` unless the tracker is the same repo.
- [ ] Attribution only on PR bodies and commits; no AI or model mention elsewhere.
- [ ] Screenshots kept to logs unless a screenshots branch is configured.
- [ ] A `--bg` run ended with exactly one result line in the grammar above.
