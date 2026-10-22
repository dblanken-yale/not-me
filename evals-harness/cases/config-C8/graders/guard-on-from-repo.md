---
type: regex
weight: 2
target: last_message
match: contains
arm: both
flags: i
---

force_push[^\n]*\bon\b[^\n]*\brepo\b
