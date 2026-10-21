---
schema: 1                        # config format version
dimensions: [correctness, tests, acceptance-criteria, security, data-loss, docs, maintainability]  # what a review looks at
severity: [blocker, major, minor]  # finding levels, most severe first
checks: verify                   # run the verify gate as part of review
approve_label: ready_to_merge    # labels key applied on approval
changes_label: needs_work        # labels key applied when changes are requested
reviewer_profile: null           # path to a reviewer profile doc; null = none
auto_approve: false              # true lets review-post send APPROVE; read from the default and global layers only
---
# Review

## What counts as a finding

A bug, a missed acceptance criterion, a missing or wrong test, a security or
data-loss risk, or docs that no longer match the code.

## What never counts

Style preferences the linter does not enforce, and rewrites that do not change
behavior.

## Per-reviewer focus

Nothing extra by default. Add a line per reviewer login for what they care about.
