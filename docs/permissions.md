# Permission rules for not-me

In auto permission mode, Claude Code checks each tool call before it runs. Routine not-me housekeeping, such as closing an issue or posting a comment, can be refused there as an external write, and the run stops until you say it again. Allow rules in your settings let that work run without a stop.

These rules change only what the session's permission check asks about. The not-me guard still runs before every matching command in every permission mode, and it still blocks what it blocks (see [guards.md](guards.md)). Every GitHub write the CLI makes still goes through its own gated path.

## Recommended rules

Add these to `permissions.allow` in your user settings file, `~/.claude/settings.json`. A project's `.claude/settings.json` or `.claude/settings.local.json` works too.

| Rule | Why |
| --- | --- |
| `Bash(gh issue close:*)` | Closing an issue a person asked to close, or one a merged PR finished |
| `Bash(gh issue comment:*)` | Posting a comment on an issue when a person asks for one in the session |
| `Bash(gh issue edit:*)` | Labels, assignees and body edits on an issue when a person asks in the session |
| `Bash(gh pr comment:*)` | Posting a comment on a pull request when a person asks in the session |
| `Bash(notme:*)` | The not-me CLI, which every skill calls for every GitHub action; it gates its own writes |
| `Bash(<launcher>:*)` | The same CLI by its full path, the form skills use when `notme` is not on the path. `<launcher>` is the `launcher` that `notme mode show` prints; it changes when the plugin updates |

For example:

```json
{
  "permissions": {
    "allow": [
      "Bash(gh issue close:*)",
      "Bash(gh issue comment:*)",
      "Bash(gh issue edit:*)",
      "Bash(gh pr comment:*)",
      "Bash(notme:*)"
    ]
  }
}
```

A broader rule that starts with the same words, such as `Bash(gh issue:*)`, also covers the narrower ones.

## Not recommended

- `Bash(gh:*)` or `Bash(gh *)`: it also allows deleting repositories and releases, changing settings and secrets, and anything else `gh` can do. The guard blocks many of those, but it is not meant to be the only check.
- `Bash` or `Bash(*)`: it allows every shell command.
- Rules for `git push`, `gh pr merge` or `gh repo`: not-me pushes through its CLI, and merging is always a person's call.

## Checking

`/not-me:doctor` reads `permissions.allow` from `~/.claude/settings.json` (or the folder `CLAUDE_CONFIG_DIR` points to) and from this checkout's `.claude/settings.json` and `.claude/settings.local.json`. Its `permissions` check lists the recommended rules none of them has and prints the JSON to add. It also names a blanket `gh` or `Bash` rule. It never edits a settings file; you add the rules yourself.
