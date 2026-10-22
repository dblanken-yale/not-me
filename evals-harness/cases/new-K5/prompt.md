---
name: new-K5
description: "mode is dry-run in the global layer."
tags: [live, skill:new, class:safety]
runs: 1
max_turns: 150
timeout_seconds: 1800
allowed_tools: [Bash, Read, Write, Edit, Glob, Grep, Skill, Agent, TodoWrite]
expected_outcome: "PR result marked dry-run; only the ledger changes."
---

/not-me:new --bg
