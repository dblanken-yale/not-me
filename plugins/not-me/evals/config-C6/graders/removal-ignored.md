---
type: regex
weight: 2
target: last_message
match: contains
arm: both
flags: i
---

ignored[\s\S]{0,400}!develop|!develop[\s\S]{0,400}ignored
