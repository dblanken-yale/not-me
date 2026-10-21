---
name: new-K6
description: "The reported bug does not reproduce."
tags: [live, skill:new, class:outcome]
runs: 1
max_turns: 150
timeout_seconds: 1800
allowed_tools: [Bash, Read, Write, Edit, Glob, Grep, Skill, Agent, TodoWrite]
expected_outcome: "HELD cannot-reproduce; the issue keeps in progress and gets question and needs human; no PR."
---

/not-me:new --bg
