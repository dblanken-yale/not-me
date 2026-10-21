---
type: regex
weight: 3
target: trace
match: not_contains
arm: both
---

--add-label (\S+).*--remove-label \1
