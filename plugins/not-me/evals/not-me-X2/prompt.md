---
name: not-me-X2
description: "Nothing to do on any leg."
tags: [live, skill:not-me, class:outcome]
runs: 1
max_turns: 40
timeout_seconds: 600
allowed_tools: [Bash, Read, Write, Edit, Glob, Grep, Skill, Agent, TodoWrite]
expected_outcome: "NONE - nothing to do."
---

/not-me --bg
