---
name: allow-repo
description: "Show the repos and project boards not-me may write to, and add or remove one when the operator asks: runs the change in this session and shows exactly what changed (repo or board, this project or every project). Only a person typing the slash command can start it. Use when the operator runs /not-me:allow-repo, a run reported a write blocked for a repo, or they ask to allow a repo or board. NOT for editing config (use /not-me:config to see layers), NOT for repos and the board already named in the repo layer on the default branch (those count without it)."
argument-hint: "<owner/name> [--everywhere] | --board OWNER/NUMBER [--everywhere] | --list | --remove <owner/name> | --remove --board OWNER/NUMBER"
disable-model-invocation: true
---

# /not-me:allow-repo

The operator typing this command is the approval: run the change here, then show
it. Repos and the board named in `.claude/not-me/project.md` count as allowed once
that file is on the default branch, so this is for anything else, or for using a
new name before its pull request merges.

Arguments: `$ARGUMENTS`.

## Steps

1. `--list`, or no argument: run `notme allow-repo list` and print its `entries`
   (scope, repo or board, added). Say that entries with scope `*` apply to every
   project.
2. `<owner/name>`: run `notme allow-repo add owner/name`. With `--everywhere`, add
   `--everywhere`. With `--board OWNER/NUMBER` (alone or after the repo), run
   `notme allow-repo add --board OWNER/NUMBER` too, with the same `--everywhere`.
3. `--remove <owner/name>` or `--remove --board OWNER/NUMBER`: run
   `notme allow-repo remove` with the same arguments.
4. After each command, print its `summary` line as it is, then the `entries` it
   returned as a short list. The change and its line in the settings ledger (the
   `ledger` path) are what the operator can check and undo.
5. A failed command (non-zero exit): print its `error` and stop. Do not retry
   with other arguments.

## Rules

1. Run an add or a remove only for what the operator typed in this invocation.
   Never add a repo or board they did not name.
2. Never edit `allowed-repos.json` or a config file to allow a repo. The repo layer
   cannot add `allow_repos`.

## Checklist

- [ ] Ran exactly the add, remove or list the operator asked for.
- [ ] Printed each `summary` and the resulting `entries`.
