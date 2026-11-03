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
   name a tracker, companion repos and a board, and the write gate trusts
   those once the same names are in `.claude/not-me/project.md` as committed
   on the default branch (the commit `git ls-remote origin HEAD` reports,
   read from the local object store when it is already there; never the
   working tree or a local ref, which a session can move; the hook does not
   ask the remote), or once the operator allowed
   them with `/not-me:allow-repo` (owner, 2026-10-30,
   dblanken-yale/not-me#118). Reason: a PR branch or a compromised repo file
   must not be able to widen what the plugin may touch; a file reaches the
   default branch only through a reviewed merge, so that review is the
   approval. (`notme/layers.py` `tighten_only`, `notme/writes.py`
   `default_branch_names`.) Since the guard only guides writes to other repos
   (decision 3), the CLI's write gate is what holds this line for runs.

2. **Every GitHub write goes through one gated path.** `GhClient.write` →
   `writes.check_write`. Reads are an explicit allow-list; disguised
   mutations are refused rather than run. Labels are set with a full-set PUT
   and read back (never add+remove in one `gh edit`, which drops labels).
   Thread replies use REST and never resolve threads. Pushes send an explicit
   refspec, verify every push URL, and never force.

3. **The guard guides; it hard-blocks only what breaks the process or cannot
   be undone** (owner, 2026-10-30, dblanken-yale/not-me#141). Three hard rule
   groups (protected branches, force push, destructive actions), each relaxable
   only with `confirm_guard_off` in the same layer and never from the repo
   layer. They keep a short list (`docs/guards.md`, with the audit table):
   commits, merges, rebases, cherry-picks, am and hard resets on a protected
   branch and pushes to one, including pushes whose target it cannot see and
   API ref or contents writes to one; force pushes and remote deletes; GitHub
   settings, secrets, rulesets, releases and deletions, `rm -rf .git`,
   `clean -x` and forced deletes of a protected branch. Everything else is a
   process rule in `references/prohibitions.md`: the guard lets the call run
   and logs it to `guard.jsonl` as `guided`. Guided today: git and gh writes to
   repos off the allowed list and changes to where git pushes (`other_repo`,
   whose key also switches the CLI write gate's repo check), and repo-layer
   config edits during a run (`process`). An issue or comment on another repo is fine when the
   operator asked in the session, and always on the plugin's own repo; runs
   stay on the project's repos through the CLI's write gate. GitHub MCP tools
   get the hard rules only, other MCP tools are not judged. Text it does not
   parse (a `bash -c` body too deep, inline code, a script file, a command it cannot lex) is allowed unless it holds one
   of a few words (`push`, `reset --hard`, a GitHub secrets action, a protected
   path name and the like), then blocked under the first relevant hard rule
   that is on. Self-protection is always on: the allow list file by path; a
   copy, move, link or extract into the not-me home (or, recursive, into a
   directory above it); a Write or Edit that leaves the global `mode` other
   than dry-run, and any shell write of the global `project.md` (reads
   pass); the plugin's own files; the global config during a run. The not-me CLI itself
   is not judged: `allow-repo add|remove`, `pause off` and `mode live` are
   the operator's slash commands, a process rule in `prohibitions.md`
   (decision 4). Keywords in
   interpreter code that starts no process and names no git, gh, notme or
   GitHub API are guided, not blocked. The inactive path
   is fork-free. Block messages say what to do next in the session and never
   send the operator to a terminal. For git and gh it follows only a leading
   `cd <path> &&` and variables set to a literal path in the same command;
   any other directory change next to a git write that reads the branch is
   blocked (fail closed), and a `git -C` path it cannot work out is guided,
   never judged as the session's (dblanken-yale/not-me#144). It is not the
   last line: GitHub rulesets on
   protected branches (no deletion, no force push, PR required) are the
   backstop the docs recommend.

4. **The CLI's trust anchor is the checkout it runs in.** Restricted mode is
   the default: a `--project-dir` or `CLAUDE_PROJECT_DIR` naming another
   checkout is refused and `NOT_ME_HOME` is ignored. It relaxes only when
   stdin is a terminal and no ancestor process is Claude Code (the desktop
   app's terminal panel counts as Claude Code). Env vars never relax
   anything; `NOT_ME_DRY_RUN` can only turn dry-run on. Changing the allow
   list, the pause and the mode is not gated by this check (owner,
   2026-10-30, dblanken-yale/not-me#118): `/not-me:allow-repo`,
   `/not-me:pause` and `/not-me:mode` are user-invocable only
   (`disable-model-invocation: true`), so a person typing the slash command
   is the approval, and the skill runs the CLI in the session. No operator
   token, no terminal hand-back. `references/prohibitions.md` forbids a run
   from issuing those commands unless the operator invoked the matching
   slash command in that turn; autonomous runs log a QUESTION instead. Every
   such change prints what changed (repo or board, this project or every
   project) and appends a line to `<not-me home>/settings-changes.jsonl`, so
   the operator can see and undo it.

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

7. **Mode defaults to dry-run; the operator turns live on.** A fresh install
   writes only to the ledger. `mode: live` in the global layer is enough
   (owner, 2026-10-30, dblanken-yale/not-me#118: the signed live marker, its
   epoch and the loader's check are gone). The operator sets it with
   `/not-me:mode live`, which runs `notme mode live` in the session (decision
   4). The repo layer can never set `mode: live`; it can only pin dry-run.
   The guard refuses a Write or Edit that would leave the global `mode`
   other than dry-run, so the change goes through the command that shows
   and logs it. Logs and the
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
- The README's "How a run works" mermaid diagram shows what a run does, which
  steps are optional, and which setting changes each one. When a change adds,
  removes or reorders a step, makes one optional or required, or adds, renames
  or removes the config key or doc that steers it, update the diagram in the
  same PR. `docs/workflow.md` keeps the label state diagram; update that one
  when label transitions change.
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
