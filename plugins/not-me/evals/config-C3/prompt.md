---
name: config-C3
description: "No repo layer and no global value for limits.fix_loop."
tags: [live, skill:config, class:outcome]
runs: 1
max_turns: 60
timeout_seconds: 600
allowed_tools: [Bash, Read, Glob, Grep]
expected_outcome: "limits.fix_loop shows 3 from the default layer."
---

/not-me:config
