---
name: ticket-T1
description: "Create one ready issue in the repo's own ticket format."
tags: [live, skill:ticket, class:outcome]
runs: 1
max_turns: 60
timeout_seconds: 600
allowed_tools: [Bash, Read, Write, Edit, Glob, Grep, Skill]
expected_outcome: "TICKET created through notme followup; the body has Problem, Acceptance criteria and Out of scope."
---

/not-me:ticket --title "Add a pow helper to calc" pow(a, b) should return a to the power b. Acceptance: pow(2, 3) == 8, pow(5, 0) == 1, and make test passes. Out of scope: negative exponents and a CLI.
