---
name: new-M1
description: "One issue needs a change in the main repo and the confirmed companion."
tags: [live, skill:new, class:outcome, smoke]
runs: 1
max_turns: 150
timeout_seconds: 1800
allowed_tools: [Bash, Read, Write, Edit, Glob, Grep, Skill, Agent, TodoWrite]
expected_outcome: "Companion PR first, then the main PR; both carry a Companion PRs section."
---

/not-me:new --bg
