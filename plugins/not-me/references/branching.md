# Branching

## When to read this

Read this before you create, check out, update, or push a branch, and before you
leave the checkout at the end of a run. It sets the base branch, the branch name,
the two merge checkpoints, and how to leave the checkout clean. The guard enforces
part of it; the rest is on you.

## Pick the base

Take the first that applies:

1. `--base <branch>` given on the command line.
2. The branch of the predecessor sub-issue, when the issue is a chained sub-issue
   of an epic (see Epics below).
3. A `Base branch: <name>` line in the issue body.
4. The epic's branch, when the issue is a sub-issue of an epic that has one.
5. The repo's default branch.

A base that is declared (by 1 to 4) but does not exist on the remote: STOP. Log the
exact name and where it was declared, add `needs_human`, and name the issue author.
Never fall back to the default branch silently.

Example: the issue says `Base branch: release-2.4` and `origin/release-2.4` does not
exist. Result line: `STOP acme/app#88 blocked - base release-2.4 not found`.

## Never touch protected branches

1. Never commit while checked out on a branch in `protected_branches`.
2. Never push to one, merge into one, or open a PR whose head is one.
3. A PR may target a protected branch as its base. That is the normal case.
4. The guard blocks these. A block is final (see `prohibitions.md`).

## Name the branch

1. Build the name from `standards.branch`, default `{issue}-{slug}`. Example:
   `88-fix-pager-last-page`.
2. The slug is the issue title in lower case, words joined by hyphens, short.
3. In a companion set, use the same name in every repo (see `companions.md`).
4. If the branch already exists on the remote and is not yours to resume, stop and
   log a QUESTION. Do not reuse someone else's branch.

## Checkpoint A: before work

1. Fetch. Check out the branch. Fast-forward it to its remote.
2. Merge the base into it: `git merge origin/<base>`.
3. Conflicts you can resolve with confidence: resolve, run the tests, commit the
   merge. Conflicts you cannot: abort the merge, log the files, park.

## Checkpoint B: before push

1. Fetch again. Merge the base in again.
2. If the merge brought in commits, run the final tests again before the push.
3. Then push the branch to its own name on origin.

## Merge, never rebase

1. Bring the base in with merge commits only. Never rebase a branch.
2. Never force-push in any form. If a push is rejected as non-fast-forward, fetch,
   merge the remote branch in, test, push again.
3. Never amend or squash a pushed commit.

## Stash by SHA

When you must set aside local changes:

1. `git stash push -m "not-me <run id> <reason>"` with a unique message.
2. Record the stash commit SHA from `git rev-parse stash@{0}` in the log.
3. Restore by that SHA: `git stash apply <sha>`, then drop it by finding the entry
   whose SHA matches. Never `git stash pop` by position: another process may have
   stashed in between.

## Leave the checkout clean

1. At the end of every run, success or not, return the checkout to the repo's
   default branch with a clean tree.
2. Restore any stash you made at the start.
3. A checkout you cannot clean is a `WARN` with the `git status` output.

## Worktrees for review

1. Review runs in a separate worktree, detached at the PR head SHA. Never check
   out the PR branch in the main checkout.
2. Never commit in a review worktree.
3. Remove only the worktree you created, at the end of the run.

## Epics

1. Work sub-issues in their listed order. Each sub-issue is its own branch and PR.
2. The first sub-issue branches from the epic base. Each later one branches from
   its predecessor's branch, and its PR targets that branch.
3. The PR body says "Builds on acme/app#91" for the predecessor PR (see
   `pr-shape.md`).
4. Never pick the epic issue itself.

## Checklist

- [ ] Chose the base by the precedence list, and stopped on a declared base that does not exist.
- [ ] Committed, pushed and merged nothing on a protected branch.
- [ ] Named the branch from `standards.branch`, the same in every companion repo.
- [ ] Merged the base in at Checkpoint A and Checkpoint B; rebased nothing.
- [ ] Never force-pushed or amended a pushed commit.
- [ ] Stashed with a unique message and restored by SHA.
- [ ] Left the checkout on a clean default branch.
