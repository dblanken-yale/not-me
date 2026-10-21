---
name: allow-repo
description: "Show the repos and project boards not-me may write to, and print the exact terminal command a person runs to allow or remove one. Adding or removing is a human action and this skill never does it. Use when the operator runs /not-me:allow-repo, a run reported a write blocked for a repo, or they ask to allow a repo. NOT for editing config (use /not-me:config to see layers), NOT for running the add or remove itself (only a person at a terminal can)."
argument-hint: "<owner/name> [--everywhere] | --board OWNER/NUMBER | --list | --remove <owner/name>"
disable-model-invocation: true
---

# /not-me:allow-repo

Changing the allow list is a human action. The CLI refuses unless stdin is a real
terminal and `NOT_ME_OPERATOR_CONFIRM` holds the operator's GitHub login, and the
guard blocks the command inside a session. This skill therefore never runs an add
or a remove. It lists, and it prints the command for a person.

`${CLAUDE_PLUGIN_ROOT}` below is substituted at load time with this plugin's
directory, so a printed command works as written.

Arguments: `$ARGUMENTS`.
## Steps

1. `--list`, or no argument: run `notme allow-repo list` and print its `entries`
   (scope, repo or board, added). Say that the global entries apply everywhere.
2. `<owner/name>`: print the command below for the person to run in their own
   terminal, with `<login>` replaced by the operator login from `notme me`:

```
NOT_ME_OPERATOR_CONFIRM=<login> "${CLAUDE_PLUGIN_ROOT}/bin/notme" allow-repo add owner/name
```

3. `--everywhere` adds it to the command (every project). `--board OWNER/NUMBER`
   goes in place of the repo name.
4. `--remove <owner/name>`: print this command instead:

```
NOT_ME_OPERATOR_CONFIRM=<login> "${CLAUDE_PLUGIN_ROOT}/bin/notme" allow-repo remove owner/name
```

5. Stop. Say to run it in a terminal outside the session, then `--list` to confirm.

## Rules

1. Never run an add or a remove, even when asked, and never set
   `NOT_ME_OPERATOR_CONFIRM` yourself. A refusal carries the command; do not retry.
2. Never edit a config file to add a repo. The repo layer cannot add `allow_repos`.

## Checklist

- [ ] Ran only `notme allow-repo list` and `notme me`.
- [ ] Printed the full command for a person; ran no add or remove.
