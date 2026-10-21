---
name: doctor
description: "Check that not-me can run here: tools, GitHub auth and scopes, config, the guard self-test and the subagent depth setting, with install hints for this operating system. Use when the operator runs /not-me:doctor, a run reported exit 7 or 8, or after installing the plugin. NOT for repairing anything (it only reports), NOT for showing run state (use /not-me:status), NOT for explaining a setting (use /not-me:config)."
argument-hint: ""
---

# /not-me:doctor

Read-only checklist. It fixes nothing and installs nothing.

## Steps

1. Run `notme doctor`. Keep the JSON and the exit code.
2. Render `checks` as a checklist, one line each: pass or fail, `name`, `detail`,
   and "required" or "optional". Failed required checks first.
3. For every failed check, print the install hint the JSON gives for this
   operating system, exactly as given. Do not restate or invent a hint.
4. Run `notme guard selftest`. Report `ok` and `active`. Exit 8 means the guard is
   enabled but not working: every run stops with `FAILED - guard not working`.
5. Run `notme config --check`. Print each error with its layer, file and line. Exit
   4 means the config is invalid. Then run `notme config --explain` only if the
   operator asks about a warning, and print its `warnings`.

## Reading the exit code of `notme doctor`

| Exit | Meaning | What to say |
|------|---------|-------------|
| 0 | Every required check passed | Ready. Optional failures are only advice. |
| 7 | The only failed required checks are `gh`, `gh` auth or `gh` scopes | Install or sign in to `gh`, add the missing scope, then run again. |
| 1 | Another required check failed (python, git, config, guard) | Fix the named check first; the hint says how. |

Exit 7 is a missing dependency or scope, so nothing is wrong with the project.
Exit 1 means something in the setup itself is broken.

## Spawn depth

A `--bg` run with `models.strategy` set to `orchestrate` nests three subagents deep
and needs `CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH` of at least 3. If the doctor lists
it as failing, say so; a lower value only makes runs do the unit themselves.

## More help

Point to `docs/install.md` in the plugin repository for the full install steps
and requirements.

## Checklist

- [ ] Ran `notme doctor`, `notme guard selftest` and `notme config --check`.
- [ ] Listed every check with pass or fail and its detail.
- [ ] Printed the JSON's per-OS hint for each failure, unchanged.
- [ ] Explained exit 7 versus exit 1, and exit 8 for the guard.
- [ ] Pointed to `docs/install.md`; changed nothing.
