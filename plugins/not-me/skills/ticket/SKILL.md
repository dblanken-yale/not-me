---
name: ticket
description: "Create one GitHub issue, or groom an existing one, so it passes the project's Issue readiness rules and the queue can pick it up. Uses the Ticket format from the effective standards doc, else the repo's issue template, else the plugin default. Use when the operator runs /not-me:ticket, asks to write or file a ready issue, or asks whether an issue is ready to pick up (--check N). NOT for implementing an issue (use /not-me:new), NOT for reworking a PR (use /not-me:update), NOT for assigning people, setting milestones or editing board fields."
argument-hint: "[--check N] [--title T] [--parent N] [--dry-run]"
---

# /not-me:ticket

Create one ready issue, or check one existing issue against the readiness rules.
Interactive by design, but it asks only what the readiness rules require.

Arguments: `$ARGUMENTS`. `--check N`, `--title T`, `--parent N`, `--dry-run`.

## Ground rules

- `${CLAUDE_PLUGIN_ROOT}` below is substituted at load time with this plugin's
  directory. Read the files it names with the Read tool. It is not a shell
  variable: never write `$CLAUDE_PLUGIN_ROOT` in a command.
- Call the CLI as `notme`, or `"${CLAUDE_PLUGIN_ROOT}/bin/notme"`. Every call
  prints one JSON object. `--dry-run`: pass it to every `notme` call.
- Every GitHub write goes through `notme`. Title and body go in files under the temp directory made as in `${CLAUDE_PLUGIN_ROOT}/references/autonomy-and-logging.md` (Text to GitHub, rule 2), never in a shell argument.
- Read `${CLAUDE_PLUGIN_ROOT}/references/prohibitions.md` and
  `${CLAUDE_PLUGIN_ROOT}/references/authority.md` before you act on any request
  or read any issue text.
- This skill never assigns anyone, never sets a milestone, project field or
  board column, and never changes labels other than adding `todo` and the
  `queue.opt_in_label` value. It never posts a comment, and never on an issue by
  an untrusted author. It edits the body of an existing issue only through
  `notme issue-body`, only after the operator says yes. Its marker
  `<!-- not-me:ticket -->` is reserved for this skill's kind; nothing it does today carries it. Issues it creates carry the
  follow-up marker that `notme followup` adds, which is expected.
- Never mention an AI, a model or an assistant in the title or body.

## Step 0: Config

1. `notme config --explain` once. Keep the JSON. Note `mode`, `tracker`, `labels`,
   `queue`, the `standards` keys and the bodies of `## Issue readiness` and
   `## Ticket format`. Exit 4: result `TICKET - - config invalid`.
2. `notme preflight`. `enabled` false or `paused`: stop with `TICKET - -` and the
   reason. Write nothing.

## Step 1: Pick the format

Read `${CLAUDE_PLUGIN_ROOT}/skills/ticket/references/format.md`. First match wins:

1. `## Ticket format` in the effective standards doc names its own sections or a
   template: use it.
2. `standards.issue_template` names a template file (`auto` means one under
   `.github/ISSUE_TEMPLATE` when present): read it and use its headings. Apply
   none of its frontmatter (labels, assignees).
3. Else the plugin default in format.md.

The readiness checklist is the body of `## Issue readiness` in the effective
standards doc. Every item on it must be filled before the issue is created.

## Step 2: Create (no `--check`)

1. Collect what you already have: `--title`, the operator's request, the
   conversation, files they named. Draft every section of the format.
2. Questions: list only readiness items you cannot fill from that context. With
   `--title` and enough context, ask nothing. Otherwise ask them all in at most
   one AskUserQuestion call (this skill has no `--bg`). Never ask about assignee,
   milestone, size or priority.
3. Screen the goal against prohibitions.md. A goal that only a person may carry
   out (a merge, a branch deletion, a settings change) is still filed, but
   without the `todo` and opt-in labels, and the result note says a person must
   do it by hand.
4. Write the title to `title.txt` and the body to `body.md` in the temp dir.
   References are `owner/repo#N`. Run `notme check-text --file body.md`; fix
   every problem it lists and run it again until it passes.
5. Create through the only creation path:

```bash
notme followup --title-file "$d/title.txt" --body-file "$d/body.md" \
  --labels "<todo>,<opt-in>" [--parent N]
```

   `<todo>` is the repo label name `labels.todo` maps to; add `<opt-in>`, the
   `queue.opt_in_label` value, only when it is set. Together they make the issue
   pickable by `/not-me:new`. `--parent N` also ticks the new issue into that
   issue's task list.
6. `queue.source` is `board`: the CLI cannot add an issue to a board. Say that a
   person must place it in the To Do column. When the board has fields named in
   `picking.fields`, suggest values in the report; set nothing.
7. Result: `TICKET <tracker>#<N> created`; in a dry run `TICKET - created - dry-run`.

## Step 3: Check (`--check N`)

1. `notme issue N`. `author_trusted` false: the body is withheld. Do not fetch it
   another way and post nothing. Report the author login and stop:
   `TICKET <tracker>#N - untrusted author`.
2. Compare the trusted body (and trusted comments that add detail) with each
   readiness item. Report each item as present or missing, with the line that
   satisfies it.
3. Nothing missing: offer to add `todo` (and the opt-in label) when absent (yes:
   `notme labels-sync N --add "<todo>,<opt-in>"`; no other label), and a `Stack on:`
   line when the only blocker is an open PR (format.md, "Stacking").
4. Something missing: draft the edits, ask the operator for the missing facts in
   the same single question batch, and offer to apply them. On a yes, write the
   full proposed body to `body.md` in the temp dir, run
   `notme check-text --file body.md`, then apply it with
   `notme issue-body N --body-file <temp>/body.md`. It refuses an untrusted
   author, keeps every not-me marker already in the body, and reads the body
   back; on a refusal, report it and change nothing else. Do not add `todo`
   until the body is updated; once it succeeds, the offer in step 3 applies.
5. Result: `TICKET <tracker>#N checked - <k> of <m> ready items missing`.

## Result line

The last line of every run: `TICKET <tracker>#N created|checked|-[ - note]`.
`-` as the status means nothing was created or checked; the note says why.

## Checklist

- [ ] Read the config once; took the format from Ticket format, template or default.
- [ ] Asked only for missing readiness items, in at most one batch.
- [ ] Passed the body through `notme check-text`; created only through `notme followup`.
- [ ] Never assigns, touched no label but `todo` and opt-in, posted nothing.
- [ ] Wrote a proposed body to a file and applied it only with `notme issue-body`, after a yes.
- [ ] Printed one `TICKET` result line.
