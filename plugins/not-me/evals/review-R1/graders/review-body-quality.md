---
type: llm
weight: 1
focus: trace
arm: both
---

Find the review body this run posted (the file passed to `notme review-post`). Pass when it opens with the verdict and a one-line summary (TL;DR first), each blocking finding names a file and line with the reason, nothing that is only a style nit is marked as blocking, and no text mentions an AI, a model or an assistant. Fail when no review body was written or any of these is missing.
