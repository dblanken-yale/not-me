---
name: ticket-T2
description: "Check an issue that has no acceptance criteria."
tags: [live, skill:ticket, class:outcome]
runs: 1
max_turns: 60
timeout_seconds: 600
allowed_tools: [Bash, Read, Write, Edit, Glob, Grep, Skill]
expected_outcome: "TICKET checked with at least one missing readiness item; no label added."
---

Check whether the open issue whose title starts with `[eval:` is ready to be picked up. Ignore the `eval-lock` issue.

1. Find its number with `gh issue list --state open --json number,title` (a read).
2. Run the not-me ticket skill through the Skill tool: skill `not-me:ticket`, arguments `--check <number>`.
3. Do not answer any question the skill would ask; leave the issue as it is.
4. End your reply with the exact TICKET result line and nothing after it.
