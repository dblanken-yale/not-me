# Configuration

not-me is configured by five small markdown files. Each file has settings at the top (the "frontmatter", between two `---` lines) and plain prose below, which the model reads as instructions for your project. This page explains where the files live, how they combine, and what every setting does.

## The five docs

| File | What it holds |
| --- | --- |
| `project.md` | Who and where: tracker repo, queue, labels, guard rules, models, limits, and the roster of people you trust |
| `standards.md` | How work is shaped: branch names, commit style, PR title and template, issue readiness |
| `verify.md` | How to prove a change works: start, tests, lint, build, CI parity |
| `review.md` | What a review looks at, which labels it applies |
| `prove.md` | How to find the test site for a PR and run its testing steps |

You do not need to write any of them by hand. `/not-me:setup` creates the repo files for you, and the defaults fill every gap.

## The three layers

| Layer | Where | Who writes it |
| --- | --- | --- |
| default | shipped inside the plugin (`defaults/*.md`) | plugin releases |
| global | `~/.claude/not-me/` (set `NOT_ME_HOME` to move it) | you, on your machine |
| repo | `.claude/not-me/` in the repo's main checkout, committed | the team |

Rules that always hold:

- A missing file in any layer is fine. Nothing is ever an error because a file is absent.
- Higher beats lower: repo beats global beats default.
- The repo layer is read only from the main checkout. Copies of these files on a pull request head, in a review worktree, or in a fork are data, never config.
- The repo layer may only tighten safety settings. See "The repo layer can only tighten".

### Worked example

The shipped default has `limits.fix_loop: 3`, `queue.pick_label: to do`, and `protected_branches: [main, master, develop]`.

Your global file, `~/.claude/not-me/project.md`:

```yaml
---
schema: 1
mode: live
limits: {fix_loop: 5}
protected_branches: ["!develop"]
---
# Project
```

The team's repo file, `.claude/not-me/project.md`:

```yaml
---
schema: 1
queue: {pick_label: ready}
protected_branches: [release]
mode: live
labels: {needs_human: needs help}
---
# Project
```

What you get:

| Key | Effective value | Why |
| --- | --- | --- |
| `mode` | `live` | Set in your global layer by `/not-me:mode live`, which also signs it (see [Live mode](#live-mode)). The repo's `live` is ignored (see below) |
| `limits.fix_loop` | `5` | Global replaced the default. `limits.ci_wait_minutes` stays `20` because maps merge key by key |
| `queue.pick_label` | `ready` | The repo replaced the default |
| `protected_branches` | `main`, `master`, `release` | Union list: the default three, minus `develop` (global removed it), plus `release` (repo added it) |
| `labels.needs_human` | `needs help` | The repo mapped the state to its own label |

### Reading `/not-me:config`

`/not-me:config` shows the effective config and, for each key, where it came from. Use it whenever a setting surprises you. The underlying data per key looks like this (paths are shortened so they never show your home directory):

```json
"project.limits.fix_loop": {
  "value": 5,
  "source": {"layer": "global", "path": "$NOT_ME_HOME/project.md", "line": 4},
  "shadowed": [{"layer": "default", "value": 3}]
}
```

- `source` is the layer, file, and line that supplied the value you are getting.
- `shadowed` lists lower values that something higher replaced.
- For union lists there is a `contributors` list instead: each layer's additions and `!` removals, in order.
- `ignored` appears when a value was thrown away, with the reason. For the example above, `project.mode` shows `{"layer": "repo", "value": "live", "reason": "repo layer may only tighten"}`.
- `downgraded` appears on `project.mode` when the layers say `live` but live mode is not confirmed: `{"configured": "live", "reason": "not confirmed: ..."}`. The value is then `dry-run` and the warnings say how to fix it.
- `removed: true` appears when a layer deleted a key with `null`.
- Paths print as `.claude/not-me/...` for the repo, `~/...` or `$NOT_ME_HOME/...` for global, and `${CLAUDE_PLUGIN_ROOT}/defaults/...` for the defaults.
- Bodies get the same treatment: for each doc it lists which layers contributed and which layer each `##` section came from.
- A `warnings` list at the end collects problems that did not stop the load, such as an ignored repo-layer value.

Config errors name the layer, file, and line (`repo .claude/not-me/project.md:5: ...`). A file with a syntax error is skipped with a warning and the lower layers are used, but the run itself stops with `FAILED - config invalid` until you fix it.

## How settings merge

1. Settings merge per key. Maps merge key by key, so setting `limits: {fix_loop: 5}` leaves the other `limits` keys alone.
2. Plain lists and scalars replace the lower value. If the default is `router: [update, new]` and you write `router: [new]`, you get `[new]`.
3. Union lists only grow. They are `protected_branches`, `allow_repos`, and `markers.legacy`. Each layer's items are added to the lower layers' items. To remove an item a lower layer added, write it with a `!` in front, and quote it so YAML reads it as text: `"!develop"`.
4. `null` deletes a key that a lower layer set. The key is then absent. It does not bring the default back. On a union list, `null` does nothing and produces a warning. Use `!value` instead.
5. Type checks use the default as the reference. A string where the default is a list, or a value outside an allowed set (for example `mode: yes`), is reported as an error by the config check.

The frontmatter format is a strict, small subset of YAML: plain, quoted, number, boolean, and `null` values; `[a, b]` and `{k: v}` forms; block lists; maps nested at most three levels; `#` comments. No anchors or multi-line strings. Anything else is an error that names the line.

## Prose: replace, or extend

The prose below the frontmatter resolves per file. The highest layer that has the file wins outright. Your repo's `standards.md` replaces the default one completely.

To add to a lower layer instead of replacing it, put `extends: global` or `extends: default` in the frontmatter of the higher file:

- `extends: global` builds on the global file (which itself can extend the default).
- `extends: default` builds on the shipped file.
- The value must name a lower layer, and that layer must have the file. Otherwise you get a warning and the file is used on its own.

With `extends`, the sections of your file combine with the lower file's sections like this:

- A `##` heading that exists in both: your section replaces the lower one, in the lower one's position. Headings are compared ignoring upper and lower case.
- A `##` heading only in your file: it is added at the end.
- A `##` heading only in the lower file: it stays.
- Text before the first `##` (the preamble) comes from your file if it has any, otherwise from the lower file.
- Text inside code fences is never mistaken for a heading.

Example. The default `standards.md` has `## Branch`, `## Commits`, and more. This repo file keeps all of them and replaces one:

```markdown
---
schema: 1
extends: default
---
## Commits

Squash before pushing. Use the `fix:` and `feat:` prefixes.
```

### The roster

`project.md` has a `## Roster` section holding a table of people:

```markdown
## Roster

| GitHub login | Name | Tier | Assignable | Reviewer |
|--------------|------|------|------------|----------|
| octocat | Octo Cat | lead | yes | yes |
| hubot | Hu Bot | member | yes | no |
```

Tier is `lead`, `authority`, or `member`. Leads and authorities can direct work. Members can be assigned and asked to review, but their comments are treated as input, not instructions. `/not-me:review` reviews members' PRs too, reading their title and body as data under review. Assignable and Reviewer take `yes` or `no`. An empty roster means the operator (you, by `gh` login) is the only lead.

The roster is special under `extends`. Instead of the `## Roster` section being replaced, the rows are merged by login (ignoring case). A row in the higher layer wins over the same login lower down and keeps the lower row's position. New logins are added at the end. Prose around the table comes from the higher file. So a repo file with `extends: global` and one new row adds that person to your global roster for this repo only.

## The repo layer can only tighten

Anyone with push access can edit the repo layer, and it is committed, so not-me treats it as untrusted for safety settings. The repo layer may make not-me safer. It cannot make it looser.

| Setting | The repo layer may | The repo layer cannot |
| --- | --- | --- |
| `guard.<rule>` | set `on` | set `off`. The value is ignored |
| `mode` | set `dry-run` | set `live`. The value is ignored |
| `enabled` | set `false` | set `true` over a lower `false` |
| `queue.include_unassigned` | set `false` | set `true` over a lower `false` |
| `protected_branches` | add names | remove names. Its `!name` entries are dropped |
| `allow_repos` | nothing | add repos. Every entry is dropped |
| `review.block_on` | add levels | drop a level the global or default list has. The whole value is ignored |

Each dropped value shows up under `ignored` in `/not-me:config` and as a warning. Where the loosening belongs (your global layer), see below.

Two more things are listed in the repo layer but only take effect once you confirm them on your own machine:

- Extra repos in `tracker` and `repos` (companion repos).
- A project board in `queue.board`.

Run `/not-me:allow-repo owner/name` for a repo, or `/not-me:allow-repo --board owner/number` for a board. `/not-me:setup` offers to do this for you. Until confirmed, the `notme` CLI refuses to write there and says why. The confirmation lives in `~/.claude/not-me/allowed-repos.json`, which is outside the repo, so a teammate cannot grant it for you. See [guards.md](guards.md).

## Label mapping

not-me thinks in abstract states. The `labels` key maps each state to the label name your tracker actually uses. Matching ignores upper and lower case. The defaults are:

| State | Default label | Meaning |
| --- | --- | --- |
| `todo` | `to do` | ready to be picked up |
| `in_progress` | `in progress` | claimed and being worked |
| `needs_review` | `needs review` | PR open and waiting for a reviewer |
| `needs_work` | `needs work` | reviewer sent the PR back |
| `ready_to_merge` | `ready to merge` | approved |
| `question` | `question` | waiting on an answer from a person |
| `blocked` | `blocked` | cannot move until something else lands |
| `epic` | `epic` | parent issue, never picked up directly |
| `needs_help` | `needs help` | not-me gave up and asks for a person |
| `needs_human` | `needs human` | a run stopped and a person must act |
| `paused` | `paused` | skip this issue until the label is removed |

### Example: reuse an existing label

Your team already has a label called `needs help` that people use when they are stuck, and you want not-me's "a person must act" state to use it instead of creating `needs human`.

```yaml
labels:
  needs_human: needs help
  needs_help: gave up
```

Two things to know:

1. Give every state its own label name. The default for `needs_help` is also `needs help`. If two states share a name, not-me can only map the name back to one of them (the later one, `needs_human`), so `needs_help` would silently stop working. Remapping `needs_help` to a new name, as above, avoids that.
2. Only the name changes. The meaning stays: `needs_human` is added whenever a run parks, holds, or gives up.

Every value in `labels` must be a string, and the config check rejects anything else. Labels are always written as the whole set in one call, followed by a read-back, so two people editing labels at once is caught instead of silently overwritten.

### Which issues the queue picks

`/not-me:new` picks an open issue only when all of these hold:

- It carries the `labels.todo` label (`to do` by default), or sits in the `queue.board_states.todo` column when `queue.source` is `board`.
- It carries `queue.opt_in_label`, when one is set.
- It does not carry `paused`, `blocked` or `epic`.
- Its author is a lead or authority on the roster (with no roster, only you).
- It is assigned to you. With `queue.include_unassigned: true`, an issue with no assignee counts too.

An issue assigned to anyone else is never picked, whatever the setting. A fresh install picks only issues assigned to you. To also pick unassigned ones, set this in your global `~/.claude/not-me/project.md`:

```yaml
queue: {include_unassigned: true}
```

A repo file cannot turn this on, because it would widen what not-me picks. When nothing is assigned to you but unassigned issues are ready, `notme pick-issue` returns `{"none": true, "reason": "unassigned_only", "unassigned": <count>, "setting": "queue.include_unassigned"}`.

### Board queues

With `queue.source: board`, work is picked from a Projects v2 board. Set the board and its column names:

```yaml
queue:
  source: board
  board: {owner: acme, number: 7, status_field: Status}
  board_states: {todo: To Do, in_progress: In progress, in_review: In review, blocked: Blocked}
```

Your token needs the `project` scope, and the board must be confirmed with `/not-me:allow-repo --board acme/7` if it was set in the repo layer. Pull request states always use labels.

## Companion sets

One issue can need changes in more than one repo, for example an app and its UI library. List them in `repos`, in push order:

```yaml
tracker: acme/app
repos:
  - {name: acme/app-ui, role: companion}
  - {name: acme/app, role: primary}
```

- Each item needs a `name` (`owner/name`). `path` and `default_branch` default to `auto`. `role` is `primary` (one only) or `companion`.
- `repos: []` means just the `tracker`.
- `tracker: auto` means the repo's `origin`. The issue lives in the tracker, and the primary PR is the one that closes it.
- not-me uses the same branch name in every repo, pushes the companions first and the primary last, and links the PRs to each other in a `## Companion PRs` section.
- Inside a companion checkout, that repo's own `.claude/not-me/` applies for standards, verify, review, and guard settings. A companion with none uses the global and default layers.
- `repos` is a replace key, not a union. A higher layer's list replaces the lower one.
- The names in `repos` need confirming on your machine if they come from the repo layer (above).

## The routing policy

`models.strategy` decides how not-me spreads work across models.

| Value | Behaviour |
| --- | --- |
| `orchestrate` (default) | The running session is the orchestrator. It splits the work into units and sends each to a model by kind: read-only support to `models.triage`, judgment-heavy or security-sensitive units to `models.review`, everything else to `models.implement`. The `## Delegation` section of `standards.md` lists the paths and kinds that count as judgment-heavy |
| `single` | Everything, including the run itself, uses `models.single_model`. No classification |

Related keys: `models.bg` is the model for `--bg` runs. `models.verifier` is the model that double-checks claims before posting: `auto` picks one from a different model family, `none` skips the check and logs that it was skipped. `models.max_tries` is how many tries a unit gets before the orchestrator does it itself: first the dispatched agent, then the same agent with the exact failure, then a fresh agent one tier up.

The accepted values for `models.strategy` are `orchestrate` and `single`. The other model keys take the model names your Claude Code client accepts.

## Kill switch, pause, and the `paused` label

Three ways to stop not-me, from widest to narrowest. Only a person should ever undo them. not-me never removes them itself.

| Control | Scope | Result |
| --- | --- | --- |
| `enabled: false` in a layer | In the repo layer: the whole team. In the global layer: this machine | `STOP - disabled`, before anything is read |
| A file named `PAUSED` in `.claude/not-me/` or `~/.claude/not-me/` | Same as above, with no commit needed | `STOP - paused` |
| The `paused` label on an issue or PR | That one item | Every run skips it. A run that sees the label appear mid-run releases its claim and stops |

A repo layer cannot override a global `enabled: false` with `true`. Delete the `PAUSED` file or fix the key to resume.

## Dry run and the ledger

`mode: dry-run` is the default. In a dry run:

- Reads are real.
- Every write (labels, comments, pushes, PRs, board moves) is appended to a ledger instead of being sent.
- The run logs `dry-run` in its result line.

`NOT_ME_DRY_RUN=1` in the environment forces dry-run for that process no matter what `mode` says.

The ledger is `<git-common-dir>/not-me/logs/intended-writes.jsonl`, one JSON object per line with a timestamp, the operation, the target, the exact command, a hash of the text it would post, and a description. The text itself is saved next to it as `payloads/<hash>.md`. Both are written with owner-only permissions, and not-me refuses to write them through a symlink.

### Live mode

To go live, run `/not-me:mode live` and follow the steps. It needs the operator token (see [install.md](install.md#the-operator-token)). It sets `mode: live` in your global `project.md` and signs it: it writes `~/.claude/not-me/mode-live.sig`, a code made from your operator token and the random epoch in `~/.claude/not-me/mode-live.epoch`. `live` takes effect only when the signature matches your current token and epoch, so typing `mode: live` by hand does not turn live mode on. Not-me then stays in dry-run, and `/not-me:mode`, `/not-me:config` (the `downgraded` entry) and `/not-me:doctor` say live is set but not confirmed. A new operator token invalidates the signature: run `/not-me:mode live` again. The repo layer cannot set `live` at all.

`/not-me:mode dry-run` switches back (with `--global`, it also deletes the signature and replaces the epoch, so an old signature saved somewhere never works again). Setting `mode: dry-run` by hand works too. Returning the global layer to dry-run by any means (a hand edit or removing the `mode` key included) revokes the signature, so going live again needs `/not-me:mode live`. A repo-layer `mode: dry-run` pin does not revoke it.

Upgrading: an existing global `mode: live` written before signatures existed runs as dry-run until you run `/not-me:mode live` once.

## Schema and upgrades

Every doc carries `schema: 1`. A file with no `schema` is treated as current.

- An older schema is upgraded in memory when read, with a note in `warnings`. Run `/not-me:setup --upgrade` to rewrite the file on disk. It shows you a diff first.
- A newer schema than the plugin knows is an error (`schema N is newer than this plugin supports`). The file is skipped. Update the plugin.
- A schema that is not a whole number is an error.

A plugin release that changes the schema ships the upgrade with it.

## Key reference

Every key of every doc, with its shipped default. Keys inside maps are written with dots, so `queue.board.owner` is `owner` inside `board` inside `queue`. The shipped defaults are in `plugins/not-me/defaults/`.

### project.md

| Key | Default | What it does |
| --- | --- | --- |
| `schema` | `1` | Config format version. Setup upgrades older files |
| `mode` | `dry-run` | `dry-run` or `live`. `live` counts only when `/not-me:mode live` signed it (see [Live mode](#live-mode)). The repo layer may only set `dry-run` |
| `enabled` | `true` | `false` turns not-me off for this layer and above. The repo layer may set `false`, never `true` over a global `false` |
| `tracker` | `auto` | `owner/name` of the repo that holds issues. `auto` means `origin` |
| `repos` | `[]` | Companion set, in push order. `[]` means just the tracker. Items are `{name, path: auto, default_branch: auto, role: primary or companion}` |
| `protected_branches` | `[main, master, develop]` | Never committed to directly. Union across layers, `"!name"` removes. The repo layer may add, and its removals are ignored |
| `operator` | `auto` | Your GitHub login. `auto` asks `gh` who is signed in |
| `queue.source` | `labels` | `labels` or `board`. Where work is picked from |
| `queue.pick_label` | `to do` | Label that marks an issue as ready to pick up |
| `queue.opt_in_label` | `null` | An extra label an issue must also carry. `null` means none required |
| `queue.include_unassigned` | `false` | `false` picks only issues assigned to you. `true` also picks issues with no assignee. Issues assigned to someone else are never picked. The repo layer may set `false`, never `true` over a lower `false` (see [Which issues the queue picks](#which-issues-the-queue-picks)) |
| `queue.board.owner` | `null` | Board owner, used when `queue.source` is `board` |
| `queue.board.number` | `null` | Board number, used when `queue.source` is `board` |
| `queue.board.status_field` | `Status` | Name of the board column field |
| `queue.board.priority_fields` | `[]` | Board fields used to rank items |
| `queue.board_states.todo` | `To Do` | Board column name for ready work |
| `queue.board_states.in_progress` | `In progress` | Board column name for claimed work |
| `queue.board_states.in_review` | `In review` | Board column name for work waiting on review |
| `queue.board_states.blocked` | `Blocked` | Board column name for blocked work |
| `labels.todo` | `to do` | Label for ready to be picked up |
| `labels.in_progress` | `in progress` | Label for claimed and being worked |
| `labels.needs_review` | `needs review` | Label for PR open and waiting for a reviewer |
| `labels.needs_work` | `needs work` | Label for a PR a reviewer sent back |
| `labels.ready_to_merge` | `ready to merge` | Label for approved |
| `labels.question` | `question` | Label for waiting on an answer from a person |
| `labels.blocked` | `blocked` | Label for cannot move until something else lands |
| `labels.epic` | `epic` | Label for a parent issue, never picked up directly |
| `labels.needs_help` | `needs help` | Label for not-me gave up and asks for a person |
| `labels.needs_human` | `needs human` | Label for a run that stopped and needs a person to act |
| `labels.paused` | `paused` | Label that makes runs skip an item |
| `reviewers.default` | `[]` | GitHub logins requested as reviewers. Add keys named after a repo to override per repo |
| `router` | `[update, new]` | Order in which `/not-me` tries work kinds |
| `workspace.new` | `checkout` | `checkout` or `worktree` for new work |
| `workspace.update` | `checkout` | `checkout` or `worktree` for rework |
| `workspace.review` | `worktree` | `checkout` or `worktree` for reviews |
| `guard.other_repo` | `on` | `on` or `off`. Writes may only target allowed repos |
| `guard.protected_branches` | `on` | `on` or `off`. No commits, pushes, or merges on protected branches |
| `guard.force_push` | `on` | `on` or `off`. No history rewrites or remote branch deletion |
| `guard.destructive_git` | `on` | `on` or `off`. No hard resets of protected branches, forced branch deletes, `clean -x`, or settings, secrets, and deletions on GitHub |
| `confirm_guard_off` | `[]` | Guard rules this file deliberately turns off. A rule set to `off` only counts if it is listed here, in the same file |
| `allow_repos` | `[]` | Extra `owner/name` repos the guard lets not-me touch. Union across layers. Repo-layer entries are ignored. Use `/not-me:allow-repo` |
| `logs.root` | `null` | Directory for run logs. `null` means `<git-common-dir>/not-me/logs` |
| `markers.legacy` | `[]` | Older run-marker strings that are still recognised as not-me's own. Union across layers |
| `attribution` | `inherit` | `inherit` adds nothing and lets the client decide, `none` adds no line at all, any other text is added as the last line of commit messages and PR bodies |
| `limits.fix_loop` | `3` | Fix rounds before a run gives up and parks |
| `limits.ci_wait_minutes` | `20` | Minutes to wait for CI on a pushed head |
| `limits.claim_stale_hours` | `8` | Hours before an unfinished claim with no pushed branch is reported as stale. It is reported, never released automatically |
| `models.strategy` | `orchestrate` | `orchestrate` or `single`. See "The routing policy" |
| `models.single_model` | `opus` | The model used for everything when `strategy` is `single` |
| `models.bg` | `opus` | The model for `--bg` runs |
| `models.implement` | `sonnet` | The model for ordinary implementation units |
| `models.review` | `opus` | The model for reviews and judgment-heavy units |
| `models.triage` | `haiku` | The model for read-only support work |
| `models.verifier` | `auto` | The model that double-checks claims. `auto` picks another family, `none` skips |
| `models.max_tries` | `3` | Tries per unit before the orchestrator takes it over |
| `notify.command` | `null` | A shell command run with the result line and log path when a run ends with something to report. `null` means none. Prefer your global layer, because a repo-layer command would run on every teammate's machine |
| `retro.file_upstream` | `false` | `true` files retro findings as issues on the plugin repo |

### standards.md

| Key | Default | What it does |
| --- | --- | --- |
| `schema` | `1` | Config format version |
| `read_first` | `auto` | Files to read before coding. `auto` means CONTRIBUTING, CLAUDE.md, README |
| `branch` | `{issue}-{slug}` | Branch name template |
| `commit` | `conventional` | `conventional` or `free` |
| `pr_title` | `{issue}: {issue_title}` | PR title template |
| `pr_template` | `auto` | Path to a PR template. `auto` means `.github/pull_request_template.md` if present |
| `closing_keyword` | `auto` | Keyword that links a PR to its issue. `auto` means `Closes owner/repo#N` when the tracker is the same repo; the reference is always fully qualified |
| `issue_template` | `auto` | Path to an issue template. `auto` means `.github/ISSUE_TEMPLATE` if present |

### verify.md

| Key | Default | What it does |
| --- | --- | --- |
| `schema` | `1` | Config format version. `verify.md` has no other keys. Its sections hold the commands, and leaving a section as is lets not-me discover them |

### review.md

| Key | Default | What it does |
| --- | --- | --- |
| `schema` | `1` | Config format version |
| `dimensions` | `[correctness, tests, acceptance-criteria, security, data-loss, docs, maintainability]` | What a review looks at |
| `severity` | `[blocker, major, minor]` | Finding levels, most severe first |
| `block_on` | `[blocker, major]` | Finding levels that make the verdict "request changes". Findings at other levels are still posted, labelled with their level, but do not block. Values must come from `severity` and the list must include `blocker`. The global layer may set any valid list; the repo layer may only add levels, and a repo list that drops one is ignored with a warning |
| `checks` | `verify` | Run the verify gate as part of review |
| `approve_label` | `ready_to_merge` | The `labels` key applied on approval |
| `changes_label` | `needs_work` | The `labels` key applied when changes are requested |
| `reviewer_profile` | `null` | Path to a reviewer profile doc. `null` means none |
| `auto_approve` | `false` | `true` lets the review skill send an APPROVE event. Read from the default and global layers only; the repo layer cannot turn it on |

### prove.md

| Key | Default | What it does |
| --- | --- | --- |
| `schema` | `1` | Config format version |
| `target.strategy` | `[deployments, bot-comment, url-template, local]` | How to find the URL to test, tried in order |
| `target.url_template` | `null` | URL pattern for the `url-template` strategy |
| `target.bot_comment_regex` | `null` | Pattern that finds the URL in a bot comment, for the `bot-comment` strategy |
| `target.hosts` | `null` | Host suffixes a proof URL may use. `null` means the built-in preview hosts. Read from the default and global layers only |
| `target.bots` | `[]` | Bot logins whose preview comments are read. Read from the default and global layers only |
| `steps_heading` | `(Functional )?[Tt]esting steps` | Regex for the PR heading that holds the testing steps |
| `fail_label` | `needs_work` | The `labels` key applied when proof fails |
| `give_up_after` | `3` | Attempts before asking for a person |
| `screenshots` | `none` | `none`, `failures`, or `all` |
