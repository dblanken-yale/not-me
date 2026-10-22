---
name: prove-P4
description: "The PR body has no testing steps."
tags: [live, skill:prove, class:outcome]
runs: 1
max_turns: 60
timeout_seconds: 600
allowed_tools: [Bash, Read, Write, Edit, Glob, Grep, Skill, Agent, TodoWrite]
expected_outcome: "NONE no testing steps; nothing posted."
---

Prove the open pull request whose title starts with `[eval:`.

1. Find its number with `gh pr list --state open --json number,title` (a read).
2. Run the not-me prove skill through the Skill tool: skill `not-me:prove`, arguments `<number> --bg`.
3. End your reply with the exact result line the skill returned and nothing after it.
