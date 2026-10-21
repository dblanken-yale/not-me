# Companions

## When to read this

Read this whenever `repos` in the effective config lists more than one repo, or a
PR you are working on has a branch of the same name in another configured repo. It
says how a companion set is found, which config applies inside each repo, the push
order, and how the set is shown to reviewers.

## What a companion set is

1. `repos` lists the repos in push order. Each has a `role`: one `primary`, the
   rest `companion`.
2. The work for one issue spans the set on the same branch name in every repo
   (see `branching.md`).
3. The issue lives in the `tracker`; the primary PR is the one that closes it.

Example: `acme/app` (primary) and `acme/app-ui` (companion), both on
`88-fix-pager-last-page`.

## Find the linked set

1. Look for the branch name only in repos listed in `repos`. Nowhere else.
2. A PR counts as part of the set only when its head is in the same repo as its
   base (never a fork), and its author is the operator or on the roster.
3. A same-named branch that fails either test is not part of the set. Log it as a
   `WARN` and leave it alone.

## Which config applies where

1. The primary's `project.md` defines the set, the queue, the labels and the roster.
2. Inside a companion checkout, that repo's own `.claude/not-me/` applies for
   `standards.md`, `verify.md`, `review.md` and its guard rules.
3. So run each repo's own tests, lint and gate additions in that repo.
4. A companion with no `.claude/not-me/` uses the global and default layers.

## Push order

1. Commit in each repo on its own branch.
2. Push companions first, in `repos` order, then the primary last.
3. A push that fails in one repo stops the pushes after it. Log which repos were
   pushed and which were not; the next run resumes from there.

## Opening PRs

1. Open companion PRs first, then the primary.
2. Every PR in the set carries a `## Companion PRs` section listing every other PR
   by `owner/repo#N`. Upsert it on the earlier PRs as later ones open.
3. Apply `needs_review` and request reviewers on the primary only when every
   companion PR exists (see `pr-shape.md`).

```markdown
## Companion PRs

- acme/app#57 (primary)
- acme/app-ui#14
```

## Reviewing a set

1. Read every PR in the set before any verdict. A finding in a companion can block
   the whole set.
2. Run each repo's checks in its own worktree.
3. Post one verdict on the primary, with a short note per companion PR.
4. Set labels on every PR in the set to match the verdict.

## Updating a set

1. Collect trusted findings from every PR in the set (see `authority.md`).
2. Fix each in the repo it points at; push in the order above.
3. Write back on every PR in the set: thread replies where the threads are, and the
   summary on each PR (see `write-back.md`).

## Checklist

- [ ] Found companion PRs only in configured repos, same-repo heads, trusted authors.
- [ ] Used each companion repo's own standards, verify, review and guard inside it.
- [ ] Used the same branch name in every repo of the set.
- [ ] Pushed companions first and the primary last.
- [ ] Upserted `## Companion PRs` on every PR in the set.
- [ ] Applied `needs_review` on the primary only once every companion PR existed.
- [ ] Reviewed the whole set before posting one verdict on the primary.
