---
name: mode
description: "Show whether not-me is in dry-run or live mode and where that is set, switch to dry-run, or go live: runs the change in this session and shows exactly what changed (which layer, this project or every project). Only a person typing the slash command can start it. Use when the operator runs /not-me:mode, asks what mode not-me is in, wants to stop real writes, or wants to go live. NOT for editing other settings (use /not-me:config to see them), NOT for allowing a repo (use /not-me:allow-repo), NOT for checking the install (use /not-me:doctor)."
argument-hint: "[dry-run [--global] | live]"
disable-model-invocation: true
---

# /not-me:mode

Shows the current mode, or changes it. The operator typing this command is the
approval: run the change here, then show it.

Arguments: `$ARGUMENTS`.

## Steps

1. No argument: run `notme mode show` and tell the user:
   - the current mode, and where it is set (layer, file and line);
   - a repo-layer dry-run pin (`repo_pin`: file and line), if one is reported,
     because it keeps the project in dry-run even when the global layer says live;
   - what the mode means. `dry-run`: reads are real, every write goes only to the
     local ledger and nothing reaches GitHub. `live`: real writes, still through
     the gated path and the guard.
2. `dry-run`: run `notme mode dry-run`, or `notme mode dry-run --global` when the
   operator asked for every project. Without `--global` it sets the repo layer
   (this project only).
3. `live`: run `notme mode live`. It sets the global layer (every project); the
   repo layer can never turn live mode on. When the output has a `note`, print it:
   the repo layer still pins dry-run, and the operator removes that line to go live
   in this project.
4. After a change, print its `summary` line as it is, then the resulting mode. The
   change has a line in the settings ledger (the `ledger` path).
5. Anything else: print the argument hint and stop. A failed command: print its
   `error` and stop.

## Rules

1. Change the mode only as the operator typed it in this invocation.
2. Never edit a config file to set the mode. The commands above do that.

## Checklist

- [ ] Ran `notme mode show`, or exactly the change the operator asked for.
- [ ] Printed the `summary` and the resulting mode, and any `note`.
