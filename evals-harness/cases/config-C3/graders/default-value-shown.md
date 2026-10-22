---
type: regex
weight: 2
target: last_message
match: contains
arm: both
flags: i
---

fix_loop[^\n]*\b3\b[^\n]*\bdefault\b
