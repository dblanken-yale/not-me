---
name: config-C1
description: "Global and repo layers both set limits.fix_loop."
tags: [live, skill:config, class:outcome]
runs: 1
max_turns: 60
timeout_seconds: 600
allowed_tools: [Bash, Read, Glob, Grep]
expected_outcome: "limits.fix_loop shows 2 from the repo layer."
---

/not-me:config
