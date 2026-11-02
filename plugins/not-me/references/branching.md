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
3. The head branch of the PR a `Stack on:`, `Based on:` or `On top of:` line names,
   when `notme issue N` returns a `stack_on` fact with `stacks` true (see Stacking below).
4. A `Base branch: <name>` line in the issue body.
5. The epic's branch, when the issue is a sub-issue of an epic that has one.
6. The repo's default branch.

A base that is declared (by 1 to 5) but does not exist on the remote: STOP. Log the
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
2. If the merge brought in commits, run the targeted tests again before the push
   (the final tests when no targeted command is configured), and the full set
   only when the targeted tests cannot cover what came in (`verification.md`,
   "Targeted tests").
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

1. Update and new runs never switch, stash or edit the checkout they start in;
   they work in a run worktree (below). At the end of every run, success or not,
   that checkout is on the branch it started on (`branch` in the preflight
   output), as it was.
2. A run that did stash in its starting checkout restores that stash by SHA.
3. A checkout you cannot clean is a `WARN` with the `git status` output.

## Run worktrees

Each update and new run works in its own worktree, so a run that dies leaves
nothing in the operator's checkout and holds a branch only for its own life.

1. Make it: `notme git worktree-add --branch <branch>` for an existing branch
   (fetch it first), or `--branch <name> --base origin/<base>` for a new one. The
   CLI picks a unique `run-*` path under `.claude/worktrees/` and returns it as
   `path`; call it `<wt>` and log it. `branch_in_use` means another worktree
   holds the branch: that PR or branch is skipped, never forced.
2. Work only in `<wt>`: `notme --project-dir <wt> git ...` for the CLI's git
   commands, `git -C <wt> ...` for raw reads, merges and the commit, and every
   implementer, verify and gate command runs with `<wt>` as its directory.
3. At the end, whatever the outcome, remove it: uncommitted edits left (a commit
   that was blocked or failed, a run that stopped mid-unit) are saved first with
   `notme --project-dir <wt> git save-patch`, which writes a patch under
   `<git-common-dir>/not-me/patches/` and resets only that run worktree. Log the
   returned `path` as a `WARN`. Then `notme git worktree-remove <wt>`. The
   branch stays.
4. A saved patch is not lost work: a later run on the same branch reads the
   `WARN` in the previous log, applies it with `git -C <wt> apply <patch>` when
   it applies cleanly, and audits it like any unit's diff; otherwise it redoes
   the work.
5. Never edit, stash, reset or commit in a worktree you did not create: the main
   checkout, a person's worktree, a session worktree. `save-patch` and
   `worktree-remove` refuse anything but a `run-*` or `review-*` worktree.

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

## Stacking on an open PR

An issue may declare one line, `Stack on: <owner/repo#PR>`, `Based on: <owner/repo#PR>`
or `On top of: <owner/repo#PR>` (a PR, or an issue whose single open PR is used).
It counts only from a trusted author, and only the CLI reads it: `notme issue N`
returns `stack_on`, and `notme pick-issue` leaves out an issue whose `stack_on` has
`ready` false (it lists them in `stack_held` with the reason).

1. `stacks` true: branch from `origin/<head>`, the `head` field of `stack_on`, and
   open the PR against that branch with `Builds on <pr>`, the `pr` field (see
   `pr-shape.md`).
2. `stack_on` not null and `ready` false, however the issue was chosen (an explicit
   number, or a change between the pick and `notme issue N`): never fall through to
   `Base branch:`, the epic or the default branch. Stop before the claim, or release
   the issue if it is already claimed. Log the reason and end with
   `HELD <ref> stack-on - <reason>`. Create no branch.
3. The PR is merged: `stacks` is false and `ready` is true: a normal issue on the
   default branch (or its other declared base).
4. `/not-me:update` on the stacked PR merges the predecessor branch in at both
   checkpoints (its base is that branch). GitHub retargets the stacked PR only
   when the predecessor's head branch is deleted on merge; otherwise the base
   stays on the old branch, and `/not-me:update` reports it (no branch edits).
   Nothing here force-pushes or deletes a branch.
5. The PR must be in the repo of this checkout; a PR in another allowed repo is held.

## Checklist

- [ ] Chose the base by the precedence list, and stopped on a declared base that does not exist.
- [ ] Committed, pushed and merged nothing on a protected branch.
- [ ] Named the branch from `standards.branch`, the same in every companion repo.
- [ ] Merged the base in at Checkpoint A and Checkpoint B; rebased nothing.
- [ ] Never force-pushed or amended a pushed commit.
- [ ] Worked in a run worktree made by the CLI, saved any leftover edits as a patch, and removed it at the end.
- [ ] Touched no worktree this run did not create; left the starting checkout on its branch.
