---
name: status
description: "Show the state of not-me at a glance: enabled or paused, the active run, items waiting on a person, stale claims, and the last result of each skill. Read-only. Use when the operator runs /not-me:status or asks what not-me is doing, who is blocking it, or whether it is paused. NOT for changing anything (pause and allow-list changes are human actions), NOT for checking the install (use /not-me:doctor), NOT for showing settings (use /not-me:config)."
argument-hint: ""
---

# /not-me:status

Read-only report. It never writes: no label, no log, no post. It owns the marker
`<!-- not-me:status -->` only to say it never posts it.

## Steps

1. Run `notme status`. Keep the JSON. Exit 4 means the config is invalid: say so and
   point to `/not-me:config`.
2. Print the report below, in this order. Leave out a section that is empty and say
   "none" for it.

### State

- Enabled or disabled, and `mode` (`dry-run` or `live`).
- Paused: say which of `paused.global` and `paused.project` is true.
- `repo`, `tracker`, `operator`.
- Active run: `skill`, `ref`, `started`, or "no active run".
- Picking: the `picking` line as given (rules and their layer, or the impact default).

### Waiting on a person

For each entry in `items`: kind and number, title, URL, and its `states`
(`needs_human`, `question`, `paused`). Say who is owed what:

- `question`: a named person owes an answer. `needs_human`: a run stopped and a
  person must act. `paused`: skipped until a person removes the label.

### Stale claims

List each entry in `stale_claims` with its URL and age. Say a person decides: not-me
never releases or takes over a claim by itself.

### Last run per skill

One row per key in `last_runs`: skill, `ref`, `status` (the one-line result) and
`file`.

### Warnings

Print each string in `warnings`. A "GitHub items not read" warning means the item
sections are incomplete, not empty.

## Where to pause or stop

`enabled: false` in the global or repo layer, or a `PAUSED` file in the global or
project config directory, stops every leg. Removing either is a human action; say
so and do not offer to do it.

## Checklist

- [ ] Ran only `notme status`; changed nothing.
- [ ] Showed state, items with URLs and who is owed what, stale claims, last runs.
- [ ] Printed warnings and named where the pause switches live.
