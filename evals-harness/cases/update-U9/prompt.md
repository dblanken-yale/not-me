---
name: update-U9
description: "Fresh install with no config layer: the U1 rework as a dry run."
tags: [live, skill:update, class:outcome]
runs: 1
max_turns: 150
timeout_seconds: 1800
allowed_tools: [Bash, Read, Write, Edit, Glob, Grep, Skill, Agent, TodoWrite]
expected_outcome: "PUSHED marked dry-run; the ledger holds the push, replies and label swap."
---

/not-me:update --bg
