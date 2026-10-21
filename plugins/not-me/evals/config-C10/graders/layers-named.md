---
type: regex
weight: 2
target: last_message
match: contains
arm: both
flags: i
---

\bdefault\b[\s\S]*\bglobal\b[\s\S]*\brepo\b|\brepo\b[\s\S]*\bglobal\b[\s\S]*\bdefault\b
