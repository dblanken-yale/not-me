---
name: new-N3
description: "The first issue is already claimed; the free one is picked."
tags: [live, skill:new, class:outcome]
runs: 1
max_turns: 150
timeout_seconds: 1800
allowed_tools: [Bash, Read, Write, Edit, Glob, Grep, Skill, Agent, TodoWrite]
expected_outcome: "The claimed issue is untouched; one PR for the free issue."
---

/not-me:new --bg
