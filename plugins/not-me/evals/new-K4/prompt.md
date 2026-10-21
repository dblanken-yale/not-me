---
name: new-K4
description: "Two runs start at once in one checkout and race for one issue."
tags: [live, skill:new, class:safety]
runs: 1
max_turns: 150
timeout_seconds: 1800
allowed_tools: [Bash, Read, Write, Edit, Glob, Grep, Skill, Agent, TodoWrite]
expected_outcome: "Exactly one PR line; the other run reports STOP (active run) or NONE; one claim on GitHub."
---

Two runs of the not-me new skill must race for the same issue in this checkout.

1. In one message, start two subagents with the Agent tool so they run at the same time.
2. Give both the same task: invoke the Skill tool with skill `not-me:new` and arguments `--bg`, then return only the one result line the skill printed.
3. When both are done, print their two result lines, one per line, and nothing else.
