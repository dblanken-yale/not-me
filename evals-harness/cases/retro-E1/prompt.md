---
name: retro-E1
description: "A finished run log shows CI runs a lint command the verify doc lacks."
tags: [live, skill:retro, class:outcome]
runs: 1
max_turns: 60
timeout_seconds: 600
allowed_tools: [Bash, Read, Write, Edit, Glob, Grep, Skill, Agent, TodoWrite]
expected_outcome: "RETRO with one lesson and one edit: verify.md pins make lint; the audit log holds the rollback diff."
---

/not-me:retro --bg
