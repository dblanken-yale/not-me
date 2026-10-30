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

1. At the end of every run, success or not, return the checkout to the branch
   it started on (`branch` in the preflight output) with a clean tree. In the
   main checkout that is usually the default branch. A linked git worktree
   cannot check out a branch another worktree holds, so it goes back to its
   own starting branch, never to the default branch.
2. Restore any stash you made at the start.
3. A checkout you cannot clean is a `WARN` with the `git status` output.

## Worktrees for review

1. Review runs in a separate worktree, detached at the PR head SHA. Never check
   out the PR branch in the main checkout.
2. Never commit in a review worktree.
3. Remove only the worktree you created, at the end of the run.

## Worktrees for new and update work

`workspace.new` (or `workspace.update`) set to `worktree` runs the work in a worktree
of its own, so several runs can start from one main checkout and none of them
changes its branch or files. Inside a linked worktree already, work in place.

1. Make it once the target is known, before `notme log start`: read the sha with
   `git rev-parse HEAD`, then `notme git worktree-add <sha> --name notme-new-<N>`
   (`notme-update-<N>` for rework). `path_exists`: add `-2`, `-3`. Keep the
   returned `path`. A resumed run whose log names a path uses that path instead.
   A lost claim removes the worktree (rule 4) before the next pick makes another.
2. From then on pass `--project-dir <path>` to every `notme` call, so the run
   marker, branch, checkpoints and push all land in that worktree. Run local git
   as `git -C <path> ...`, never `cd <path> && git ...` (the guard does not follow
   `cd`). Run each check with the worktree as its directory, and brief the
   implementer with the absolute path.
3. The worktree starts detached: there is nothing to stash. Branch with
   `notme --project-dir <path> git branch-create <name> --base origin/<base>`.
4. On every way out, after `notme log finish`, run `notme git worktree-remove <path>`
   without `--project-dir`. Remove only the worktree this run made. A tree that will not come off clean stays: a `WARN` naming it.
   "committed but NOT PUSHED": keep it and name the path in the log summary.

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
`ready` false (it lists them in `stack_held` with the reason). With
`queue.stacking: auto`, an issue with no line gets a `stack_on` from its open
blockers (`via` is `blockers`): ready when they all resolve to one open PR that
passes the same checks, held when a blocker has no open PR yet. Treat it the same.

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
- [ ] Stashed with a unique message and restored by SHA.
- [ ] Left the checkout clean on the branch it started on.
- [ ] With `workspace` `worktree`: worked only in my own worktree, `--project-dir` on every `notme` call.
