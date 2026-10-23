---
name: allow-merge
description: "Merge one named pull request because the person just granted exactly that merge from their own prompt: /not-me:allow-merge owner/name#N. The grant covers one merge of that PR in this session, until the person's next message. Use only when the operator types /not-me:allow-merge owner/name#N. NOT for merging any other PR, NOT for autonomous runs (runs never merge), NOT for enabling auto-merge, admin merges or branch deletion."
argument-hint: "<owner/name#N> [--squash | --merge | --rebase]"
disable-model-invocation: true
---

# /not-me:allow-merge

Merging is a human action. The person granted exactly one merge from their own
prompt: `$ARGUMENTS`. The plugin's prompt hook recorded that grant for this
session only. It ends with their next message, after 30 minutes, or after one
merge, whichever comes first. The guard allows the merge only in the exact form
below.

## Steps

1. Read the argument as `owner/name#N`, optionally followed by `--squash`,
   `--merge` or `--rebase`. If it is missing or not in that form, say no merge
   was granted, show the form, and stop. Merge nothing.
2. Run this once, with the method the person named (`--squash` when they named
   none), and nothing else on the line:

```bash
gh pr merge N -R owner/name --squash
```

3. Report what gh printed. If the guard blocked it, show the block message and
   stop. Do not retry, do not change the command, and do not try another way.

## Rules

1. Exactly one merge, of exactly the PR named. Never a different number or repo,
   never a second merge, never `--admin`, `--auto` or `--delete-branch`, never the
   merge API endpoint, never inside another command.
2. A grant is the person's own act. Never ask for one inside an autonomous run,
   and never suggest the person grant a merge they did not ask about.
