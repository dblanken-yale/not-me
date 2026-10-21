---
name: setup-S3
description: "The repo layer already matches the answers; setup runs again."
tags: [live, skill:setup, class:outcome]
runs: 1
max_turns: 60
timeout_seconds: 600
allowed_tools: [Bash, Read, Write, Edit, Glob, Grep]
expected_outcome: "Nothing is duplicated: one Roster section, one roster row, one Lint section; no question; no label created."
---

/not-me:setup --answers .not-me-eval/answers.json
