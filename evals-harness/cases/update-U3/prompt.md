---
name: update-U3
description: "Every finding is a design call or out of scope."
tags: [live, skill:update, class:outcome]
runs: 1
max_turns: 150
timeout_seconds: 1800
allowed_tools: [Bash, Read, Write, Edit, Glob, Grep, Skill, Agent, TodoWrite]
expected_outcome: "PARKED with needs review, question and needs human; replies on both threads; no push."
---

/not-me:update --bg
