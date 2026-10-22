---
type: llm
weight: 1
focus: trace
arm: both
---

Find the PR bodies this run wrote or upserted (`notme pr-open`, `notme pr-body`, `notme companion-prs`). Pass when every PR in the set lists the other PR under a `## Companion PRs` heading by owner/repo#N. Fail otherwise.
