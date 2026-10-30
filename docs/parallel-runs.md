# Parallel runs

not-me can work several issues at the same time, each in its own worktree. This page says how to start that,
and lists what used to stop it and where each of those stands.

## How to run several at once

1. Set `workspace.new: worktree` in `project.md` (repo or global layer):

   ```yaml
   workspace: {new: worktree, update: checkout, review: worktree}
   ```

   Each `/not-me:new` run then makes its own worktree under `<main checkout>/.claude/worktrees/notme-new-<issue>`,
   branches there, and never changes the main checkout's branch or files. It passes `--project-dir <worktree>`
   to every `notme` call and runs git as `git -C <worktree> ...` (see "Worktrees for new and update work" in
   `plugins/not-me/references/branching.md`). It removes the worktree when it finishes.

2. From a session in the main checkout, run:

   ```
   /not-me --parallel 3
   ```

   The router checks that not-me is on and not paused, picks up to 3 distinct ready issues from
   `notme pick-issue` by your picking rules, and starts one subagent per issue, all at once, each running
   `/not-me:new <issue>`. It prints each run's result line. N above `limits.parallel_runs` (default `3`) is
   lowered to it. It stops with `STOP - parallel needs workspace.new: worktree` when that setting is missing,
   and it is refused together with `--bg`.

   Doing it by hand works the same way: start each agent (or each terminal session) in the main checkout,
   **without** worktree isolation, and have each run `/not-me:new <issue>` on a different issue.

Claims keep two runs off one issue. A claim is compare-and-set on the issue, backed by a local lock file in
`<git-common-dir>/not-me/claims/` that every worktree shares, so the second run to claim an issue gets exit 3
and stops with `STOP <ref> claimed elsewhere`. `tests/test_queue_labels.py` (`ClaimTests`, the two
`test_a_parallel_run_...` cases) shows it, including the race where the first run holds the lock but has not
written yet.

## Limits

| Limit | Where | Default |
| --- | --- | --- |
| Runs `/not-me --parallel` starts at once | `limits.parallel_runs` in `project.md` | `3` |
| A test environment every worktree shares | `shared_env: true` in `verify.md` makes the verify step hold `<git-common-dir>/not-me/verify.lock` (`notme verify-lock`), so only one run tests at a time | `false` (each worktree tests on its own) |
| Runs per worktree | the active-run marker | one |
| Subagent depth | the router starts each run as a subagent, and the run starts its implementer as another; `/not-me:doctor` checks the setting | |

Each run still costs what a single run costs; three at once use the shared usage pool three times as fast.

## Blockers to running N tickets at once

| Blocker | Status | Notes |
| --- | --- | --- |
| Run lock | Fixed by dblanken-yale/not-me#136 | The active-run marker was repo wide, so a second run stopped with `active_run`. It is now per worktree, in that worktree's own git dir; one worktree holds one run. |
| Harness worktree isolation | Open, outside not-me | An agent started with Claude Code's worktree isolation has every `notme git ...` call (branch-create, push) refused, because the harness cannot tell what a git call wrapped in `notme` targets; it asks for git named right after the launcher in a plain command. Raw git writes from skills are not allowed (every push goes through the CLI write gate), so not-me does not work around this. Start agents without isolation and let not-me make the worktree (`workspace.new: worktree`). |
| Shared main checkout | Fixed by dblanken-yale/not-me#139 | Unisolated runs in one checkout used to switch its branch under each other. With `workspace.new: worktree` each run works only in its own worktree; `/not-me --parallel` requires that setting. |
| Config read only from the main checkout | By design | The CLI's trust anchor is the main checkout, so a branch or a worktree cannot widen what not-me may do. Every parallel run reads the same config; an edit made in a worktree counts once it is merged and pulled. |
| Run logs | Not a blocker | Each run writes its own log under `<git-common-dir>/not-me/logs`, named by run id, so runs never share a file. `/not-me:retro` stops while any run is active, so `--parallel` skips the retro; run `/not-me:retro <log>` afterwards. |
| The guard's view of active runs | By design (dblanken-yale/not-me#136) | The guard finds every run marker from the git common dir without reading config, and keeps the config read-only while a run is active in any worktree. |
| Worktree name prefix | Fixed by dblanken-yale/not-me#139 | `notme git worktree-add` took only `review-` names. It now also takes `notme-new-` and `notme-update-` names, still checked against one pattern and placed only under `<main checkout>/.claude/worktrees/`. The `notme-` part keeps a person's own session worktrees there (often named `new-...` or `update-...`) off limits. |
| `cd <worktree> && git commit` | Open, dblanken-yale/not-me#144 | The guard does not follow `cd`, so a commit written that way is judged against the main checkout's branch and blocked. Skills are moving to `git -C <worktree>`; `branching.md` already says so for worktree runs. |
| `--parallel` with `--bg` | Open | A `--bg` result is exactly one line, and a batch has one line per run. Use `--parallel` in an attended session for now. |
| Rework (`update`) in parallel | Open | `branching.md` covers `notme-update-<N>` worktrees, but the update skill does not make one yet and `--parallel` starts only new-issue runs. |
