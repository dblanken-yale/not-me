---
name: status
description: "Show the state of not-me at a glance: enabled or paused, the active run, items waiting on a person, stale claims, and the last result of each skill. Read-only. Use when the operator runs /not-me:status or asks what not-me is doing, who is blocking it, or whether it is paused. NOT for changing anything (pause and allow-list changes are human actions), NOT for checking the install (use /not-me:doctor), NOT for showing settings (use /not-me:config)."
argument-hint: ""
---

# /not-me:status

Read-only report. It never writes (no label, log or post; never the `<!-- not-me:status -->` marker).

## Steps

1. Run `notme status`. Keep the JSON. Exit 4 means the config is invalid: say so and
   point to `/not-me:config`.
2. Print the report below, in this order. Leave out a section that is empty and say
   "none" for it.

### State

- Enabled or disabled, `mode`, and which of `paused.global` and `paused.project` is true.
- `repo`, `tracker`, `operator`, and the `queue` summary (if not `ok`, it cannot pick).
- Active run: `skill`, `ref`, `started`, or "no active run".
- Picking: the `picking` line as given (rules and their layer, or the impact default).

### Waiting on a person

For each entry in `items`: kind and number, title, URL, and its `states`. Say who is
owed what: `question`, a named person owes an answer; `needs_human`, a run stopped and a
person must act; `paused`, skipped until a person removes the label.

### Stale claims and interrupted runs

List each `stale_claims` entry (URL, age) and each `interrupted_runs` entry (a run that
died before it finished: skill, ref, run_id, file). A person decides; not-me never releases a claim.

### Queue drift

When `queue_drift` is not empty, list each entry: URL, label, Status, and `skip` (why
it cannot be fixed). A person fixes the rest with `notme queue-resync`. When
`queue_drift_truncated` is true, say the board has more items than were read.

### Last run per skill

One row per key in `last_runs`: skill, `ref`, `status` and `file`.

### Warnings

Print each string in `warnings`. A "GitHub items not read" warning means the item
sections are incomplete, not empty.

## Where to pause or stop

`enabled: false` in the global or repo layer, or a `PAUSED` file in the global or project
config directory, stops every leg. Removing either is a human action; do not offer to.

## Checklist

- [ ] Ran only `notme status`; changed nothing.
- [ ] Showed state (with the queue summary), items with URLs and who is owed what, stale claims, queue drift,
  last runs, warnings, and where the pause switches live.
