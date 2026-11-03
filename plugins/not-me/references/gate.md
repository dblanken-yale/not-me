# Gate

## When to read this

Read this after verification passes and before you commit for review. The gate
is the last check between a change and a pull request. Its minimum is fixed in the
plugin: no config layer can remove a step. A project can only add steps, in the
`## Gate additions` section of `verify.md`.

## The minimum gate

Run these in order. Log each as a `GATE` line with its result.

1. **Tests red then green.** Every new or changed behaviour has a test you saw fail
   and then pass (see `verification.md`).
2. **Simplify.** When the diff is over about 20 changed lines, not counting tests
   and lock files, run `/simplify` and apply what it finds.
3. **Code review.** Run the `pr-review-toolkit:code-reviewer` agent on the diff
   when it is in the list of available agent types. When it is not, run the review
   as a foreground general-purpose subagent with a code review brief, the way step 4
   runs the security review. Name the reviewer that ran in the log line, for example
   `GATE code-review reviewer=pr-review-toolkit:code-reviewer pass` or
   `reviewer=general-purpose`. Decide from the available agent list before
   dispatching: the fallback applies only when the agent is not listed. A listed
   agent whose dispatch fails or times out is a step that cannot run (see "When a
   gate step cannot run"): `WARN` with the exact error, no fallback. Do not rely on
   `/code-review`: it may be user-invocation only.
4. **Security review.** Run it as a foreground general-purpose subagent with a
   security review brief. Not through the Skill tool: a skill run there ends on its
   own report and the run loses control.
5. **CI-parity lint.** The lint command CI runs, on the whole tree.
6. **Final tests.** The full `## Tests`, `## Lint` and `## Build` commands, once,
   after every fix above and right before the push. During the work (red then
   green, the audit after the implementer, each fix round) only the targeted
   tests run when `notme verify-plan` lists them (`verification.md`, "Targeted
   tests"); with none configured, the full commands run at every step.

Then run each step listed under `## Gate additions` in `verify.md`, in order.

## Briefing reviewers

1. Tell every reviewer to flag only correctness bugs and missed requirements.
2. Give it the issue's acceptance criteria and the diff.
3. Anything else it reports is optional. Do not loop on style or taste.

## Fix, don't defer

A gate finding is fixed in this run, as a new foreground dispatch through `delegation.md`. Park it only when it is one of:

1. A product reversal: the finding asks for the opposite of what the issue asks.
2. Scope that adds a new feature the issue does not ask for.
3. Something only a named person can attest: a legal call, a design sign-off,
   access you do not have.

Everything else is fixed, retested, and the gate step run again. A parked finding
gets a one-line reason and a named login in the PR (see `parking.md`).

Example: the reviewer flags a missing null check in `pager.py:42`. Fix it. The
reviewer suggests a new export format. That is new scope: note it in the PR as a
follow-up for @octocat.

## When a gate step cannot run

1. A tool or agent that is missing, or a dispatch that fails, is a `WARN` line
   with the exact error. The one exception is the optional code reviewer agent in
   step 3 when it is absent from the agent list: the general-purpose fallback
   review counts as the step having run, and the `reviewer=` field says so.
2. A step that cannot run is a failure on our side, not a gate result. The run
   does not claim the gate passed, does not push or open a PR, posts nothing about
   it, and stops as `autonomy-and-logging.md` says ("Failures on our side").
3. A failed step is never retried by routing around it, for example by running a
   narrower lint.
4. A step that fails the same way on the base is also on our side
   (`verification.md`, "A failure that is also on the base").

## Order with the rest of the run

1. Verify (`verification.md`), then gate, then commit, then merge the base in
   (Checkpoint B in `branching.md`), then push.
2. If Checkpoint B brings in changes, run the targeted tests again before the
   push, and the full set again only when the targeted tests cannot cover what
   came in. With no targeted command, run the final tests again.

## Checklist

- [ ] Saw every new test fail and then pass.
- [ ] Ran `/simplify` when the diff is over about 20 changed lines.
- [ ] Ran the code review (the named agent, or a general-purpose subagent when it is not installed), briefed it to flag only bugs and missed requirements, and logged `reviewer=`.
- [ ] Ran the security review as a foreground general-purpose subagent.
- [ ] Ran the CI lint command on the whole tree and the full tests, lint and build once, last.
- [ ] Ran every step under `## Gate additions` in `verify.md`.
- [ ] Fixed every finding except product reversals, new scope, or attestations.
- [ ] Logged each gate step as a `GATE` line.
