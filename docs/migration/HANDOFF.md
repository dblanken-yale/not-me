# Handoff: finishing not-me

Written 2026-10-22 at the end of the first build day, for the next orchestrating
session (expected to be Claude Opus) and for Dave. Read this, then `CLAUDE.md`,
then the plan file named below. Everything else is reachable from those three.
It lives under `docs/migration/` because it names the source repositories, which
the plugin's own files may not (lint rule R5).

## Where things live

| Thing | Path |
|-------|------|
| This repo (public) | `~/code/not-me`, GitHub `dblanken-yale/not-me`, branch `main` |
| Approved plan with every decision | `~/.claude/plans/can-you-create-a-greedy-lark.md` (read its "Revision 2026-10-21 evening" section first; it supersedes the eval parts below it) |
| Decisions that must survive edits | `CLAUDE.md` in this repo |
| Memory notes from the first session | `~/.claude/projects/.../memory/` entries `not-me-plugin-project`, `merge-prs-as-they-pass`, `no-long-lived-claude-token` |
| Model-routing rules the build followed | `~/.claude/trials/model-routing.md`; log in `~/.claude/trials/model-routing-log.tsv` |
| Fixture repos (private, throwaway) | `dblanken-yale/not-me-fixture`, `dblanken-yale/not-me-fixture-companion` |
| Live run results | `results/evals/<date>/<ID>-<n>.json` (gitignored) |
| The unrun U0 spike | `.spike/run.sh` (untracked) |

## What not-me is, in one paragraph

A Claude Code plugin (marketplace and plugin in one repo) that runs an autonomous
dev loop in any GitHub repository: `/not-me:new` picks a ready issue and opens a
PR, `/not-me:update` reworks a PR labeled `needs work`, `/not-me:review` reviews
PRs labeled `needs review`, `/not-me:prove` runs a PR's testing steps on its
deploy target, `/not-me` routes between them, and `setup`, `config`, `doctor`,
`status`, `ticket`, `retro` and `allow-repo` support them. It replaces three
bespoke skill families Dave uses daily (YaleSites `not_dave*`, palliative-care-rag
`pcr-*`, YDS-Web-Component `wc_dave*`). Mechanics live in a stdlib-only Python
CLI (`notme`), judgment stays in the model, and a PreToolUse guard hook fails
closed. Project specifics live in markdown config docs with three layers
(plugin defaults, `~/.claude/not-me/`, repo `.claude/not-me/`).

## State at handoff

- 37 PRs merged (number 37 was merging on green when this was written; check
  `gh pr list` for anything still open). `main` carries every module, the CLI,
  12 skills, 13 references, user docs, the fixture seeder, 43 eval cases and the
  session-driven proof harness.
- Checks on `main`: `python3 scripts/run-tests.py` 1168 tests OK;
  `python3 plugins/not-me/tests/run_guard_cases.py` 774/774;
  `scripts/check-skills.py`, `scripts/lint-evals.py`, `scripts/gen-cli-docs.py
  --check`, `claude plugin validate --strict plugins/not-me` all clean. CI runs
  the same on macOS, Linux and Windows with Python 3.9 and 3.13.
- Live proof so far (first day, from Dave's terminal):
  - N2 (nothing to do): passed end to end, all graders and state assertions, $0.90.
  - N1 (happy path): the skill claimed the issue, fixed the bug, pushed one
    conventional commit, opened a PR, moved the issue to `needs review`; every
    safety check and transcript grader passed. Two outcome checks failed and
    were fixed in PR 37 (bare `Closes #N`, no marker comment on the issue). N1
    has not been rerun since PR 37.
  - Nothing else has run live yet.

## Decisions made on day one that are not obvious from the code

Each of these was decided by Dave or forced by something we hit. Do not reverse
them without asking him.

1. **No long-lived Claude token, ever.** `claude plugin eval` runs cases in a
   fresh sandbox home that cannot see the interactive login; the only way to run
   it was `claude setup-token`, which Dave refused. So `claude plugin eval` was
   removed from the repo. Proof runs are a real Claude Code session with Dave's
   own login, launched by `evals-harness/run_live.py`. Never suggest
   `setup-token`, `CLAUDE_CODE_OAUTH_TOKEN` or an API key as a fix for anything.
2. **The runner swaps `~/.claude/not-me` for the duration of a run.** Inside a
   session the `notme` CLI ignores `NOT_ME_HOME` (deliberate, decision 4 in
   `CLAUDE.md`), so the scenario's global layer must sit in the real directory.
   The runner renames the real one to `~/.claude/not-me.backup-<RID>` and
   restores it afterwards. Restore never deletes: the run's layer is renamed to
   `~/.claude/not-me.replaced-<RID>` and the message names it. This went through
   three security review rounds; the invariant "nothing under `~/.claude` is ever
   deleted or overwritten by `run_live.py`" is what every reviewer checked. Keep
   it.
3. **The fixture PAT must be granted only the two fixture repos.** The plugin
   repo is public, so a 404 probe cannot prove that and GitHub's `permissions`
   field on a public repo reports the account's role, not the token's grant.
   `scaffold.py check_token` therefore reads `gh api user/repos` (a fine-grained
   token lists only the private repos in its grant), calibrates a no-op label
   PATCH on the fixtures (must be 404, authorised) and requires the same PATCH
   on the plugin repo to fail with 403 "Resource not accessible by personal
   access token". The token reaches `gh` only through `GH_TOKEN` in the
   environment, never argv.
4. **Git credentials inside the session come from a file, not the environment.**
   Claude Code sets `GIT_CONFIG_COUNT`/`KEY_*`/`VALUE_*` for its own git proxy
   and displaced the helper the runner had put there. The helper now lives in
   the run's `GIT_CONFIG_GLOBAL` file and in the workspace's repo-local config.
5. **A pickable issue may be assigned to the operator.** The queue filter
   refused any assignee; the claim step and the design only refuse someone
   else's. A lead assigning work to you before labeling it is the normal case.
6. **Closing references are always `owner/repo#N`**, never a bare `#N`, even in
   the same repository. GitHub honours the qualified form. This matches Dave's
   global standard for every issue and PR reference.
7. **The happy path of `new` leaves one upserted `new` marker comment on the
   issue naming the PR**, so a person reading the issue sees the run happened.
8. Everything in `CLAUDE.md` "Decisions that must survive edits" (layers that
   only tighten, one gated write path, fail-closed guard, trust anchor, untrusted
   text never reaches the model, compare-and-set claims, dry-run default, no
   project names in the plugin, abstract labels, CLI for every GitHub action,
   merging is a human action). A person can hand one named merge to an
   interactive session with `/not-me:allow-merge owner/repo#N` (a
   UserPromptSubmit hook writes the grant; the guard spends it once); runs
   never merge. Design in `docs/guards.md` "Merge grants".

## How to work here (the orchestration loop that built this)

Dave orchestrates nothing; the session orchestrates and delegates every unit
of real work to a subagent, then validates the result itself.

1. Pick the unit. One feature branch per unit (`feat/...`, `fix/...`), based on
   `main`. For parallel units use worktrees under `~/code/not-me-wt/<name>`
   (`git worktree add --no-track -b <branch> <path> origin/main`; the main
   checkout's `.git/config` cannot be locked from the sandbox, so pass
   `--no-track` and `git pull origin main` explicitly).
2. Route the model per `~/.claude/trials/model-routing.md`: Sonnet for tooling,
   scripts, docs, CLI modules and tests; Opus for guard, merge semantics, prose
   that encodes judgment, eval design, security-sensitive code; Haiku for
   read-only triage. Pass `model` explicitly on every dispatch.
3. Write a self-contained brief: goal, exact file paths, conventions (Python 3.9
   stdlib, unittest, portable to Windows, commit messages end `Created with
   AI`), explicit scope ("touch only X"), acceptance criteria, and the checks to
   run. Subagents inherit no context. Tell them not to push.
4. Validate yourself: run the checks listed in `CLAUDE.md` "How to work here",
   read the diff, compare to the acceptance criteria. Never trust the summary.
5. Anything touching gh/git calls, file handling, the guard or the runner gets a
   security review by a fresh Opus agent (read-only brief: "flag correctness
   bugs and missed requirements only; file:line plus a one-sentence fix"). A
   second verification pass after the fix round caught real bugs twice on day
   one; do it.
6. Fix loop: Sonnet gets two tries, then a fresh Opus agent with both failed
   attempts in the brief; after an Opus failure, stop and ask Dave with what
   failed, what was tried, and a recommendation.
7. Push, open the PR (description ends `Created with AI`, references in full
   `owner/repo#N` form), and merge it with `gh pr merge N --squash` the moment
   CI is green (no `--delete-branch`, Dave's policy hook blocks it). Then pull
   `main` and rebuild any open branch on it (cherry-pick its own commits onto
   `origin/main`; squash merges make stacked branches conflict). GitHub runs no
   PR workflows while a PR is `CONFLICTING`.
8. Log every coding unit to `~/.claude/trials/model-routing-log.tsv` (format in
   the routing file).
9. Keep `CLAUDE.md`, this file and the plan file true when a decision changes.

CI quirks already learned: Windows runners need `SYSTEMROOT` passed to child
Pythons and a local git identity; `pathlib.Path.glob` raises on absolute
patterns on Windows; shell-quoted paths in dry-run output; `CLAUDE.md` is
globally gitignored on Dave's machine (`git add -f`).

## How to test

Everything below the live runs is runnable from inside a Claude Code session.

```bash
cd ~/code/not-me
python3 scripts/run-tests.py                      # unit + harness + docs tests
python3 plugins/not-me/tests/run_guard_cases.py   # 774 hook table cases
python3 scripts/check-skills.py                   # skill lint R1-R14
python3 scripts/lint-evals.py                     # case lint E1-E6
python3 scripts/gen-cli-docs.py --check           # docs/cli.md in sync
claude plugin validate --strict plugins/not-me
```

Live proof runs need Dave's terminal (his Claude login, outside any sandbox):

```bash
cd ~/code/not-me && git checkout main && git pull origin main
NOT_ME_FIXTURE_TOKEN=<fine-grained PAT for the two fixture repos only> \
  bash scripts/eval-live.sh --owner dblanken-yale \
  --repo dblanken-yale/not-me-fixture \
  --companion dblanken-yale/not-me-fixture-companion \
  --skill new --scenario N1 --runs 1
```

What a run does, in order: seed the fixture (reset to baseline, create the
scenario's issues and PRs, write a manifest), check the token, clone and stage
the workspace, move `~/.claude/not-me` aside, launch `claude -p '/not-me:new
--bg' --plugin-dir plugins/not-me --output-format json --allowedTools ...` in
the workspace, grade the session transcript against
`evals-harness/cases/<skill>-<ID>/graders/*.md`, grade the GitHub state against
the scenario's `expect` block, clean up, restore the global layer, tear the
fixture down. Result file: `results/evals/<date>/<ID>-<n>.json` with `ok`,
`graders`, `grade`, `session` (session id, turns, cost) and `errors`.
`--launch interactive` runs plain `claude` in the foreground instead, for
watching a run or answering permission prompts. `docs/testing.md` has the full
description, the token recipe, and what to do after a dead run
(`run_live.py --restore-global RID`).

Close any other not-me session before a live run: while it runs, every session
on the machine reads the scenario's global layer.

A run that fails has three places to look: the result JSON's `errors`, the
transcript graders table (`graders`), and the state table (`grade`). The
session transcript itself is at
`~/.claude/projects/<workspace path with non-alphanumerics as "-">/<session id>.jsonl`,
with subagent transcripts beside it under `<session id>/subagents/`. Reading
the Bash tool calls and results in it is how both day-one N1 failures were
diagnosed in minutes.

Scenario IDs and what they cover are in `fixture/scenarios/*.json` and
`fixture/README.md`; the plan file lists all of them with intent. Scenarios
present on day one: C1 C3 C6 C8 C10, D1, E1 E3, K1-K6, M1, N1 N2 N3 N4 N6 N7 N8
N12, P1 P4, R1 R2 R3 R5 R10, S1 S3, T1 T2, U1 U2 U3 U4 U5 U9 U10, X1 X2.

## What is still to do, in order

1. **Rerun N1 on `main`** after PR 37. Expected: `ok=True`. If the marker
   comment or closing reference still fails, read the subagent transcript for
   the `pr-open` and `comment-upsert` calls.
2. **Run the rest of the `new` scenarios** (N3 N4 N6 N7 N8 N12) one at a time.
   Each first run will probably surface one skill or harness defect, as N1 and
   N2 did; fix each on its own branch, merge, rerun.
3. **`update` scenarios** U1 U2 U3 U4 U5 U9 U10, then `review` R1 R2 R3 R5 R10,
   `prove` P1 P4 (needs a deploy target; the fixture serves `site/` with
   `python3 -m http.server`, see `fixture/README.md`), router X1 X2, control
   K1-K6 (K4 is two sessions racing one issue; the runner has no two-session
   mode yet, so K4 needs either a second manual session or a small runner
   extension), companions M1, setup S1 S3, config C*, ticket T1 T2, retro E1
   E3, delegation D1.
4. **Known gaps to close as they come up:**
   - The `--bg` contract says the final message is exactly one line, but
     `claude -p` returns the whole final turn and the skill sometimes prints
     prose above the result line. Add a grader for "last message is one line"
     and tighten `references/bg-mode.md` once a happy path is stable.
   - `llm` graders are skipped (no judge). Quality class is advisory anyway.
   - An empty file named `bash` appeared in the repo root during the first
     session-driven run, created by the session. It was deleted; if it comes
     back, find the command that writes it.
   - The `--restore-global` liveness check assumes a pid start time can be
     read; Windows and Linux readers are covered only by CI unit tests.
5. **U0 spike**: `bash .spike/run.sh` from Dave's terminal, both CLI and
   desktop. It checks `${CLAUDE_PLUGIN_ROOT}` substitution, bare `/not-me`,
   `!` injection versus the hook, exec-form hook fail-open, desktop auto mode
   surfacing guard blocks, and subagent spawn depth. Record the answers in
   `docs/spikes/U0.md` and adjust whatever they contradict.
6. **G0 gate**: six runs per scenario over at least two days meeting the class
   thresholds (safety 100%, outcome at least 5 of 6, happy paths 6 of 6);
   `evals-harness/gate_report.py` writes `docs/gates/G0-<date>.md` from
   `results/evals/`. Also: coverage, doctor on Dave's Mac and in
   `python:3.9-slim`, an adversarial guard review by the other model family.
7. **Release 0.1.0** through release-please (a `feat:` or `fix:` PR to `main`
   opens the release PR; merging it tags and publishes). `docs/release.md`.
8. **Migration, pcr first** (then wc, then YaleSites): `/not-me:setup
   --import`, the `notme import-legacy` extractors over frozen snapshots in
   `tests/fixtures/legacy/`, shadow period with `shadow_compare.py`, cutover,
   rollback plan. None of this exists yet; the plan file's "Phase 4" and
   "Proof gate and rollout" sections specify it.
9. Housekeeping: delete the `~/.claude/not-me.replaced-*` directories once
   inspected; `git worktree prune` and remove stale `~/code/not-me-wt/*`
   directories (needs Dave's terminal; the sandbox cannot write
   `.git/worktrees`); Dave revokes the day-one fixture PAT and makes a fresh one
   per session.

## Things an orchestrator new to this repo gets wrong

- Reading `~/.claude/not-me/` or writing to it from a session during a live run.
  The runner owns that directory for the run's duration.
- Routing around the guard when it blocks something during development. The
  block message names the allow command; the block is usually correct.
- Letting a subagent push, merge or open PRs. The orchestrator does those, so the
  loop stays auditable.
- Treating an agent's "all fixed" as verified. Run the checks and read the diff.
- Hand-editing `docs/cli.md`, `CHANGELOG.md` or `version` (generated or owned by
  release-please).
- Adding any Yale, YaleSites, Pantheon, Lando or personal-path string anywhere
  outside `docs/migration/`, `tests/fixtures/legacy/` and `fixture/` (lint R5
  fails the build).
- Polling CI with `sleep` loops in the foreground; run the watcher in the
  background and act on its notification.
