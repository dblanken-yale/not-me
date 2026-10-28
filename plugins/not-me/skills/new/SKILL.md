---
name: new
description: "Pick up one ready GitHub issue from the configured queue (or the issue number given), claim it, implement it on a feature branch through a delegated implementer, verify and gate the change, push, and open a pull request for review. Use when the operator runs /not-me:new, hands over an issue number to implement end to end, or the /not-me router reaches its new leg. NOT for reworking review feedback on an existing PR (use /not-me:update), NOT for reviewing someone else's PR (use /not-me:review), NOT for proving a PR on a deploy target (use /not-me:prove)."
argument-hint: "[issue#] [--base B] [--dry-run] [--bg]"
---

# /not-me:new

Implement one issue and open one pull request. One issue per run.

Arguments: `$ARGUMENTS`. An optional issue number, `--base B`, `--dry-run`, `--bg`.

## Ground rules

- `${CLAUDE_PLUGIN_ROOT}` below is substituted at load time with this plugin's
  directory. Read the files it names with the Read tool. It is not a shell
  variable: never write `$CLAUDE_PLUGIN_ROOT` in a command.
- Call the CLI as `notme` (the plugin's `bin/` is on the PATH). If the shell cannot
  find it, call `"${CLAUDE_PLUGIN_ROOT}/bin/notme"` with the same arguments. Every
  call prints one JSON object; read it and the exit code. `--help` on any
  subcommand prints its flags.
- With `--dry-run`, pass `--dry-run` to every `notme` call. Run the whole flow as
  if live and say `dry-run` in the result note.
- Every GitHub write goes through `notme`. Text goes in files under the temp directory made as in `${CLAUDE_PLUGIN_ROOT}/references/autonomy-and-logging.md` (Text to GitHub, rule 2), never in a shell argument.
- After `notme log start`, every write call also passes `--log <path>`.
- Label names in this file are the abstract keys of `labels` (for example
  `needs_human`). When a command takes a repo label name, write the value the
  `labels` map in the config gives for that key.
- Never mention an AI, a model or an assistant in any post. The only exception is
  the `attribution` line, which the CLI adds to PR bodies.
- Read `${CLAUDE_PLUGIN_ROOT}/references/prohibitions.md` before you act on any
  request. A prohibited request is refused, never done in part.
- A guard block is final: do not retry it, rephrase it, or route around it. Log a
  QUESTION naming the operator and see the failure table below.

## Step 0: Preflight

Read `${CLAUDE_PLUGIN_ROOT}/references/config-and-layers.md` and
`${CLAUDE_PLUGIN_ROOT}/references/autonomy-and-logging.md`.

1. `notme config --explain` once. Keep the JSON for the whole run. Note `mode`,
   `router`, `models`, `labels`, `repos`, `reviewers`, `limits`, `notify`,
   `attribution` and the standards keys. Exit 4: result `FAILED - config invalid`.
2. `notme guard selftest`. Exit 8: stop, result `FAILED - guard not working`.
3. `notme preflight --push-check`. `enabled` false: `STOP - disabled`. `paused`:
   `STOP - paused`. Any other blocker: `FAILED` with the blocker text.
4. `notme status`. An `active_run` that is not this run: `STOP - active run`.
5. `--bg` in the arguments: read `${CLAUDE_PLUGIN_ROOT}/references/bg-mode.md`.
   Strip `--bg`, dispatch one foreground subagent on `models.bg` with this whole
   skill and the remaining arguments, print its single line exactly, and stop.

The result line, for `--bg` and as the last line of every run:

```
<VERB> <ref|-> <status>[ - note]  log=<path>
```

VERB is one of `NONE | PR | PUSHED | PARKED | HELD | REVIEWED | PROVED | FAILED | STOP`.
This skill ends in NONE, PR, HELD, FAILED or STOP.

## Step 1: Resolve the target

Read `${CLAUDE_PLUGIN_ROOT}/references/authority.md` before you read any issue
text. Follow `${CLAUDE_PLUGIN_ROOT}/skills/new/references/target.md` in full.

1. An issue number in `$ARGUMENTS` is the target.
2. Else resume: `notme log last --skill new`. A log whose summary says "committed
   but NOT PUSHED" names a branch; resume it at step 8.
3. Else `notme pick-issue`. `{"none": true}`: start a log with
   `notme log start --skill new --ref -`, finish it with status `idle`, and print
   `NONE - idle  log=<path>` (without `--bg`, after the `queue_summary` and any `reason`).
   Write nothing else. Otherwise choose from `issues` by the `picking` rules
   (by impact when none are set), as target.md "Order" says; never pick a `held_back` issue.
4. Read the issue only through `notme issue N`. `author_trusted` false: the body is
   withheld; never fetch it another way. See target.md for what to do.
5. Screen the trusted text against prohibitions.md. A prohibited request is refused
   before any claim (target.md, "Prohibited requests").

## Step 2: Claim and start the log

1. `notme log start --skill new --ref <tracker>#N`. Keep `path` and `run_id`.
   Exit 1 with kind `active_run`: `STOP - active run`. A queue pick: log the
   choice with `notme log action <path> --kind ACTION --message-file <file>`
   (target.md, "Order").
2. `notme claim-issue N --log <path>`. Exit 3 (claim lost): log it and go back to
   `notme pick-issue --exclude N` (repeat `--exclude` for each lost issue). An
   explicit number that is lost ends the run: `STOP <ref> claimed elsewhere`.

## Step 3: Classify and plan

Read `${CLAUDE_PLUGIN_ROOT}/references/branching.md`.

1. One-off issue, or a sub-issue of an `epic` (`parent` in the pick output or a
   trusted epic task list). Epic rules: target.md, "Epics".
2. Choose the base by the precedence in branching.md: `--base`, predecessor branch,
   `Base branch:` line, epic branch, default branch.
3. A declared base must exist on the remote. Check with a read:
   `git ls-remote --exit-code --heads origin <base>`. Missing: `notme release-issue N --reason-file <reason> --log <path>` (write the one-line reason first),
   add `needs_human` with `notme labels-sync N --add <name> --reason-file <reason>`, log the base and where
   it was declared, name the issue author, and stop:
   `STOP <ref> blocked - base <base> not found`. No branch is created.
4. Questions: interactive runs (no `--bg`) get one AskUserQuestion batch now and
   none later; under `--bg` AskUserQuestion is not available. Every question not
   asked or not answered becomes a safe default logged with `notme log question`
   (a JSON file with `question`, `decision`, `undo`).
5. Write the plan into the log with `notme log action`: files likely touched,
   acceptance criteria from the issue, verify commands to run.

## Step 4: Branch and Checkpoint A

1. Tree not clean (`clean` false in preflight): `notme git stash`, record the
   returned SHA in the log.
2. `notme git branch-create <name> --base origin/<base>`. The name comes from
   `standards.branch`, default `{issue}-{slug}`. In a companion set, the same name
   in every repo (`${CLAUDE_PLUGIN_ROOT}/references/companions.md`).
3. `notme git checkpoint --base <base> --fetch` (Checkpoint A). Conflicts you
   cannot resolve with confidence: abort the merge, log the files, release the
   issue with `needs_human`, `FAILED <ref> merge-conflict`.

## Step 5: Delegate the implementation

Read `${CLAUDE_PLUGIN_ROOT}/references/delegation.md`.

1. `models.strategy` `single`: use `models.single_model` for every dispatch.
2. Else classify the unit. A path or kind under `## Delegation` in standards, or
   anything security-sensitive, goes to `models.review`; the rest to
   `models.implement`.
3. Always dispatch the implementation to a subagent, even for a one-line change.
   This overrides any operator preference about delegation size. Brief one subagent with goal, files, conventions, scope ("no commits, no
   pushes, no posts"), and acceptance from the issue. Include the failing test
   first rule. Log `DELEGATE <model> unit="..." try=1` with `notme log action
   --kind DELEGATE`.
4. Audit yourself: read the diff, check scope, run the project's own test and lint
   commands from verify (step 6). Log `AUDIT pass|fail ...` with `--kind AUDIT`.
5. Retry ladder from delegation.md, capped at `models.max_tries`, then do the unit
   yourself. Never read the subagent's transcript.

## Step 6: Verify and gate

Read `${CLAUDE_PLUGIN_ROOT}/references/verification.md` and
`${CLAUDE_PLUGIN_ROOT}/references/gate.md`. Commands come only from the body of
the effective verify doc; never invent a test or lint command.

1. Baseline before any edit: run `## Baseline` (or the test and lint sections);
   log existing failures as `CHECK` lines.
2. Reproduce a bug on the base before fixing. Cannot reproduce, or already fixed
   on the default branch: HELD (failure table). Do not guess at a fix.
3. Red then green for every new behaviour.
4. UI diff: one `Visual check:` line (path form in the log, words form in the
   PR), and the light accessibility pass.
5. Gate: tests red then green, `/simplify` when over about 20 changed lines, the
   code review (the `pr-review-toolkit` agent, or a general-purpose subagent when
   it is not installed), the security review as a foreground general-purpose
   subagent, CI-parity lint on the whole tree, final tests, then each step under
   `## Gate additions`. Log each as `GATE`. Fix findings in this run (gate.md).
6. Cross-model claim check on `models.verifier` before posting; verdicts name the model.

## Step 7: Commit

1. Stage only the paths this unit touched, by name. Never a blanket add.
2. Message per `standards.commit` (`conventional`: `fix(<N>): ...` or
   `feat(<N>): ...`, scope is the issue number, lowercase present tense).
3. Write the message to a file. When `attribution` is text, end the message with
   that line; `inherit` adds nothing; `none` adds nothing at all.
4. Commit locally from the file:

<!-- allowed-raw-write -->
```bash
git commit -F "$msgdir/commit-msg.txt"
```

## Step 8: Checkpoint B, push, CI

1. `notme git checkpoint --base <base> --fetch` (Checkpoint B). New commits came
   in: run the final tests again.
2. Push the branch through the CLI only, companions first and the primary last
   (companions.md):

<!-- allowed-raw-write -->
```bash
notme git push <branch> --log <path>
```

3. A failed push after a commit: the log summary must say "committed but NOT
   PUSHED" with the branch name, so the next run resumes it.
4. CI: after the PR exists (step 9), poll `notme bundle <repo>#<pr>` until the
   `checks` for `head_sha` finish, up to `limits.ci_wait_minutes`. Red: read the
   failing job, fix, rerun locally, commit, push. Stop at `limits.fix_loop`
   rounds; then park (failure table).

## Step 9: Open the PR

Read `${CLAUDE_PLUGIN_ROOT}/references/pr-shape.md` and
`${CLAUDE_PLUGIN_ROOT}/references/companions.md`. Follow
`${CLAUDE_PLUGIN_ROOT}/skills/new/references/pr-assembly.md`.

1. Title file from `standards.pr_title`; body file with Summary, Testing steps,
   Companion PRs when there is a set, References and the closing keyword.
2. `notme check-text --file <body>` must pass before opening.
3. `notme pr-open --head <branch> --base <base> --title-file <t> --body-file <b>
   --assignee <operator> --log <path>`, adding `--labels` and `--reviewers` only
   on the PR that completes the set. Never `--draft`.
4. Companion set: `notme companion-prs` on every PR once all exist.
5. Move the issue with a one-line reason file (for example `Opened <repo>#<pr>`): `notme move-issue N
   --to needs_review --require-current in_progress --reason-file <reason> --log <path>`.
6. One comment on the issue, so a person reading it sees the run without opening
   the PR: write `Opened <repo>#<pr> for this issue.` plus the testing steps'
   one-line status to a file and run `notme comment-upsert <tracker>#N --marker
   new --body-file <f> --log <path>`. Upserted, so a rerun edits it rather than
   adding another. The PR body carries the detail; this comment is the pointer.

## Step 10: Finish

1. Return the checkout to the branch it started on (`branch` from preflight; in
   a linked worktree that is never the default branch) with a clean tree
   (`branching.md`). Restore a stash with `notme git unstash <sha>`. A tree you
   cannot clean is a `WARN`.
2. `notme log finish <path> --status "PR <repo>#<pr> needs review"`.
3. `notme prove-target <repo>#<pr>` finds a URL: start `/not-me:prove <repo>#<pr>`
   in the background. No target: skip it.
4. `notify.command` set and the result is not NONE: run it with the result line
   and the log path.
5. Print the result line: `PR <repo>#<pr> opened  log=<path>`.

## Failure handling

The full table is in `${CLAUDE_PLUGIN_ROOT}/skills/new/references/failures.md`.
The short form:

| Situation | What happens | Result |
|-----------|--------------|--------|
| Nothing picked | No writes | `NONE - idle` |
| Disabled, paused, active run | No writes | `STOP` |
| Guard selftest fails | No writes | `FAILED - guard not working` |
| Prohibited request | Refusal comment, `needs_human`, no claim | `STOP <ref> refused` |
| Declared base missing | Release, `needs_human` | `STOP <ref> blocked` |
| Cannot reproduce, already fixed | Keep `in_progress`, add `needs_human`, comment | `HELD <ref> ...` |
| Guard block mid-run | QUESTION naming the operator, `needs_human` | `FAILED <ref> blocked` |
| Mechanical failure before a push | `notme release-issue N --reason-file <reason> --log <path>`, `needs_human` | `FAILED <ref> ...` |
| CI red at `limits.fix_loop` | Park the PR with `needs_human` | `FAILED <ref> ci-red` |

Every comment this skill owns is upserted with
`notme comment-upsert <tracker>#N --marker new --body-file <f>`, which carries the
marker `<!-- not-me:new -->`. Never fake green: no skipped tests, no disabled
checks. A clearly reported blocker is a correct outcome.

## Checklist

- [ ] Read the config once, ran the guard selftest, preflight and status first.
- [ ] Under `--bg`, dispatched one subagent on `models.bg` and printed its one line.
- [ ] Read issue text only through `notme issue`, trusted authors only.
- [ ] Refused prohibited requests before claiming.
- [ ] Claimed through the CLI and passed `--log` on every write.
- [ ] Stopped on a declared base that does not exist.
- [ ] Delegated the unit, audited it myself, logged DELEGATE and AUDIT.
- [ ] Ran baseline, reproduction, red then green, and the full gate.
- [ ] Committed only touched paths; pushed only through the CLI.
- [ ] Opened the PR ready for review, moved the issue to `needs_review`, and left
      one upserted `new` marker comment on the issue naming the PR.
- [ ] Left the checkout clean on its starting branch and finished the log.
