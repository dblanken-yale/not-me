# Interrupted runs

## When to read this

Read this at Step 1 of `/not-me:new` or `/not-me:update`, on every path: an
explicit target, a pick, and the idle path. When `notme status` lists a run of
this same skill in `interrupted_runs`, that run died before it finished its log,
so its item is stuck. This hands the item to a person. It never resumes the work.

## Which run counts

Do this before the pick and before `notme log start`. After the start,
`notme log last --skill <skill>` returns the new run's own log.

1. Take the entry in `interrupted_runs` (from `notme status`; entries are
   `{skill, run_id, ref, file}`) whose `skill` equals the running skill (`new` or
   `update`, the log folder name). Ignore every other entry. No such entry: nothing to do here.
2. Run `notme log last --skill <skill>`. Record its full `path`. Its `run_id` must
   equal the entry's `run_id`. If it does not, record no path and make no hand-off;
   once your log exists, log a `WARN` line
   (`notme log action <path> --kind WARN --message-file <f>`) that says so.
3. A log that says "committed but NOT PUSHED" is a finished log, not this case.
   `/not-me:new` resumes it as its own target.md says.
4. Qualify the entry's `ref` before any write, since a bare `N` or `#N` can resolve
   to different items: `<repo>#N` for `update` (a PR in the code repo),
   `<tracker>#N` for `new`. `repo` and `tracker` come from `notme preflight`.

## When to do it

1. Before the pick: pass the interrupted number to it, so the run cannot pick
   that item again: `notme pick-needs-work --exclude N` or `notme pick-issue --exclude N`.
2. After the new run's `notme log start`: do the hand-off below, finishing the old
   log by the recorded path, never by running `notme log last` again. This holds on
   the idle path too: right after the idle `notme log start`, before its
   `log finish`. It is the one exception to "Write nothing else" there.
3. The hand-off is an `ACTION` line in the new run's log
   (`notme log action <path> --kind ACTION --message-file <file>`), not its result.
4. A run that stops before its own log exists (`STOP <ref> claimed elsewhere`,
   `STOP - active run`) makes no hand-off and leaves the old log open. The next
   run picks it up again.

## Hand off

1. A `ref` of `-` (an idle run), or an explicit target equal to the interrupted ref (a person chose it): add no label and post no comment. Log the `ACTION`, go to the last rule, then carry on as normal.
2. Add `needs_human` for the qualified ref:
   `notme labels-sync <ref> --add <name> --reason-file <reason> --log <path>`.
   `<name>` is the repo label name that `labels` maps `needs_human` to.
3. Read the old log for its branch name. If it names none, say so.
4. Write a short comment in the run's temp directory: the branch, the old log's
   base file name (never a local path), and that the work there was never
   committed or pushed. Run `notme check-text --file <f>`, then
   `notme comment-upsert <ref> --marker <skill> --body-file <f> --log <path>`.
   The marker is the skill name, `new` or `update`.
5. Close the old log:
   `notme log finish <old path> --status "FAILED <ref> interrupted - handed to a person"`.

## After the hand-off

1. Do not resume, commit, push, or rebuild the abandoned work. A person decides.
2. Carry on with the normal flow and pick new work as usual.

## Checklist

- [ ] Checked on every path of Step 1 and counted only this skill's entry.
- [ ] Recorded the old log's `path` before `notme log start`, with a matching `run_id` (else a `WARN`, no hand-off).
- [ ] Passed the interrupted number to the pick with `--exclude`.
- [ ] Qualified the ref before any write.
- [ ] Once my log existed: labelled and commented unless idle or chosen, finished the old log by the recorded path, logged the `ACTION`.
- [ ] Made no attempt to resume the abandoned work.
