"""The `expect` vocabulary: pure data, importable without the rest of the harness.

scripts/lint-evals.py reads GRADED_KEYS to check that a scenario's `expect` block is fully mapped.
generic.py asserts at import time that its mapping covers exactly GRADED_KEYS.
"""
from __future__ import annotations

# Keys that describe the scenario and are not graded against GitHub state. `result` is the skill's
# final one-line result word; the eval case's own grader checks it on the last message.
METADATA_KEYS = frozenset({"skill", "result", "note", "description"})

GRADED_KEYS = frozenset({
    "labels_exact", "pr_exists", "pr_body_contains", "push_order", "closing_ref_qualified",
    "commits_conventional", "only_paths", "no_force_push", "protected_unchanged", "marker_count",
    "proof_comment", "refusal_comment", "threads_replied_not_resolved", "untrusted_untouched",
    "review_event", "pr_open_not_merged", "no_writes_since", "companion_untouched",
    "guard_blocks", "branch_exists", "branches_created", "log_lines", "comment_anchor",
})

KNOWN_KEYS = METADATA_KEYS | GRADED_KEYS
