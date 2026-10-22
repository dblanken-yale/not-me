---
name: not-me-X1
description: "A needs-work PR and a ready issue; only the update leg runs."
tags: [live, skill:not-me, class:outcome]
runs: 1
max_turns: 40
timeout_seconds: 1800
allowed_tools: [Bash, Read, Write, Edit, Glob, Grep, Skill, Agent, TodoWrite]
expected_outcome: "PUSHED from the update leg; the ready issue is untouched."
---

/not-me --bg
