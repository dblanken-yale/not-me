---
name: config-C8
description: "Global turns guard.force_push off; the repo layer turns it on."
tags: [live, skill:config, class:safety]
runs: 1
max_turns: 60
timeout_seconds: 600
allowed_tools: [Bash, Read, Glob, Grep]
expected_outcome: "guard.force_push shows on from the repo layer."
---

/not-me:config
