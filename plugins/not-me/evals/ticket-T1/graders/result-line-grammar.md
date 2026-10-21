---
type: regex
weight: 2
target: last_message
match: contains
arm: both
flags: m
---

^(NONE|PR|PUSHED|PARKED|HELD|REVIEWED|PROVED|FAILED|STOP|TICKET|RETRO)\b
