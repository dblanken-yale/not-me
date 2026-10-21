---
name: setup-S1
description: "Fresh repo, non-interactive setup from an answers file."
tags: [live, skill:setup, class:outcome]
runs: 1
max_turns: 60
timeout_seconds: 600
allowed_tools: [Bash, Read, Write, Edit, Glob, Grep]
expected_outcome: "The five repo-layer docs are written from the answers; no question asked; no label created."
---

/not-me:setup --answers .not-me-eval/answers.json
