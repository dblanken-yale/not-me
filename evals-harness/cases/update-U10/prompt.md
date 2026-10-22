---
name: update-U10
description: "An earlier run's summary comment is already on the PR."
tags: [live, skill:update, class:outcome]
runs: 1
max_turns: 150
timeout_seconds: 1800
allowed_tools: [Bash, Read, Write, Edit, Glob, Grep, Skill, Agent, TodoWrite]
expected_outcome: "PUSHED; the summary comment is upserted, so one marker comment remains."
---

/not-me:update --bg
