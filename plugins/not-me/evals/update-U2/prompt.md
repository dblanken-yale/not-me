---
name: update-U2
description: "The only needs-work PR is not assigned to the operator."
tags: [live, skill:update, class:outcome]
runs: 1
max_turns: 150
timeout_seconds: 1800
allowed_tools: [Bash, Read, Write, Edit, Glob, Grep, Skill, Agent, TodoWrite]
expected_outcome: "NONE - idle; no GitHub write."
---

/not-me:update --bg
