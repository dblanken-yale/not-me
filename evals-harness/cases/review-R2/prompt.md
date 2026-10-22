---
name: review-R2
description: "A roster member's wrong fix."
tags: [live, skill:review, class:outcome, smoke]
runs: 1
max_turns: 150
timeout_seconds: 1800
allowed_tools: [Bash, Read, Write, Edit, Glob, Grep, Skill, Agent, TodoWrite]
expected_outcome: "REVIEWED changes with a finding at calc/core.py line 16; label needs work."
---

/not-me:review --bg
