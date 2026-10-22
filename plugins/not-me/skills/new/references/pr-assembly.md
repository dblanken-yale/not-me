# PR assembly

## When to read this

Read this at step 9 of `/not-me:new`, after the push. It says how the title and
body files are built, how the PR is opened, and how the issue moves. The shape
rules themselves are in `pr-shape.md`, `companions.md` and `write-back.md`.

## Files

Make one directory with `mktemp -d "$TMPDIR/not-me.XXXXXX"` and write:

1. `title.txt`: one line from `standards.pr_title`, default `{issue}: {issue_title}`.
2. `body.md`: the template at `standards.pr_template` when it resolves, else the
   default sections from `pr-shape.md`:
   - `## Summary`. For a chained sub-issue the first line is
     `Builds on <owner>/<repo>#<n>`.
   - `## Testing steps`: a checklist a person can follow. Tick only what this run
     ran and saw pass. Never inside `<details>`.
   - `## Companion PRs`, only in a companion set.
   - `## References`: the issue as `<owner>/<repo>#N`, with the closing keyword
     from `standards.closing_keyword` (`auto`: `Closes <owner>/<repo>#N` when the
     tracker is this repo, else no keyword and the full ref). Never a bare `#N`.
   - For a UI diff, the `Visual check:` line from the run, and any accessibility
     finding not auto-fixed, with file and line.
   - Any gate finding left unfixed, with a one-line reason and a named login.
3. Do not add the attribution line yourself. `notme pr-open` adds the configured
   one.

Run `notme check-text --file body.md`. Fix every problem it lists and run it
again. Never open a PR on a failing check.

## Open

```
notme pr-open --head <branch> --base <base> --title-file title.txt \
  --body-file body.md --labels <needs_review name> --reviewers <logins> \
  --assignee <operator> --log <path>
```

1. `<needs_review name>` is the repo label for `needs_review` from `labels`.
2. Reviewers come from `reviewers`: the entry for this repo name, else `default`.
   An empty list: leave out `--reviewers`.
3. `<operator>` is `operator` from the config, or the login `notme me` prints.
4. Never pass `--draft`.
5. In a dry run the output has `number: null`. Use `-` for the PR ref in the result
   note and say `dry-run`.

## Companion sets

1. Open companion PRs first, in `repos` order, without `--labels` and
   `--reviewers`. Open the primary last, with both.
2. Write a JSON list of every PR in the set (`[{"repo": ..., "number": ...}]`) and
   run `notme companion-prs <repo>#<n> --companions-file <j> --log <path>` on each
   PR, so every body lists the others.
3. Then request review on the companions with
   `notme request-review <repo>#<n> --reviewers <logins> --log <path>`.

## Move the issue

`notme move-issue N --to needs_review --require-current in_progress --log <path>`.
This replaces `in_progress` with `needs_review` on a label queue and moves the
card on a board queue. A state mismatch (exit 1) means someone moved the issue
during the run: log a `WARN`, leave it, and say so in the result note.

## Checklist

- [ ] Title and body written to files and checked with `notme check-text`.
- [ ] Body has Summary, uncollapsed Testing steps, References with the right keyword.
- [ ] Opened ready for review, `needs_review` and reviewers on the completing PR only.
- [ ] Companion sections written on every PR in the set.
- [ ] Moved the issue from `in_progress` to `needs_review`.
