# Write-back assembly

## When to read this

Read this at step 8 of `/not-me:update`, after the push (or after deciding to
park). It says which files to write, which commands post them, and which labels
each outcome leaves. The shape rules themselves are in `write-back.md`,
`parking.md` and `companions.md`.

## Files

Make one work directory in the temp directory made as in `${CLAUDE_PLUGIN_ROOT}/references/autonomy-and-logging.md` (Text to GitHub, rule 2) and write one file per
post. Run `notme check-text --file <f>` on each; for the summary add `--summary`.
Fix every problem it lists and run it again. Never post a failing text.

## Thread replies

1. One reply per finding thread, using the reply lead from `findings.md` for its
   decision. Implemented: commit SHA, file and line, test name. Deferred: the
   question and who owes it. Refused: the rule and who can do it by hand.
   Disagreed: the file and line that show why.
2. Post with `notme reply-thread <ref> --to <reply_to> --body-file <f> --log <path>`.
   For a `comment` finding inside a thread, `--to` is its `id`.
3. Never resolve a thread, and never call `notme resolve-threads` from this skill.
4. Never reply on a thread that `feedback` did not return. Untrusted threads get
   no reply at all.

## Summary comment

`notme comment-upsert <ref> --marker update --body-file summary.md --log <path>`.
The CLI adds the kind and run markers and edits the earlier summary in place.

1. First line: `**TL;DR:**` with fixed and total counts and what is left.
2. `**Waiting on:**`, one login per line with what they owe; "nobody" when nothing
   is owed. Never "you".
3. Sections: Fixed, Deferred, Refused, Disagreed, each item with its thread or
   file and line. Plain PR comments that asked for a change are answered here.
4. Verification and gate results inside `<details>`, with a blank line after the
   `<summary>` line: each check by its command and result only, with
   no account of local trouble the run got past. That stays in the run log as a
   `WARN` (autonomy-and-logging.md, "Failures on our side"). The same holds for
   replies.
5. Add `IGNORED n comment(s) from: <logins>` when `ignored_count` is above zero.
   Never quote them.

## Testing steps

When the fixes add or change something a person should check, write one step per
line and run `notme testing-steps <ref> --steps-file steps.txt --log <path>`. It
appends to the PR's testing section; it never collapses it.

## Review requests

After a push: `notme request-review <ref> --reviewers <logins> --log <path>`, with
the authors of the addressed findings, leaving out the PR author. An empty list:
skip it. Parked runs request no review.

## Outcome table

| Outcome | Transition | Then add | Result line |
|---------|------------|----------|-------------|
| Every finding implemented or disagreed, pushed | `needs_work` to `needs_review` | nothing | `PUSHED <ref> needs_review - n/m fixed` |
| Some implemented, some deferred | `needs_work` to `needs_review` | `question` | `PUSHED <ref> needs_review - n/m fixed` |
| Some implemented, some refused | `needs_work` to `needs_review` | `needs_human` | `PUSHED <ref> needs_review - n/m fixed` |
| Nothing pushed, every finding disagreed | `needs_work` to `needs_review` | `needs_human` | `PARKED <ref> needs_review - waiting on @<login>` |
| Parked (see `findings.md`, "Park labels") | `needs_work` to `needs_review` | `needs_human`, plus `question` when a named person owes an answer | `PARKED <ref> needs_review - waiting on @<login>` |

1. The transition is `notme transition <N> --from needs_work --to needs_review
   --kind pr --reason-file <reason> --log <path>`; the reason file holds one line such as `Findings addressed in <sha>`. A PR picked as an answered question uses
   `--from question`.
2. Extra labels: `notme labels-sync <ref> --add <names> --reason-file <reason> --log <path>`, with the
   repo names from `labels`; the reason file holds one line on why (the park reason). Never add and remove in one call.
3. Every PARKED result adds `needs_human` (`parking.md`).
4. A read-back mismatch (exit 5) is a failure: log it and stop the write-back.

## Companion sets

Write back on every PR in the set: thread replies where the threads are, one
summary per PR, labels on each to match.

## Checklist

- [ ] Every body checked with `notme check-text` before posting.
- [ ] No account of local trouble in any reply or the summary.
- [ ] One reply per finding thread; no thread resolved; no reply on untrusted threads.
- [ ] One upserted summary with TL;DR first and the Waiting on list.
- [ ] Testing steps appended when they changed.
- [ ] Review re-requested from finding authors after a push.
- [ ] Labels left as the outcome table says.
