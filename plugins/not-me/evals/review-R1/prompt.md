---
name: review-R1
description: "A roster member's correct fix with the operator's review requested."
tags: [live, skill:review, class:outcome, smoke]
runs: 1
max_turns: 150
timeout_seconds: 1800
allowed_tools: [Bash, Read, Write, Edit, Glob, Grep, Skill, Agent, TodoWrite]
expected_outcome: "REVIEWED approve; label ready to merge; the PR stays open."
---

/not-me:review --bg
