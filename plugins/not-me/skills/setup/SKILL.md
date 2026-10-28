---
name: setup
description: Interactive setup for not-me. Detects the repo, labels, boards, roster candidates, companion checkouts and verify commands, runs one batched interview, then writes the six config docs (project, standards, verify, review, prove, role) to the repo layer or, with --global, the global layer, keeping the shipped comments. Also reruns only the queue choice (project --queue) or the picking rules (project --picking), checks the current config (--check), upgrades old schemas (--upgrade) and runs non-interactively from an answers file (--answers). NOT for running work (use /not-me), NOT for diagnosing a broken install (use /not-me:doctor), NOT for changing the allow list (use /not-me:allow-repo).
argument-hint: "[--global] [all|project|standards|verify|review|prove|role] [--queue] [--picking] [--check] [--upgrade] [--answers FILE] [--import SOURCE]"
disable-model-invocation: true
allowed-tools: AskUserQuestion, Read, Write, Edit, Bash, Glob, Grep
---

# not-me setup

You write not-me's configuration with the user. Arguments: `$ARGUMENTS`.

Paths written `${CLAUDE_PLUGIN_ROOT}/...` below are substituted at load time with
the plugin's directory; use them as literal paths. Every CLI call goes through
`"${CLAUDE_PLUGIN_ROOT}/bin/notme" <command>`; it prints one JSON object. Below,
`notme <command>` is short for that.

Setup never posts comments, reviews or a PR body; its marker `<!-- not-me:setup -->`
is reserved and never written.

Read before starting: `${CLAUDE_PLUGIN_ROOT}/references/config-and-layers.md`
(layers, merge rules, what the repo layer may not loosen). Read
`${CLAUDE_PLUGIN_ROOT}/references/companions.md` before the companions step and
`${CLAUDE_PLUGIN_ROOT}/references/delegation.md` before writing `## Delegation`.

## Safety rules (all modes)

1. Write only inside `.claude/not-me/` at the git toplevel (the repo layer), inside
   `~/.claude/not-me/` (the global layer; `config --explain` shows its real path),
   and, when the user asks, inside `.claude/not-me/` of a companion checkout.
2. The only GitHub write is the CLI's `labels-ensure`, after an explicit yes.
3. Never edit the allow list. Print `/not-me:allow-repo` commands for the user.
4. Never write a frontmatter key that is not in the shipped defaults under
   `${CLAUDE_PLUGIN_ROOT}/defaults/`, except `extends`.
5. Never put `mode: live` in the repo layer. Never set `enabled: true` or turn a
   `guard` rule off from setup.

## Parse arguments

- Scope: `all` (default) or one doc name: `project`, `standards`, `verify`,
  `review`, `prove`, `role`.
- `--global`: target the global layer and ask only operator-level keys.
- `--queue`: with scope `project` (or no scope), rerun only the queue choice
  (see Queue mode).
- `--picking`: only the picking step (Step 2a) on `project.md`. Alone, it means
  scope `project`; with another doc name, print the argument hint and stop. With
  `--global`, it targets the global layer.
- `--check`: report only, write nothing (see Check mode).
- `--upgrade`: schema upgrade only (see Upgrade mode).
- `--answers FILE`: read answers from a JSON file and ask nothing. Keys are in
  `${CLAUDE_PLUGIN_ROOT}/skills/setup/references/interview.md`.
- `--import SOURCE`: reserved. Print "Importing an existing setup is a later unit
  of not-me; nothing was changed." and stop.

An unknown argument: print the argument hint and stop.

## Step 0: where things stand

1. Run `notme doctor`. Show each failed check with its detail. A failed required
   check does not stop setup, but say live mode cannot be turned on until it passes.
2. Run `notme status`. When `active_run` is set and its marker is fresh, stop:
   "A not-me run is active; run setup when it ends." Do not override this.
3. Run `notme config --explain`. Show a table per doc: `project.md`,
   `standards.md`, `verify.md`, `review.md`, `prove.md`, `role.md`, with the layer that wins
   today (default, global layer, repo layer), the file path, and warnings.
4. Without `--global`, run `git rev-parse --show-toplevel`. Outside a git checkout,
   refuse to write the repo layer and offer `--global` instead.
5. In `--queue`, `--check` and `--upgrade` mode, go to that section now.

## Step 1: detect

Run the probes in `${CLAUDE_PLUGIN_ROOT}/skills/setup/references/detection.md`,
starting with `notme preflight` for the repo, tracker and default branch. With
`--global`, skip the repo probes. Keep the results; each one becomes the
recommended answer to a question.

When the repo has the state labels (or will after Step 3) and a project board
linked to it, the team likely uses both. Offer `queue.source: either` (picks an
issue with the label or in the board's To Do column) or `queue.sync: true` in the
interview so claims, moves and releases keep the labels and the board Status in
step, and fill `queue.board_states` from that board's real Status column names
(the Board columns probe), never the shipped defaults. Say that the board must
be confirmed on the operator's allow list before the board side is written, and
that a failed board or label mirror write is only a warning.

## Step 2: interview

Follow `${CLAUDE_PLUGIN_ROOT}/skills/setup/references/interview.md`: AskUserQuestion,
at most four questions per call, several calls allowed, detected values offered
first, each key explained with its comment from the shipped defaults. With
`--answers`, read the file instead and ask nothing.

In a full or `project` run, Step 2a follows the interview. Cover: tracker vs code repo; queue source (labels, a board, or either, and which board),
and `queue.sync` when both labels and a board were detected;
roster rows and tiers; reviewers; whether every reviewer must approve, and
each reviewer's role and pass label; companion set and push order; label mapping;
protected branches to add; `models.strategy` (`orchestrate` or `single`);
confirmation of the verify commands; the prove `target` strategy; `notify.command`;
and whether to turn on live mode at the end.

With `--global`, ask only `mode`, `attribution`, `limits`, `models`, `router`,
`notify` and `logs.root` (write `mode` only as `dry-run`; live is Step 7's
command), plus Step 2a's picking answers. `queue.include_unassigned` is the one
`queue` key the global layer may get, and only through Step 2a. Refuse repo-only
answers (tracker, repos, other `queue` keys, labels,
reviewers, roster, roles, verify commands, ticket format) with one line saying they
belong in a repo layer. Never ask about `allow_repos`.

## Step 2a: picking (project scope, or `--picking`)

Follow `${CLAUDE_PLUGIN_ROOT}/skills/setup/references/picking.md`: show how issues
are picked now, list the ranking signals from `notme picking-signals` (board fields,
labels), ask which layer to write, what to rank by, whether to include unassigned
issues and which epics to target, then write only `picking.fields`,
`picking.target_epics`, `queue.include_unassigned` and the `## Picking work` section,
keeping the rest of the file. Keeping the current rules writes no section and no
`picking.fields`.

With `--picking`, run Step 0, then this step, then Step 5 (picking keys only), Step 6
items 1 to 4 and Step 7 items 1 and 3. It skips Step 1, `notme labels-ensure`, the
live-mode prompt and the `/not-me:allow-repo` commands: it changes nothing on GitHub.

## Step 3: labels (project scope, repo layer)

Follow `${CLAUDE_PLUGIN_ROOT}/skills/setup/references/labels.md`. Show the
recommended eleven next to the repo's existing labels. Per state the user picks
create, map to an existing label, or skip. Refuse a mapping where two states share
a label name; ask again for those states. List the pass labels role.md names
too, and refuse one equal to a state label or shared by two roles. Labels are
created later, in Step 6, and only after a yes.

## Step 4: companions (project scope, repo layer)

For each detected sibling checkout, ask whether it is in the set and where it goes
in push order. Write `repos` with one `primary` (this repo) and the rest
`companion`, in push order.

Then tell the user each companion must be confirmed once by a person, and print the
commands, one per companion, plus one per board declared only in the repo layer:

```
/not-me:allow-repo owner/companion
/not-me:allow-repo owner/name --board owner/number
```

Offer to write a minimal `.claude/not-me/` in each companion checkout with only
`standards.md` and `verify.md`, rendered the same way from that checkout's own
detected commands. Never write a companion's `project.md`.

## Step 5: render

For each doc in scope, start from `${CLAUDE_PLUGIN_ROOT}/defaults/<doc>.md` (one of
`project.md`, `standards.md`, `verify.md`, `review.md`, `prove.md`, `role.md`). When the
target file already exists, start from it instead and change only answered keys.

Frontmatter:

1. Keep `schema` and every comment. Keep the default's line layout and its trailing
   `# comment` on each key.
2. Write each key the user answered as a live line with the new value; for a map
   key, write only the answered subkeys (maps merge key by key).
3. In a new global or repo file, turn each unanswered key into a commented-out line
   (`# key: value  # comment`) so it documents the option without shadowing a
   lower layer.
4. Quote a value that starts with `!`, `[`, `{`, `#` or contains `: `. For a nested
   value, produce it with `dump()` in `${CLAUDE_PLUGIN_ROOT}/notme/frontmatter.py`
   and keep the comment.
5. When a global doc exists for a repo-layer doc, ask whether to add to it or
   replace it. Add to it: write `extends: global`, and the body holds only the
   sections setup produced. Replace it: no `extends`, and the body is the full
   default body with the produced sections swapped in.

Body sections the interview produces:

- `project.md`: `## Roster` table rows (login, name, tier, assignable, reviewer),
  and `## Picking work` (Step 2a).
- `standards.md`: `## Delegation` paths and kinds, `## Ticket format`,
  `## Issue readiness`.
- `verify.md`: `## Tests`, `## Lint`, `## Build`, `## Environment start` with the
  confirmed commands and where each was found, and `## Gate additions`.
- `role.md`: only when the user wants every reviewer to approve, a starter
  `## Roles` table with one row per roster reviewer (Reviewer yes), Role and
  Pass label as answered or left empty. Any row turns the rule on; no rows
  keeps one approval enough.

Keep every other section's default text. Do not invent sections.

## Step 6: write and check

1. Before writing, copy each existing target file into the temp directory (`autonomy-and-logging.md`, Text to GitHub, rule 2) and show a unified
   diff (`diff -u old new`, an empty old for a new file). Ask once to write them
   all. With `--answers`, write without asking.
2. Write the files.
3. Run `notme config --check`. On errors (exit 4), restore every file from its
   copy, show the errors with layer, file and line, and stop.
4. Run `notme config --explain` again and show which layer now wins per doc.
5. Labels: run `notme labels-ensure --check`, show `missing` (state labels and
   the pass labels role.md names), and ask before running `notme labels-ensure`.
   A declined pass label is not an error: that role gets no pass label and a
   review run warns once. With `--answers`, create only when
   `create_labels` is true. A `"dry_run": true` result created nothing: say so.

## Step 7: finish

1. Run `notme doctor` again.
2. If it passes and the user said yes to live mode, do not edit any file: run
   `notme mode show` and print the live command it gives for the user to run in
   their own terminal (as `/not-me:mode live` does). Only that signed command turns live on; a
   hand-set `mode: live` stays dry-run, and the repo layer can never set it.
3. Otherwise explain dry-run: every write a run would make is recorded in the
   ledger and nothing is sent, until the user runs `/not-me:mode live`.
4. Print the next commands: `/not-me:doctor`, `/not-me --dry-run`, and each
   `/not-me:allow-repo` confirmation from Step 4.

## Queue mode (`project --queue`)

Follow `${CLAUDE_PLUGIN_ROOT}/skills/setup/references/queue.md`: show the current
source and what it picks (the queue summary from `notme config --explain`), list
the tracker's labels and its owner's boards so the user picks, write only the queue
keys of the repo layer's `project.md`, and for a board print the
`/not-me:allow-repo owner/name --board owner/number` command for the user. Skip
Steps 1 to 5 and the other questions. With `--global`, refuse: the queue belongs in
a repo layer.

## Check mode (`--check`)

Write nothing. From `notme config --explain` and `notme config --check`, report per
doc: the winning layer and file, the keys still at their default, missing docs,
schema drift (a doc whose `schema` differs from the shipped default), repo-layer
values the CLI shadowed, and config errors. End with the command that fixes each
finding (`/not-me:setup <doc>`, `/not-me:setup --upgrade`).

## Upgrade mode (`--upgrade`)

Only schema 1 exists today. Read each doc's `schema` from `notme config --explain`.
If every doc is at schema 1, say "All docs are at schema 1, the only schema; nothing
to upgrade." and stop. A doc with a schema newer than the shipped default came from
a newer plugin: report it and change nothing.

## Result

End with one block: the layer written, each file written or left alone, the picking
rules and keys written (or the current rules kept) and any file whose `## Picking work`
shadows them, labels created or pending, companions needing confirmation, the mode, and the next
commands.
