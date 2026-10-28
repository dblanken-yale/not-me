# Ticket format and readiness

## When to read this

Read this at step 1 of `/not-me:ticket`, before drafting a body or checking one.
It holds the plugin default format, used only when the effective standards doc
and the repo's issue template give none, and the default readiness checklist.
The project's own `## Issue readiness` and `## Ticket format` always win.

## Default format

```markdown
## Goal

One or two sentences: what changes and why it matters.

## Acceptance criteria

- [ ] An observable result a reviewer can check.
- [ ] Another one. Each item is testable on its own.

## Reproduction or context

For a bug: numbered steps, what happens, what should happen, where (branch,
page, command). For other work: the background a newcomer needs, with links.

## Out of scope

What this issue does not cover, so the PR stays small.
```

Title: a short imperative phrase naming the change, for example
`Show the empty state on the search page`. No issue number, no labels in the title.

## Readiness checklist

An item counts as present only when it is specific enough to act on.

| Item | Present when | Missing when |
|------|--------------|--------------|
| Goal | States what changes and the reason | Only a topic, such as "search page" |
| Acceptance criteria | A checklist of at least one testable result | Prose with no checkable outcome |
| Reproduction or context | Steps with expected and actual results, or the background and links | "It is broken" with no steps |
| Out of scope | At least one line, even "Nothing beyond the criteria above" | Section absent |

A project's `## Issue readiness` may add items (a size, a design link, a base
branch). Each extra item is checked the same way: present, specific, actionable.

## Using a repo issue template

1. Keep the template's headings and their order.
2. Fill each heading from what the operator gave. Drop the template's HTML
   comments and placeholder text.
3. If a readiness item has no matching heading, add it as its own `##` section
   at the end.

## What to ask, and what not to

Ask only for readiness items you cannot fill. Good questions name the gap:
"What should the page show when there are no results?" Never ask for assignee,
milestone, size, priority or labels. Size and priority are suggestions in the
report when the board has those fields, never questions and never writes.

## Stacking

`notme issue N` returns `blocked_by` and `stack_on`. GitHub dependencies link
issues, not PRs, so the case to offer is: the only open blocker is an issue whose
single open PR is X (open, not a draft; check it with `notme bundle <X>`) and
`stack_on` is null. Offer to add one line, `Stack on: <owner/repo#X>` (or `Based on:`,
`On top of:`; the blocker issue's own ref works too), to the body: the issue may then
start now, branched from X's branch. Say it still waits on any other open blocker, a
draft or closed PR, or `queue.stacking: off`. Apply it only after a yes, through the
body edit in Step 3 of the skill. A `stack_on` already there: report `ready` and `reason`.

## Checklist

- [ ] Used the project's format first, then the template, then this default.
- [ ] Every readiness item is present and specific.
- [ ] References are `owner/repo#N`; no local paths or secrets in the body.
