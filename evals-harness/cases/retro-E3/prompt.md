---
name: retro-E3
description: "The newest run log is clean."
tags: [live, skill:retro, class:outcome]
runs: 1
max_turns: 60
timeout_seconds: 600
allowed_tools: [Bash, Read, Write, Edit, Glob, Grep, Skill, Agent, TodoWrite]
expected_outcome: "RETRO with zero lessons and zero edits; verify.md unchanged."
---

/not-me:retro --bg
