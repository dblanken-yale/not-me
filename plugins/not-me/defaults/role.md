---
schema: 1                        # config format version
---
# Roles

Who must approve a pull request before it is ready to merge, and the pass label
each reviewer role earns on the way.

## Roles

| GitHub login | Role | Pass label |
|--------------|------|------------|

One row per reviewer from the roster in project.md. Role is a short name such as
code or functional. Pass label is the repo label a PR gets once a reviewer in that
role approves; leave it empty for no label.

## How approval works

With no rows, one approval moves a pull request from needs review to ready to
merge.

With at least one row, every roster reviewer (Reviewer = yes) must approve,
except the PR's author. Until they all have, the PR stays in needs review and
gets the pass label of each role with an approving reviewer. A reviewer whose
latest review is no longer an approval loses their role's pass label, unless
another reviewer in that role still approves. A reviewer with no row, or a role
with no pass label, still counts toward everyone approved; only the label is
skipped.

The rows here and in the global layer always apply. A repo-layer role.md may add
rows for new logins; a repo row that changes a row from those layers is ignored.
