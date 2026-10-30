# Verification

## When to read this

Read this before you change any code, and again before you claim a change works.
It covers the verify plan, the baseline, reproducing the problem, red-then-green
tests, targeted tests, running the project's own checks, the visual check, a light
accessibility pass, the cross-model claim check, and the CI wait. The minimum gate that follows
verification is in `gate.md`.

## Baseline first

1. Before you edit anything, run `notme verify-plan --base <base>` once and keep
   its JSON: `baseline` (run, reason), `targeted` (commands) and `ci_wait`.
2. `run` true: run the commands under `## Baseline` in `verify.md`, or the test
   and lint commands when that section is left as is. `run` false
   (`baseline: auto` and green base CI at its head SHA): skip it and log the
   plan's reason as a `CHECK` line. Red, pending, missing or unreadable base CI
   always runs it. A rework run (`/not-me:update`) passes `--sha` with `HEAD`
   after Checkpoint A, so a merge commit made there (no CI) runs the baseline.
3. Record every failure that already exists, with its name, in the run log as a
   `CHECK` line. These are not yours to fix and not yours to blame on the change.
4. A baseline you could not run is a `WARN` line with the exact error. Never skip
   it silently.

Example: `CHECK baseline: 212 passed, 2 failed (test_tz_offset, test_locale_sort) before any change`.

## Reproduce before fixing

1. For a bug, follow the issue's steps on the base branch and see the failure.
2. Write down what you ran and what you saw.
3. Cannot reproduce, or it is already fixed on the default branch: stop. The issue
   is HELD (see `parking.md`). Do not guess at a fix.

## Red, then green

1. Write the test that captures the bug or the new behaviour first.
2. Run it and watch it fail for the right reason. Log the failing line.
3. Make the change. Run the test again and watch it pass.
4. A test that passed before your change proves nothing. Rewrite it until it fails
   without the change.

## Targeted tests

1. The plan lists targeted commands: during the work (red then green, the audit
   after the implementer, each fix round) run only those, with the changed test
   files or test names as trailing arguments. None listed: the full commands.
2. The full `## Tests`, `## Lint` and `## Build` run once, right before the push
   (gate step 6). Checkpoint B commits: targeted again, the full set only when the
   targeted tests cannot cover them (shared config, fixtures, the build).
3. Only a command pinned under `## Targeted tests` is targeted; never narrow one.
4. Never run a command under `## Person only` in `verify.md` (live evals, real
   model or paid service calls), not even when CI runs it. A person runs those.

## Use the project's own commands

1. Take commands from `verify.md`: `## Tests`, `## Lint`, `## Build`, `## CI parity`.
2. A section left as shipped means "discover it": read package manifests, the
   Makefile, and the run steps in the CI workflow files. Log what you found as a
   `DECISION` line.
3. Before the push, run every step the CI workflow runs, in the same order. Not a narrower local
   check: lint on one file is not the same gate as lint on the tree.
4. Use `## Environment start`, `## Health check` and `## Reset` as written when the
   change needs a running app.

## Shared test environment

`shared_env: false` (the default): each worktree verifies on its own; take no lock.
`shared_env: true` (one stack every worktree shares): run `notme verify-lock
acquire --log <path>` right before the first command that uses it and `notme
verify-lock release --log <path>` right after the last; nothing else waits on it.
Kind `verify_locked`: log a `CHECK` naming the holder and acquire again with
`--wait-seconds 540`, up to `limits.ci_wait_minutes` in all; then `FAILED <ref>
shared env busy`, a failure on our side (`autonomy-and-logging.md`). Never skip or
shorten verification to avoid the wait. `notme log finish` drops a held lock.

## Visual check

UI diffs need a visual check line in the run log and another in the PR. The run
log keeps the path. Text bound for GitHub never holds a local path.

```
Visual check: <path of the screenshot you actually read>     (run log)
Visual check: read <what was checked, in words>              (PR body, GitHub)
Visual check: not run (<named reason>)                       (either)
```

1. Reading the screenshot is the gate. Capturing it is not. Open the image and
   compare it with what the issue asks for.
2. The reason in the second form names the cause: `no env`, `no UI files changed`,
   `browser tool missing`. Never "skipped" or "not needed" alone.
3. No other wording. "Looks fine" or "verified visually" without a path (log) or
   without words that say what was compared with what the issue asks (PR) is a
   false claim. Example: `Visual check: read the settings page at 375px; the Save
   button sits below the form as the issue asks`.
4. Link a screenshot in the PR only when `prove.screenshots` names a branch
   (`branch:<name>`) and the image already exists on it. The run never pushes
   images.

## Light accessibility pass

For UI diffs, check what you changed for text contrast, alt text on images that
carry meaning (empty alt on decorative ones), labels on form fields, and
accessible names on buttons and links, including icon-only ones.

Auto-fix the trivial ones (a missing alt, a missing label). Flag the rest in the PR
with file and line. Never pass a finding silently.

## Evidence before claims

1. A green build is the floor, not the proof. Say what you ran and what it showed.
2. Every claim in a post must point at evidence: a command and its result, a test
   name, a commit SHA, a screenshot path.
3. Every gate finding you did not fix gets a one-line reason in the PR or summary.

## Cross-model claim check

Before you post, check the claims this run originated with a second model.

1. Claims to check: each disagreement with a reviewer, each deferral, each
   "already fixed", and each unfixed gate finding with its reason.
2. Dispatch one subagent on `models.verifier`. `auto` means a
   different model than the one running: a run on `opus` is checked by `sonnet`, a
   run on any other model is checked by `opus`. Never ask the operator about it. A
   model name pins that model. `none` means skip this check and log that it was
   skipped.
3. Give it the claims, the diff, and the file paths. Ask for one verdict per
   claim: confirm or reject, with a reason.
4. Apply verdicts mechanically. A rejected disagreement becomes an implement. A
   rejected "already fixed" becomes a fix. A rejected deferral is implemented when
   in scope, else deferred with the verifier's reason added.
5. Log each verdict as a `CHECK` line that names the model that verified, for
   example `CHECK verifier(sonnet) confirm: <claim>`. When `models.verifier` is
   `none`, log `CHECK claim check skipped: models.verifier is none`. Do not argue
   with the verdict.

## CI wait and fix loop

1. After a push, wait for CI on the pushed head SHA, not on an older run. Wait up
   to `limits.ci_wait_minutes`. `ci_wait` false in the plan
   (`limits.ci_wait_minutes: 0`): open the PR and do not wait. Log
   `CHECK CI wait skipped: limits.ci_wait_minutes is 0`. A red CI result is then
   left to `/not-me:update` or a person; the run never claims CI passed.
2. CI red: read the failing job's log, fix, run the same step locally, push again.
3. Each push after a red run is one round. Stop at `limits.fix_loop` rounds.
4. At the cap, once the failure is not also on the base (next section): the PR
   keeps `needs_work`, gains `needs_human` and a summary of what is still red
   naming the failing job and test; the result is `FAILED <ref> ci-red` (`parking.md`).
5. A CI wait that times out is a `WARN`, not a pass.

## A failure that is also on the base

Before a check or CI job counts against the change (the `models.max_tries` or
`limits.fix_loop` outcomes), make sure it is not failing on the base too.

1. The baseline already shows the same failure: it is on the base.
2. Else run the same command on the base: `git rev-parse origin/<base>` (a read),
   `notme git worktree-add <base sha> --name review-base-<short sha>`, run it in
   the returned `path`, then `notme git worktree-remove <path>`.
3. Same failure on the base: it is a failure on our side, not the change's.
   Follow "Failures on our side" in `autonomy-and-logging.md`: no label, no post,
   `FAILED <ref> base-red - <check>`, stop.
4. Passes on the base: the change caused it. Go on with the normal outcome.

## Checklist

- [ ] Ran `notme verify-plan` before any edit and kept its JSON.
- [ ] Captured the baseline test and lint state before any edit, or logged why the plan skipped it.
- [ ] Ran only the targeted tests during the work when the plan has them, and the full set once before the push.
- [ ] Ran no person-only command.
- [ ] Reproduced the bug, or held the issue when it could not be reproduced.
- [ ] Saw each new test fail before the change and pass after it.
- [ ] Ran every CI step locally with the project's own commands.
- [ ] With `shared_env` true, held `notme verify-lock` only while using the environment.
- [ ] Wrote a `Visual check:` line for UI diffs: path form in the log, words form in the PR.
- [ ] Did the accessibility pass and flagged what was not auto-fixed.
- [ ] Ran the cross-model claim check, or logged that `models.verifier` is `none`; verdict lines name the verifier.
- [ ] Waited on CI for the pushed head and stopped at `limits.fix_loop`, or logged that `ci_wait` is false.
- [ ] Ran a failing check on the base before counting it against the change.
