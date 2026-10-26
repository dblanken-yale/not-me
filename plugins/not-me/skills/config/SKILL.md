---
name: config
description: "Explain the effective not-me configuration: every key with its value and the layer, file and line it came from, prose docs per layer, ignored loosening attempts, and warnings. Use when the operator runs /not-me:config or asks why a setting has a value or how to change it. NOT for editing any setting (a person changes the files), NOT for checking tools or auth (use /not-me:doctor), NOT for run state (use /not-me:status)."
argument-hint: "[--layer default|global|repo]"
---

# /not-me:config

Read-only explanation. It never edits a config file.

Arguments: `$ARGUMENTS`. An optional `--layer` limits the output to one layer.

## Steps

1. Run `notme config --explain`, adding `--layer <name>` when given. Exit 4 means
   the config is invalid: run `notme config --check` and print each error with its
   layer, file and line, then stop.
2. Print the layer directories from `layers` (default, global, repo) and say which
   exist. A missing layer is fine; defaults fill the gaps.
3. Render `keys` as a table, one row per key:

   | Key | Effective value | Layer | File:line |
   |-----|-----------------|-------|-----------|

   Use `value`, `source.layer`, `source.path` and `source.line`. Show `contributors`
   for union lists and `removed` entries for `!name` removals.
4. Render `bodies`: for each of the five docs, the layers used and each section's
   layer. A prose doc resolves per file: the highest layer with the file wins,
   unless it declares `extends`.
5. Render `picking`: the rules in effect (`rules`, quoted) and the `layer` they came
   from (`default` is the impact default; a `## Picking work` section in a global or
   repo `project.md` replaces it), then `fields` and `target_epics`.
6. List every `ignored` entry (layer, value, reason) and every string in `warnings`.
   These are loosening attempts the CLI dropped, or schema notes. Report them.

## How to change a value

Edit the file in the layer you mean and run `/not-me:config` again to check it.

| To change | Edit |
|-----------|------|
| The plugin default | Not edited; it ships with the plugin |
| One machine or operator | The global layer, in the not-me directory under the Claude config |
| The whole team | The repo layer, in `.claude/not-me/` in the project |

Per key, repo beats global beats default. Maps merge key by key; scalars and plain
lists replace. `null` removes a lower scalar. Union lists only grow, and `!name`
removes one entry.

## What the repo layer cannot loosen

The repo layer is committed by anyone with push access, so it may only tighten:

1. `guard` rules: it may turn one on, never off.
2. `mode`: it may set `dry-run`, never `live` over a lower `dry-run`.
3. `enabled`: it may set `false`, never `true` over a lower `false`.
4. `protected_branches`: it may add names, never remove one.
5. `allow_repos`: it may not add any. Only the operator's own allow list can
   (see `/not-me:allow-repo`).

A value that tries is shown as ignored. Setup upgrades (`setup --upgrade`) are the
operator's to run.

## Checklist

- [ ] Ran `notme config --explain`; edited nothing.
- [ ] Showed every key with value, layer and file:line.
- [ ] Showed the layer used by each prose doc and section, and the picking rules.
- [ ] Listed ignored loosening attempts and warnings.
- [ ] Said which layer to edit, and what the repo layer cannot loosen.
