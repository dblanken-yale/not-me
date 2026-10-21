# Verification

## When to read this

Read this before you change any code, and again before you claim a change works.
It covers the baseline, reproducing the problem, red-then-green tests, running the
project's own checks, the visual check, a light accessibility pass, the
cross-model claim check, and the CI wait. The minimum gate that follows
verification is in `gate.md`.

## Baseline first

1. Before you edit anything, run the commands under `## Baseline` in `verify.md`,
   or the test and lint commands when that section is left as is.
2. Record every failure that already exists, with its name, in the run log as a
   `CHECK` line. These are not yours to fix and not yours to blame on the change.
3. A baseline you could not run is a `WARN` line with the exact error. Never skip
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

## Use the project's own commands

1. Take commands from `verify.md`: `## Tests`, `## Lint`, `## Build`, `## CI parity`.
2. A section left as shipped means "discover it": read package manifests, the
   Makefile, and the run steps in the CI workflow files. Log what you found as a
   `DECISION` line.
3. Run every step the CI workflow runs, in the same order. Not a narrower local
   check: lint on one file is not the same gate as lint on the tree.
4. Use `## Environment start`, `## Health check` and `## Reset` as written when the
   change needs a running app.

## Visual check

UI diffs need a visual check line in the run log and the PR. There are exactly two
forms:

```
Visual check: <path of the screenshot you actually read>
Visual check: not run (<named reason>)
```

1. Reading the screenshot is the gate. Capturing it is not. Open the image and
   compare it with what the issue asks for.
2. The reason in the second form names the cause: `no env`, `no UI files changed`,
   `browser tool missing`. Never "skipped" or "not needed" alone.
3. No other wording. "Looks fine" or "verified visually" without a path is a false
   claim.

## Light accessibility pass

For UI diffs, check what you changed for:

1. Text contrast against its background.
2. Alt text on images that carry meaning; empty alt on decorative ones.
3. Labels on form fields.
4. Accessible names on buttons and links, including icon-only ones.

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
2. Dispatch one subagent on `models.verifier`. `auto` means a model from the other
   family than the one running; `none` means skip this check and log that it was
   skipped.
3. Give it the claims, the diff, and the file paths. Ask for one verdict per
   claim: confirm or reject, with a reason.
4. Apply verdicts mechanically. A rejected disagreement becomes an implement. A
   rejected "already fixed" becomes a fix. A rejected deferral is implemented when
   in scope, else deferred with the verifier's reason added.
5. Log each verdict as a `CHECK` line. Do not argue with the verdict.

## CI wait and fix loop

1. After a push, wait for CI on the pushed head SHA, not on an older run. Wait up
   to `limits.ci_wait_minutes`.
2. CI red: read the failing job's log, fix, run the same step locally, push again.
3. Each push after a red run is one round. Stop at `limits.fix_loop` rounds.
4. At the cap, the outcome is "still needs work": park the PR with `needs_human`
   (see `parking.md`) and name the failing job and test.
5. A CI wait that times out is a `WARN`, not a pass.

## Checklist

- [ ] Captured the baseline test and lint state before any edit.
- [ ] Reproduced the bug, or held the issue when it could not be reproduced.
- [ ] Saw each new test fail before the change and pass after it.
- [ ] Ran every CI step locally with the project's own commands.
- [ ] Wrote one `Visual check:` line in one of the two allowed forms for UI diffs.
- [ ] Did the accessibility pass and flagged what was not auto-fixed.
- [ ] Ran the cross-model claim check, or logged that `models.verifier` is `none`.
- [ ] Waited on CI for the pushed head and stopped at `limits.fix_loop`.
