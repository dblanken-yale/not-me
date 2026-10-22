---
name: new-N6
description: "Fresh install with no config layer: defaults apply and the run is a dry run."
tags: [live, skill:new, class:outcome, smoke]
runs: 1
max_turns: 150
timeout_seconds: 1800
allowed_tools: [Bash, Read, Write, Edit, Glob, Grep, Skill, Agent, TodoWrite]
expected_outcome: "PR result marked dry-run; the ledger holds the intended writes; GitHub is unchanged."
---

/not-me:new --bg
