---
name: allow-repo
description: "Show the repos and project boards not-me may write to, and print the exact terminal command a person runs to allow or remove one. Adding or removing is a human action and this skill never does it. Use when the operator runs /not-me:allow-repo, a run reported a write blocked for a repo, or they ask to allow a repo. NOT for editing config (use /not-me:config to see layers), NOT for running the add or remove itself (only a person at a terminal can)."
argument-hint: "<owner/name> [--everywhere] | --board OWNER/NUMBER | --list | --remove <owner/name>"
disable-model-invocation: true
---

# /not-me:allow-repo

Changing the allow list is a human action. The CLI refuses unless stdin is a real
terminal and the person types the operator token (created once with
`notme doctor --init-token`) at its hidden prompt; scripts can set
`NOT_ME_OPERATOR_CONFIRM` to the token instead. The guard blocks the command in a
session, so this skill never runs an add or a remove. It lists, and prints the
command. `${CLAUDE_PLUGIN_ROOT}` is substituted at load time with this plugin's
directory, but a person's terminal does not have it: print absolute paths.

Arguments: `$ARGUMENTS`.
## Steps

1. `--list`, or no argument: run `notme allow-repo list` and print its `entries`
   (scope, repo or board, added). Say that the global entries apply everywhere.
2. `<owner/name>`: run `notme mode show`. Print the command below, with `<notme>`
   its `launcher` and `<project>` this checkout's top folder (no token yet,
   `token_present` false: give `"<notme>" doctor --init-token` first):

```
cd "<project>" && "<notme>" allow-repo add owner/name
```

3. `--everywhere` adds it to the command (every project). `--board OWNER/NUMBER`
   goes in place of the repo name. `--remove <owner/name>` uses `allow-repo remove`.
4. Stop. On Windows (`shell` `git-bash`), prefix `bash` and say to use Git Bash. Say to run it in a terminal outside the session, then `--list` to confirm.

## Rules

1. Never run an add or a remove, even when asked, never set
   `NOT_ME_OPERATOR_CONFIRM` yourself, and never read the token. A refusal carries the command; do not retry.
2. Never edit a config file to add a repo. The repo layer cannot add `allow_repos`.

## Checklist

- [ ] Ran only `notme allow-repo list` and `notme mode show`.
- [ ] Printed the full command for a person; ran no add or remove.
