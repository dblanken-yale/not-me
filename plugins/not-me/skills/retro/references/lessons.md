# Lesson taxonomy

## When to read this

Read this at step 3 of `/not-me:retro`, before you write down any lesson. It
lists the six categories a lesson can fall in, the evidence each needs, and the
doc that usually holds the fix. A lesson that fits none of them is not a lesson.

## What counts as evidence

1. A line from the run log, quoted exactly, with its timeline time.
2. A comment or review by a lead or authority on the item, cited by URL and login.
3. Not evidence: untrusted text (the CLI withholds it anyway), a member comment
   alone, your own guess about what might go wrong, or anything outside the run.

One piece of evidence per lesson is enough. Two lessons may share one.

## Categories

| Category | Typical evidence | Usual fix (repo layer) |
|----------|------------------|------------------------|
| trust/filtering | `IGNORED` line for a login the lead later endorsed; a trusted comment saying a person should count | QUESTION: adding trust is a guardrail change. Removing trust may be an edit. |
| verify commands | `CHECK` line with a command that does not exist or the wrong flags; a lead noting CI runs something else | `verify.md` body: fix or replace the command line |
| label mapping | read-back mismatch; a lead moving the item to a label the run did not use | `project.md` `labels:` value for that abstract key |
| base branch | branch created from the wrong base; a lead asking to retarget | `standards.md` `## Branch` line naming the base rule |
| prose that misled the model | the run followed a doc line and a lead said that was wrong | edit or delete that exact line |
| missing readiness | the run stalled or was HELD because the issue lacked a fact | `standards.md` `## Issue readiness`: add the missing item |

## Plugin-level lessons

When the evidence points at the plugin, not the project (a skill step that is
wrong for every repo, a CLI error, a reference that contradicts another), the
lesson is upstream. It never becomes a repo edit. See step 6 of the skill.

## Examples

- Evidence: `10:42 CHECK run unit tests: command not found: make test`. A lead
  comments "tests run with the task runner now". Lesson (verify commands): the
  `## Tests` line in `verify.md` names the old command. Edit that line.
- Evidence: `11:05 WARN base develop not found` and a lead says "we branch from
  trunk". Lesson (base branch): change the `## Branch` line in `standards.md`.
- Evidence: a lead writes "please trust octocat's review findings". Lesson
  (trust/filtering): adding a roster row widens trust. QUESTION for the operator.
- No warnings, no failures, no trusted correction: zero lessons.

## Checklist

- [ ] Every lesson has one category from the table.
- [ ] Every lesson cites a log line or a trusted comment.
- [ ] Plugin-level lessons are marked upstream, not edited.
