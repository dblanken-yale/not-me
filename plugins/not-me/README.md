# not-me

not-me is a project-agnostic autonomous dev loop for Claude Code, shipped as a plugin and marketplace. It picks up ready GitHub issues, implements them, opens PRs, reworks PRs after review, reviews PRs, and proves PRs on a deploy target.

It is configured per repo by markdown docs in `.claude/not-me/` (layered: plugin defaults, then `~/.claude/not-me/`, then the repo). A stdlib-only Python CLI, `notme`, does all GitHub mechanics. A PreToolUse guard hook blocks writes to other repos, commits, pushes, and merges to protected branches, force-pushes, and destructive git. Works in the Claude Code CLI and desktop.

## Quickstart

Install (coming in 0.1.0):

```
/plugin marketplace add dblanken-yale/not-me
/plugin install not-me@not-me
```

Then, in the repo you want to automate (coming in 0.1.0):

```
/not-me:doctor
/not-me:setup
/not-me --dry-run
```

Update with:

```
/plugin update not-me@not-me
```

## Commands (coming in 0.1.0)

| Command | What it does |
| --- | --- |
| `/not-me` | Router: picks the most valuable work and runs the right skill |
| `/not-me:new` | Pick up a ready GitHub issue, implement it, open a PR |
| `/not-me:update` | Address review feedback on a `needs work` PR |
| `/not-me:review` | Review `needs review` PRs, approve or send back |
| `/not-me:prove` | Run a PR's testing steps on a deploy target and post proof |
| `/not-me:retro` | Review recent runs and suggest config improvements |
| `/not-me:ticket` | Create or groom an issue |
| `/not-me:status` | Show the queue and recent activity |
| `/not-me:setup` | Guided per-repo setup of `.claude/not-me/` |
| `/not-me:config` | View or change configuration |
| `/not-me:doctor` | Check requirements, config, and guard health |
| `/not-me:allow-repo` | Allow the guard to write to another repo |

## Requirements

git >= 2.30, gh >= 2.40, python3 >= 3.9.
