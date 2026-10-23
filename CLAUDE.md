# CLAUDE.md

Working notes for anyone (person or model) changing this repository. Read this
before editing. It records what not-me is, the decisions behind it, and the
invariants that must not be reverted by accident. The full design history is
in the PR descriptions on GitHub; this file is the short form.

## What this is

not-me is a Claude Code plugin (marketplace + plugin in one repo) that runs an
autonomous dev loop in any GitHub repository: pick up a ready issue and open a
PR, rework a PR labeled `needs work`, review PRs labeled `needs review`, prove a
PR on a deploy target, and a few helper commands. It replaces three bespoke
skill families that did the same job in three repositories; the goal is one
plugin another developer can install and succeed with, with every
project-specific detail living in that project's own config docs.

Layout:

- `plugins/not-me/notme/` — stdlib-only Python package (3.9+). All GitHub and
  git mechanics live here. "Gathers, never decides": it returns JSON, judgment
  stays in the model.
- `plugins/not-me/cli/notme.py` + `bin/notme` — the `notme` CLI the skills call.
- `plugins/not-me/hooks/` — the PreToolUse guard (`repo_guard.py`) and
  `hooks.json`.
- `plugins/not-me/defaults/*.md` — layer 1 of the config and the single source
  of every default. No default lives in Python; a test enforces this.
- `plugins/not-me/references/*.md` — operating rules the skills tell the model
  to read mid-run.
- `plugins/not-me/skills/*/SKILL.md` — the skills themselves.
- `fixture/` — contents and seeder for the throwaway eval repositories.
- `docs/` — user guides. `docs/cli.md` is generated; do not hand-edit it.

## Decisions that must survive edits

These were decided deliberately. Changing one is a design change that needs
the owner's say-so, not a cleanup.

1. **Three config layers, repo may only tighten.** Plugin defaults < user
   global `~/.claude/not-me/` < repo `.claude/not-me/`. Frontmatter keys merge
   per key; union lists (`protected_branches`, `allow_repos`, `markers.legacy`)
   only grow. The repo layer cannot turn a guard rule off, set `mode: live`,
   enable approval, shorten `protected_branches`, or add `allow_repos`; it can
   name companion repos and a board, but the write gate trusts those only once
   the operator confirmed them in the global allow list. Reason: a PR branch
   or a compromised repo file must not be able to widen what the plugin may
   touch. (`notme/layers.py` `tighten_only`, `notme/writes.py`.)

2. **Every GitHub write goes through one gated path.** `GhClient.write` →
   `writes.check_write`. Reads are an explicit allow-list; disguised
   mutations are refused rather than run. Labels are set with a full-set PUT
   and read back (never add+remove in one `gh edit`, which drops labels).
   Thread replies use REST and never resolve threads. Pushes send an explicit
   refspec, verify every push URL, and never force.

3. **The guard is small and cannot be widened by the model.** Four rule groups
   (other repo, protected branches, force push, destructive actions), each
   relaxable only with `confirm_guard_off` in the same layer and never from the
   repo layer. It keeps a short list (`docs/guards.md`): commits, merges,
   rebases, cherry-picks, am and hard resets on a protected branch and pushes to
   one; force pushes and remote deletes; git and gh writes to repos off the
   allowed list and anything that changes where git pushes; GitHub settings,
   secrets, rulesets, releases and deletions, `rm -rf .git`, `clean -x` and
   forced deletes of a protected branch. Text it does not parse (a `bash -c`
   body too deep, inline code, a script file, a command it cannot lex) is
   allowed unless it holds one of a few words (`push`, `reset --hard`, a GitHub
   secrets action, a protected path name and the like), then blocked under the first relevant rule
   that is on. Self-protection is always on: the operator token, the live-mode
   epoch and marker and the allow list by path;
   `notme allow-repo add|remove`, `pause off` and `mode` other than show or
   dry-run as parsed commands in every invocation form; a pseudo-terminal or
   detached wrapper around notme; the plugin's own files; the config during a
   run. The inactive path is fork-free. Block messages name the exact
   `/not-me:allow-repo owner/name` command. The guard does not follow `cd`, and
   it is not the last line: GitHub rulesets on protected branches (no deletion,
   no force push, PR required) are the backstop the docs recommend.

4. **The CLI's trust anchor is the checkout it runs in.** Restricted mode is
   the default. It relaxes only when all of these hold: stdin is a terminal,
   no ancestor process is Claude Code (the desktop app's terminal panel
   counts as Claude Code), and, for allow-list changes and turning the pause
   off, `NOT_ME_OPERATOR_CONFIRM` equals the random token in
   `~/.claude/not-me/operator-token` that `notme doctor --init-token` creates
   for a person. Env vars never relax anything; `NOT_ME_DRY_RUN` can only
   turn dry-run on. The guard blocks pseudo-terminal and detached wrappers
   around notme and reads of the token so these checks cannot be spoofed
   from a session; the CLI checks are the second layer, the guard is the
   first.

5. **Untrusted text never reaches the model as instructions.** Authors are
   filtered by login against the roster before any body is read; only counts,
   logins and URLs of untrusted items are reported, and quoted lines inside
   trusted replies are stripped. The one exception: a roster member's PR
   title and body reach the reviewer (`bundle --review`), flagged as data
   under review, never instructions. An endorsement is a flag on a trusted
   comment, never a binding to the untrusted text. Markers are honoured only
   when the real author matches the marker's login and is trusted.

6. **Claims are compare-and-set on issue metadata**, read back through the
   whole timeline, and fail closed when our own event cannot be found. Stale
   claims are reported, never auto-released.

7. **Mode defaults to dry-run; live is signed by a person.** A fresh install
   writes only to the ledger. Live needs `notme mode live`, run by a person
   (decision 4's checks): it sets `mode: live` in the global layer and writes
   `<not-me home>/mode-live.sig`, an HMAC keyed by the operator token plus
   the random epoch in `<not-me home>/mode-live.epoch` (key = token, NUL,
   epoch) over a versioned message and the resolved home. The config loader
   honours live only when that marker verifies (`notme/config.py`
   `_confirm_live`); otherwise the effective mode is dry-run with a warning,
   so no file edit can turn live on, and a new token means going live again.
   Going back to dry-run in the global layer deletes the marker and replaces
   the epoch, so a marker copy saved while live never verifies again; the
   loader does the same whenever the global layer's own `mode` is not `live`
   (a hand edit included; a repo-layer pin does not revoke). The guard's
   Edit/Write check on the global `mode` is UX, not the boundary. Logs and the
   active-run marker live under `<git-common-dir>/not-me/` and never move
   with a configured log root, so the guard can always find a running run.

8. **No project-specific names in the plugin.** No org, person, hosting or
   product names from the original repositories appear in code, defaults,
   references, docs or tests outside `docs/migration/`,
   `tests/fixtures/legacy/` and `fixture/`. `scripts/check-skills.py` rule R5
   enforces a denylist.

9. **Labels are abstract and mappable.** Code uses the eleven abstract states
   in `defaults/project.md` `labels:`; the repo label name is a mapping a team
   can change. Two abstract states mapping to one name is a config error.

10. **Skills call the CLI for every GitHub action.** No raw `gh` or `git`
    writes in skill prose (lint R6). Text goes to GitHub through files, never
    shell arguments. The `--bg` result is exactly one line in the grammar in
    `references/bg-mode.md`; `NONE` means only "nothing to do".

11. **Merging is a person's call, never a skill's.** No not-me skill merges,
    and merging is not part of any skill's flow; autonomous runs never merge.
    A person merges, by hand or by asking Claude directly in a session. The
    guard does not block merges, so this rule lives in the skills and
    `references/prohibitions.md`, backed by GitHub rulesets where a repo wants
    a review on every merge. Nothing here deletes branches or changes
    repository settings, even when a lead asks.

## How to work here

- One feature branch per unit, Conventional Commit titles, squash-merge, and
  merge each PR as soon as it is green. release-please owns `version` and
  `CHANGELOG.md`; never hand-edit them.
- Before committing: run `python3 scripts/run-tests.py`,
  `python3 plugins/not-me/tests/run_guard_cases.py`,
  `python3 scripts/check-skills.py`, `python3 scripts/gen-cli-docs.py --check`,
  and `claude plugin validate --strict plugins/not-me`. CI runs them on
  macOS, Linux and Windows with Python 3.9 and 3.13; keep code portable
  (`pathlib`, no POSIX-only calls, guard mode and symlink assertions).
- Changes that touch gh/git calls, file handling or the guard get a security
  review. Reviewers flag correctness and missed requirements; style is
  optional.
- This repo's own not-me repo layer lives in `.claude/not-me/` (pinned
  verify commands, extra delegation paths). It is committed, but a global
  gitignore that ignores `.claude/` will skip it: stage it with
  `git add -f .claude/not-me/<file>`. The CLI reads it only from the main
  checkout, so an edit in a worktree takes effect once merged and pulled.
- When the guard blocks something during development, that is usually the
  guard working. Do not route around it; the allow command is in the message.
- Commit messages and PR descriptions end with `Created with AI`. Nothing
  else in the repo mentions AI involvement; the plugin's own posts follow the
  configurable `attribution` policy.

## Where the context lives

- Start of a new orchestrating session: `docs/migration/HANDOFF.md` (what is
  done, what is next, the decisions that are not obvious from the code, how to
  test). Keep it true when any of that changes.
- Design plan and the reasoning behind each unit: the PR descriptions in this
  repository, in number order.
- Operating rules the model follows at run time: `plugins/not-me/references/`.
- User-facing behaviour: `docs/`.
- Eval scenarios and the fixture contract: `fixture/README.md` and
  `fixture/scenarios/`.
