---
type: llm
weight: 1
focus: trace
arm: both
---

Find the review-thread replies and the summary comment this run wrote (the files passed to `notme reply-thread` and `notme comment-upsert`). Pass when each reply says what was done or why not with a reason, the summary leads with who is owed what or that nothing is owed, and no text mentions an AI, a model or an assistant. Fail when no reply was written or any of these is missing.
