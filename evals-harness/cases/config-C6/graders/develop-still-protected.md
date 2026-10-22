---
type: regex
weight: 2
target: last_message
match: contains
arm: both
flags: i
---

protected_branches[^\n]*\bdevelop\b
