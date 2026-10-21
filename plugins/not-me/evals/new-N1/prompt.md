---
name: new-N1
description: "Happy path: fix a failing add() and open one PR."
tags: [live, skill:new, class:outcome, smoke]
runs: 1
max_turns: 150
timeout_seconds: 1800
allowed_tools: [Bash, Read, Write, Edit, Glob, Grep, Skill, Agent, TodoWrite]
expected_outcome: "One PR against main closes the issue with a qualified reference; the issue moves to needs review; the run log has DELEGATE and AUDIT lines."
---

/not-me:new --bg
