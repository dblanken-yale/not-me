# The altitude ladder

## When to read this

Read this at step 4 of `/not-me:retro`, before each edit. It says where a fix
belongs, from the narrowest change to the broadest, and which changes are never
edits at all. Take the lowest rung that fixes the lesson.

## The ladder

1. **Delete a line** that is wrong or misled the run.
2. **Edit an existing line** so it says the right thing.
3. **Add a line** to an existing section of an existing repo-layer doc.
4. **Change a frontmatter value** in a repo-layer doc, when the value only
   tightens or is neutral (a label name, a branch template, a verify command).
5. **Add a section**, or create a repo-layer doc with `extends: default` that
   holds only the new section.
6. **Not an edit**: a QUESTION for the operator, or an upstream lesson.

Count each changed hunk as one edit. Three is the cap for a run; when there are
more lessons, keep the three with the strongest evidence and record the rest in
the audit log as proposals.

## Never an edit

Any change that would relax one of these goes to rung 6 as a QUESTION:

- `guard` rules or `confirm_guard_off`
- `protected_branches` (removing a name) or `allow_repos` (adding a repo)
- the prohibitions, or any line that narrows a refusal
- the gate: removing or skipping a `## Gate additions` step, a verify step or a
  review rule, or raising fix-loop limits. Correcting a wrong verify command to
  the one CI really runs is an edit, not a relaxation.
- trust: roster rows, tiers, endorsement rules, bot handling
- `mode` (toward `live`) and `enabled` (toward `true`)

The repo layer may only tighten these anyway; the CLI drops a loosening value.
An edit that tightens one of them (adding a protected branch, a gate step) is
allowed when the evidence asks for it.

## Writing the edit

1. Match the doc's voice: short, imperative, no names of people beyond logins
   already in the doc.
2. No absolute paths, no secrets, no mention of an AI or a model.
3. Keep the line where the run will read it: the section the run actually
   consulted, not a new catch-all section.

## Checklist

- [ ] Picked the lowest rung that fixes the lesson.
- [ ] At most 3 edits; extra lessons recorded as proposals.
- [ ] No guardrail relaxed; each such change written as a QUESTION.
