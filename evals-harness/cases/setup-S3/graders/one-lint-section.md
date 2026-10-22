---
type: regex
weight: 2
target: {source: file, path: '.claude/not-me/verify.md'}
match: count:1
arm: with-only
flags: m
---

^## Lint
