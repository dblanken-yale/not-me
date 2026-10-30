# PR shape

## When to read this

Read this before you open a pull request or edit a PR body. It fixes the title, the
body sections, when reviewers and labels go on, and how stacked PRs link. What a
comment looks like and how labels are written are in `write-back.md`.

## Title

1. Build it from `standards.pr_title`, default `{issue}: {issue_title}`.
   Example: `88: Last page drops the final item`.
2. Do not add prefixes, tags, or emoji the template does not have.

## Body

The first line is a linked heading for the issue, so a reader sees the issue first:
`## [88: Last page drops the final item](https://github.com/acme/app/issues/88)`.
The link text is always `<n>: <issue title>`, whatever `standards.pr_title` says.
Put a backslash before each of these in the title: ``\ ` * _ [ ] < > # ! | ~ & @ $``.
The URL points at the tracker repo, which may be another repo than the PR. A PR with
no issue has no heading. Section edits on an existing body keep this line in place.

Below the heading: if `standards.pr_template` resolves to a template, fill its
sections. Otherwise use these, in order:

1. `## Summary`: what changed and why, in a few lines.
2. `## Testing steps`: a checklist a person can follow on a deploy target. Never
   collapse it in `<details>`. Tick a step only when you ran it and saw it pass.
3. `## Companion PRs`: one line per PR in the set, when there is a set (see
   `companions.md`).
4. `## References`: issues and PRs as `owner/repo#N`, for example `acme/app#88`.

Closing keyword (`standards.closing_keyword`):

1. `auto` and the tracker is the same repo as the PR: `Closes acme/app#88`. Always the
   full `owner/repo#N`, never a bare `#88`: a bare number is ambiguous across a
   companion set, and GitHub honours the qualified form in the same repository.
2. `auto` and the tracker is another repo: no keyword. List `acme/tracker#88` under
   References only.
3. Any other value: use it as the keyword with the full ref, `Fixes acme/tracker#88`.

Attribution follows `write-back.md`. Nothing else in the body names an AI, a model,
or an assistant.

## Open, then label

1. Open the PR ready for review. Never as a draft.
2. Request reviewers and apply `needs_review` only once every companion PR in the
   set exists. A primary PR opened before its companions waits.
3. Reviewers come from `reviewers`, by repo name, else the default list.
4. Then write back: issue labels to the in-review state, and the board column when
   the queue is a board. Use one full-set label write with a read-back.

## Stacked PRs

For chained sub-issues of an epic, and for an issue whose `stack_on` fact has `stacks` true
(see `branching.md`):

1. Each PR targets its predecessor's branch, not the default branch.
2. The body's first line under Summary is `Builds on acme/app#91` (for a stacked
   issue, declared or found from its blockers, the `pr` field of `stack_on`).

## Example body

```markdown
## [88: Last page drops the final item](https://github.com/acme/app/issues/88)

## Summary

The pager dropped the last item when the total was a multiple of the page size.
`pager.py` now rounds the page count up.

## Testing steps

- [ ] Open the item list with exactly 50 items.
- [ ] Go to page 2. It is empty and says "No more items".
- [ ] Go back to page 1. All 50 items are listed.

## Companion PRs

- acme/app-ui#14: matching empty-state text.

## References

- Closes acme/app#88
```

## Checklist

- [ ] Title follows `standards.pr_title`.
- [ ] Body starts with the linked issue heading, when there is an issue.
- [ ] Body has Summary, uncollapsed Testing steps, Companion PRs when needed, References.
- [ ] References are `owner/repo#N`; the closing keyword follows the auto rule.
- [ ] Opened ready for review, not as a draft.
- [ ] Requested reviewers and set `needs_review` only once every companion PR existed.
- [ ] Stacked PRs target the predecessor and say "Builds on".
- [ ] Wrote issue labels and the board column back after opening.
