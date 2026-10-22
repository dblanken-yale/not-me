---
type: regex
weight: 2
target: {source: file, path: '.claude/not-me/verify.md'}
match: contains
arm: with-only
---

make lint
