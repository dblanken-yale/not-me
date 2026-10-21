# Setup: labels

not-me tracks work by eleven abstract states. Each maps to one label name in the
tracker under `labels:` in `project.md`. These are the recommended names, which are
also the defaults.

| State | Recommended label | Meaning |
|-------|-------------------|---------|
| `todo` | to do | ready to be picked up |
| `in_progress` | in progress | claimed and being worked |
| `needs_review` | needs review | PR open and waiting for a reviewer |
| `needs_work` | needs work | reviewer sent the PR back |
| `ready_to_merge` | ready to merge | approved |
| `question` | question | waiting on an answer from a person |
| `blocked` | blocked | cannot move until something else lands |
| `epic` | epic | parent issue; never picked up directly |
| `needs_help` | needs help | not-me gave up and asks for a person |
| `needs_human` | needs human | a run stopped and needs a person to act |
| `paused` | paused | skip this issue until the label is removed |

## The choice per state

Show a table: state, recommended name, the closest existing repo label (same name
case-insensitively, else a likely synonym such as "ready" for `todo` or "wip" for
`in_progress`). The user picks one of:

1. **create**: use the recommended name; it is created later if missing.
2. **map to an existing label**: use that label's exact name.
3. **skip**: keep the recommended name in the config and create nothing now.

## Checks before writing

1. Two states may not share one label name, compared case-insensitively. Check the
   mapping before writing; `notme config --check` reports the same thing as an error.
   Ask again for the clashing states.
2. A label name may not start with a dash.
3. Write only the states whose name differs from the default. An unlisted state
   keeps its default name.

## Creating labels

1. After the docs are written and `notme config --check` passes, run
   `notme labels-ensure --check` and show `missing`.
2. Ask once: "Create these N labels in owner/name?" Only on an explicit yes, run
   `notme labels-ensure`. It creates every missing configured label, so when any
   state was skipped, say so in the question.
3. In dry-run the result carries `"dry_run": true` and nothing was created; the
   labels are only in the ledger. Say that, and that the same command creates
   them once `mode` is live.
