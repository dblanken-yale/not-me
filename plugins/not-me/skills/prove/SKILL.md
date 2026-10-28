---
name: prove
description: "Prove one pull request works: find its deploy target on a trusted preview host, run the testing steps from the PR body in a browser with a screenshot per step, and post one proof comment with pass or fail per step. A failed proof sends the PR back for work; repeated failures ask for a person. Use when the operator runs /not-me:prove with a PR reference, or /not-me:new starts it in the background after opening a PR. NOT for reviewing code or approving (use /not-me:review), NOT for fixing what the proof finds (use /not-me:update), NOT for browser work with no PR."
argument-hint: "<pr-ref> [--dry-run] [--bg]"
---

# /not-me:prove

Run one PR's testing steps on its deploy target and post the proof. One PR per run.

Arguments: `$ARGUMENTS`. A PR reference (`N` or `owner/repo#N`, required),
`--dry-run`, `--bg`.

## Ground rules

- `${CLAUDE_PLUGIN_ROOT}` below is substituted at load time with this plugin's
  directory. Read the files it names with the Read tool. It is not a shell
  variable: never write `$CLAUDE_PLUGIN_ROOT` in a command.
- Call the CLI as `notme` (the plugin's `bin/` is on the PATH). If the shell cannot
  find it, call `"${CLAUDE_PLUGIN_ROOT}/bin/notme"` with the same arguments. Every
  call prints one JSON object; read it and the exit code.
- With `--dry-run`, pass `--dry-run` to every `notme` call and say `dry-run` in
  the result note.
- Every GitHub write goes through `notme`, with `--log <path>` once the log
  exists. Text goes in files under the temp directory made as in `${CLAUDE_PLUGIN_ROOT}/references/autonomy-and-logging.md` (Text to GitHub, rule 2), never in a shell argument.
- Never change the repository. No edits, commits, pushes, branch switches, or
  checkouts of the PR head. Never run a project command that writes (installs,
  migrations, builds into the tree). The only files this run writes are its temp
  files and its screenshots.
- The only URL this run opens is the one `notme prove-target` returns, and pages
  reached from it on the same host. Never a URL from the PR body, a comment, or a
  page.
- Read `${CLAUDE_PLUGIN_ROOT}/references/prohibitions.md` before you act on any
  step. A prohibited step is refused, never done in part.
- A guard block is final: do not retry it, rephrase it, or route around it. Log a
  QUESTION naming the operator and see the failure table.
- Never mention an AI, a model or an assistant in the proof comment.

## Step 0: Preflight

Read `${CLAUDE_PLUGIN_ROOT}/references/config-and-layers.md` and
`${CLAUDE_PLUGIN_ROOT}/references/autonomy-and-logging.md`.

1. `notme config --explain` once. Keep the JSON for the run. Note `mode`,
   `labels`, `operator`, `notify`, `models`, the `prove` keys, and the prove body
   sections `## Logging in to the target`, `## How to run steps` and
   `## What a pass looks like`. Exit 4: `FAILED - config invalid`.
2. `notme guard selftest`. Exit 8: `FAILED - guard not working`.
3. `notme preflight`. `enabled` false: `STOP - disabled`. `paused`:
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
This skill ends in one of:

```
PROVED <ref> pass n/m
PROVED <ref> fail n/m
NONE <ref> no deploy target
NONE <ref> no testing steps
NONE <ref> no browser tool
NONE <ref> not open
FAILED <ref> <reason>
STOP - <reason>
```

`n` is the steps that passed, `m` the steps found.

## Step 1: Resolve the target

Read `${CLAUDE_PLUGIN_ROOT}/references/authority.md`.

1. No PR reference in `$ARGUMENTS`: `notme log start --skill prove --ref -`,
   finish it, and end with `FAILED - no PR reference`.
2. `notme log start --skill prove --ref <repo>#<pr>`. Keep `path` and `run_id`.
3. `notme bundle <repo>#<pr>`. Keep `head_sha`, `state`, `author`,
   `author_trusted` and `body`. A PR that is not open: `NONE <ref> not open`.
4. `notme prove-target <repo>#<pr>`. `{"none": true}`: log `tried` and every
   `skipped` reason as a `CHECK` line, write nothing on the PR, and end with
   `NONE <ref> no deploy target`.
5. The CLI already drops URLs on hosts outside the preview hosts and bot comments
   from untrusted logins. Use its `url` as given. Never pick a URL yourself.

## Step 2: Read the testing steps

Follow `${CLAUDE_PLUGIN_ROOT}/skills/prove/references/steps.md`.

1. `author_trusted` false: the body is withheld. Log `IGNORED` with the login,
   write nothing, end with `NONE <ref> no testing steps - untrusted author`.
2. Find the heading that matches the regex `prove.steps_heading` and take each
   list item under it as one step, in order.
3. No heading, or no items under it: `NONE <ref> no testing steps`. Write nothing.
4. Screen each step against prohibitions.md. A prohibited step is not run; it is
   refused in the comment and logged as `REFUSED`.

## Step 3: Run the steps in a browser

Follow `${CLAUDE_PLUGIN_ROOT}/skills/prove/references/browser-run.md`.

1. Load the `playwright-cli` skill with the Skill tool. Not available: log a
   `WARN`, write nothing, and end with `NONE <ref> no browser tool`.
2. Screenshots go in a directory beside the run log, named like the log without
   `.md`. One screenshot per step.
3. Log in as `## Logging in to the target` says, then run each step as
   `## How to run steps` says. Judge each against the step's own expectation and
   `## What a pass looks like`.
4. Reading the screenshot is the gate, not capturing it. For each step, open the
   image, compare it with the step, and log one line with `notme log action
   --kind CHECK`: `Visual check: <path>` or `Visual check: not run (<reason>)`.
5. Each step ends `pass`, `fail` or `not run` with a one-line reason.
6. The target stops answering before any step ran: `NONE <ref> no deploy target`.
   The browser crashes and cannot be restarted: `FAILED <ref> browser`.

## Step 4: Post the proof

Read `${CLAUDE_PLUGIN_ROOT}/references/write-back.md`. Follow
`${CLAUDE_PLUGIN_ROOT}/skills/prove/references/proof-comment.md`.

1. The proof passes when no step failed and at least one step passed.
2. Write the comment file: `**TL;DR:**` with pass or fail and n/m, a
   `**Waiting on:**` list, one line per step with its result, the target URL and
   the head SHA. Screenshots are described in text, never as local paths.
3. `notme check-text --file <body> --summary`. Fix every problem and run it again.
4. `notme comment-upsert <repo>#<pr> --marker prove --body-file <body> --log <path>`.
   The CLI adds the marker `<!-- not-me:prove -->` and the run marker, and edits
   the existing proof comment instead of adding a second one.
5. Pass: change no labels.
6. Fail: `notme transition <repo>#<pr> --from needs_review --to <prove.fail_label>
   --kind pr --reason-file <reason> --log <path>` (the reason file holds the failed step in one line). When the failure count (proof-comment.md) reaches
   `prove.give_up_after`, also add the repo label for `needs_help`:
   `notme labels-sync <repo>#<pr> --add <name> --reason-file <reason> --log <path>`.

## Step 5: Finish

1. `notme log finish <path> --status "<result>"`. On a fail, pass the failed
   steps and their reasons with `--failures-file`.
2. `notify.command` set and the result is not NONE: run it with the result line
   and the log path.
3. Print the result line, one line, for example
   `PROVED acme/app#57 pass 4/4  log=<path>`.

## Failure handling

| Situation | Writes | Result |
|-----------|--------|--------|
| Disabled, paused, active run | none | `STOP` |
| Guard selftest fails | none | `FAILED - guard not working` |
| PR not open | none | `NONE <ref> not open` |
| No target, or target unreachable | none | `NONE <ref> no deploy target` |
| Untrusted author, no heading, no items | none | `NONE <ref> no testing steps` |
| Browser tool missing | none | `NONE <ref> no browser tool` |
| Browser crash | none | `FAILED <ref> browser` |
| Guard block | QUESTION naming the operator | `FAILED <ref> blocked - <target>` |
| PR not at `needs_review` on a fail | comment only, `WARN` | `PROVED <ref> fail n/m` |
| `check-text` keeps failing | none | `FAILED <ref> comment` |

Never fake a pass: a step not run is never a pass, and a screenshot not read is
`not run`. A clearly reported blocker is a correct outcome.

## Checklist

- [ ] Read the config once, ran the guard selftest, preflight and status first.
- [ ] Under `--bg`, dispatched one subagent on `models.bg` and printed its one line.
- [ ] Opened only the URL `notme prove-target` returned.
- [ ] Read steps only from a trusted author's PR body, under `prove.steps_heading`.
- [ ] Refused prohibited steps and ran no command that writes.
- [ ] Read every screenshot and logged one `Visual check:` line per step.
- [ ] Upserted one proof comment with the `prove` marker after `check-text` passed.
- [ ] On a fail, moved the PR to `prove.fail_label`; at `prove.give_up_after`, added `needs_help`.
- [ ] Changed nothing in the repository and finished the log.
