---
name: pause
description: "Pause not-me or lift a pause, for this project or for every project: runs the change in this session and shows exactly what changed. While paused, the queue picks nothing and claims are refused. Only a person typing the slash command can start it. Use when the operator runs /not-me:pause, wants not-me to stop picking up work, or wants it to start again. NOT for the paused label on one issue (a person sets or removes that label), NOT for dry-run (use /not-me:mode), NOT for showing run state (use /not-me:status)."
argument-hint: "on | off [--global]"
disable-model-invocation: true
---

# /not-me:pause

The operator typing this command is the approval: run the change here, then show
it.

Arguments: `$ARGUMENTS`.

## Steps

1. `on`: run `notme pause on`. `off`: run `notme pause off`. Add `--global` only
   when the operator asked for every project; without it the pause is for this
   project only.
2. No argument, or anything else: print the argument hint and stop.
3. Print the command's `summary` line as it is. A change also has a line in the
   settings ledger (the `ledger` path). A failed command: print its `error` and
   stop.

## Rules

1. Change the pause only as the operator typed it in this invocation.
2. Never create or delete a `PAUSED` file by hand. The command does that.

## Checklist

- [ ] Ran exactly `notme pause on` or `notme pause off` as asked.
- [ ] Printed the `summary`.
