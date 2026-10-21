---
name: config-C10
description: "All three layers are present."
tags: [live, skill:config, class:quality]
runs: 1
max_turns: 60
timeout_seconds: 600
allowed_tools: [Bash, Read, Glob, Grep]
expected_outcome: "The default, global and repo layers are named and no personal path is printed."
---

/not-me:config
