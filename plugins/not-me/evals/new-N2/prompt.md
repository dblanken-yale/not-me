---
name: new-N2
description: "Nothing carries the pick label."
tags: [live, skill:new, class:outcome]
runs: 1
max_turns: 150
timeout_seconds: 1800
allowed_tools: [Bash, Read, Write, Edit, Glob, Grep, Skill, Agent, TodoWrite]
expected_outcome: "NONE - idle; no GitHub write."
---

/not-me:new --bg
