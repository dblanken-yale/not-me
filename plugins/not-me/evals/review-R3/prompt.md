---
name: review-R3
description: "Only the operator's own PR is waiting, with no review requested."
tags: [live, skill:review, class:outcome]
runs: 1
max_turns: 150
timeout_seconds: 1800
allowed_tools: [Bash, Read, Write, Edit, Glob, Grep, Skill, Agent, TodoWrite]
expected_outcome: "NONE - idle; nothing posted."
---

/not-me:review --bg
