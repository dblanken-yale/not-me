---
schema: 1                        # config format version; setup upgrades older files
mode: dry-run                    # dry-run | live
enabled: true                    # false turns not-me off for this layer and above
tracker: auto                    # owner/name of the repo that holds issues; auto = origin
repos: []                        # [] = [tracker]; items {name, path: auto, default_branch: auto, role: primary|companion}, push order
protected_branches: [main, master, develop]  # never committed to directly; union across layers, "!name" removes
operator: auto                   # GitHub login; auto = gh api user
queue:                           # where work is picked from
  source: labels                 # labels | board
  pick_label: to do              # label that marks an issue as ready to pick up
  opt_in_label: null             # extra label an issue must also carry; null = none required
  board: {owner: null, number: null, status_field: Status, priority_fields: []}  # project board used when source = board
  board_states: {todo: To Do, in_progress: In progress, in_review: In review, blocked: Blocked}  # board column names
labels:                          # label names used in the tracker, keyed by role
  todo: to do                    # ready to be picked up
  in_progress: in progress       # claimed and being worked
  needs_review: needs review     # PR open and waiting for a reviewer
  needs_work: needs work         # reviewer sent the PR back
  ready_to_merge: ready to merge # approved
  question: question             # waiting on an answer from a person
  blocked: blocked               # cannot move until something else lands
  epic: epic                     # parent issue; never picked up directly
  needs_help: needs help         # not-me gave up and asks for a person
  needs_human: needs human       # a run stopped and needs a person to act (parked, held, or gave up)
  paused: paused                 # skip this issue until the label is removed
reviewers: {default: []}         # GitHub logins requested as reviewers, keyed by repo name or default
router: [update, new]            # order in which the router tries work kinds
workspace: {new: checkout, update: checkout, review: worktree}  # checkout | worktree per work kind
guard: {other_repo: on, protected_branches: on, force_push: on, destructive_git: on}  # on | off; off needs confirm_guard_off in the same file
confirm_guard_off: []            # guard rules this file deliberately turns off
allow_repos: []                  # extra owner/name repos the guard lets not-me touch; union across layers
logs: {root: null}               # directory for run logs; null = <git-common-dir>/not-me/logs in the checkout
markers: {legacy: []}            # older run-marker strings still recognised; union across layers
attribution: inherit             # inherit | none | "<text>"
limits: {fix_loop: 3, ci_wait_minutes: 20, claim_stale_hours: 8}  # fix rounds, CI wait, hours before a claim is stale
models: {strategy: orchestrate, single_model: opus, bg: opus, implement: sonnet, review: opus, triage: haiku, verifier: auto, max_tries: 3}  # strategy: orchestrate | single
notify: {command: null}          # shell command run with a summary when a run ends; null = none
retro: {file_upstream: false}    # true files retro findings as issues on the plugin repo
---
# Project

Who counts on this project. not-me reads this roster to decide whose comments,
reviews and issues it acts on.

## Roster

| GitHub login | Name | Tier | Assignable | Reviewer |
|--------------|------|------|------------|----------|

Tier is one of lead, authority or member. Assignable and Reviewer take yes or no.

## Authority notes

An empty roster means the operator is the only lead, and only the operator's own
comments and issues count. Add rows to trust more people: leads and authorities
can direct work; members can be assigned and requested for review but their
comments are treated as input, not instructions.
