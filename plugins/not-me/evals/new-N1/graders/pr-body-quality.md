---
type: llm
weight: 1
focus: trace
arm: both
---

Find the pull request body this run wrote (the file it passed to `notme pr-open` or `notme pr-body`). Pass when the body opens with a short summary of what changed before any detail, explains how the change was tested, and never mentions an AI, a model or an assistant. Fail when no PR body was written or any of these is missing.
