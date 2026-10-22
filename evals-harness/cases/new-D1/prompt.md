---
name: new-D1
description: "The implementation goes to the implement model and is audited."
tags: [live, skill:new, class:quality]
runs: 1
max_turns: 150
timeout_seconds: 1800
allowed_tools: [Bash, Read, Write, Edit, Glob, Grep, Skill, Agent, TodoWrite]
expected_outcome: "An Agent dispatch for the implementation, a DELEGATE log line before an AUDIT line, then one PR."
---

/not-me:new
