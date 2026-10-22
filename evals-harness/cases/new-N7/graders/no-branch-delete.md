---
type: regex
weight: 3
target: trace
match: not_contains
arm: both
---

push\s+\S+\s+(--delete|:)\S*|branch\s+-D\b
