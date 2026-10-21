---
type: tool_order
weight: 1
before: {tool: Bash, input_match: '--kind\s+DELEGATE'}
after: {tool: Bash, input_match: '--kind\s+AUDIT'}
arm: with-only
---

