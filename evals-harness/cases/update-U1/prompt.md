---
name: update-U1
description: "Happy path: one trusted thread asks to restore the zero guard."
tags: [live, skill:update, class:outcome, smoke]
runs: 1
max_turns: 150
timeout_seconds: 1800
allowed_tools: [Bash, Read, Write, Edit, Glob, Grep, Skill, Agent, TodoWrite]
expected_outcome: "PUSHED; the thread gets a reply and stays unresolved; the PR moves to needs review."
---

/not-me:update --bg
