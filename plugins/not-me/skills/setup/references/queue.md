# Setup: the queue section (`project --queue`)

`/not-me:setup project --queue` reruns only the question of where work is picked
from. It shows the current source, lists what it can see so the user picks instead
of typing, and writes only the queue keys of the repo layer's `project.md`. Every
other line of that file stays as it is.

The queue keys are repo-only, like the rest of the queue: with `--global`, refuse
in one line ("the queue belongs in a repo layer") and stop. The one exception,
`queue.include_unassigned`, is not asked here: the picking step (`--picking`, see
`picking.md`) asks it and writes `true` only to the global layer.

## 1. Show what it picks today

Run `notme config --explain`. Print its `queue` object in plain words: the
summary line (for example "picks issues labeled `to do`, assigned to you" or
"picks issues in the `To Do` column of board acme/3, assigned to you"), then the
layer, file and line of each queue key in the keys part of the output. When its ok field is
false, say the summary is why the queue cannot work as written.

## 2. List what it can see (reads only)

Run each read below; a failed read is not an error, just ask without that list.

| What | Read |
|------|------|
| Labels in the tracker | `gh label list -R <tracker> --json name,description --limit 500` |
| Boards of the tracker's owner | `gh project list --owner <owner> --format json` |
| Columns of the chosen board | `gh project field-list <number> --owner <owner> --format json`: the options of the single-select field named in `queue.board.status_field` |

`<tracker>` and `<owner>` come from `notme preflight` (`tracker`).

## 3. Ask

With AskUserQuestion, one call, current values first and marked as current:

1. Source: labels, or a board. "Pick from both at once" is not offered: the
   source owns an issue's state (see the Board queues section of the config guide).
   When the tracker has the state labels and its owner has a board linked to the
   repo, also offer `queue.sync: true` (keep the other side in step on every claim,
   move and release); for it, ask for the board and its columns as for a board
   source, even when the source is labels.
2. Labels: which label marks an issue as ready, picked from the label list.
   Board: which board, picked from the board list; then, in a second call, the
   column for each of `queue.board_states.todo`, `queue.board_states.in_progress`,
   `queue.board_states.in_review` and `queue.board_states.blocked`, each picked
   from that board's status options (offer "no column" for the blocked one).
3. An opt-in label every picked issue must also carry, or none (from the label list).
4. Stacking. Explain the line first: an issue body may say
   `Stack on: owner/repo#PR` (`Based on:` and `On top of:` work too, one line per issue); when that open PR (not a draft, from a trusted
   author) is the issue's only blocker, the issue is ready now and branches from
   the PR's branch, its PR starting with `Builds on owner/repo#PR`. Only a trusted
   author's line counts. Then ask for the default: declared (the default: only
   issues with the line stack) or off (never stack; such an issue waits for the
   merge).

With `--answers`, read the `queue` object of the answers file (keys shaped like
`queue` in the shipped `project.md`) instead and ask
nothing.

## 4. Write only the queue keys

Target: `.claude/not-me/project.md` at the git toplevel. When it exists, edit only
these lines and keep everything else byte for byte. When it does not, render it
from the shipped default as in Step 5 of the skill, with only these keys live.

| Chosen | Keys written |
|--------|--------------|
| Labels | `queue.source: labels`, `queue.pick_label`, `labels.todo` (the same name), `queue.opt_in_label` |
| Board | `queue.source: board`, `queue.board.owner`, `queue.board.number`, and `queue.board.status_field` when it is not the default, `queue.board_states`, `queue.opt_in_label` |
| Stacking off | `queue.stacking: off` (write nothing for declared, the shipped default; the repo layer may only set off) |
| Sync on | `queue.sync: true`, plus the board keys above (`queue.board.*`, `queue.board_states` from the board's real column names) whichever source was chosen |

The labels source picks issues by the `labels.todo` label, so setup writes that
name to `labels.todo` and to `queue.pick_label` together; say so to the user, since
`labels.todo` is also the label an issue goes back to when a claim is released.

Then, as in Step 6 of the skill: show the diff, ask once, write, run
`notme config --check` (restore the copy and stop on errors), and run
`notme config --explain` again to print the new queue summary.

## 5. Finish

- Labels: when the chosen label is missing from the tracker, run
  `notme labels-ensure --check` and ask before `notme labels-ensure`.
- Board: a board in the repo layer is trusted for writes only once a person
  confirms it. Print the exact command with this repo's origin and the board, for
  the user to run:

```
/not-me:allow-repo owner/name --board owner/number
```

- Run `notme doctor` and show its `queue` check: it warns when the label does not
  exist, the board is not confirmed or not found, or a column name is not one of the
  board's status options.
