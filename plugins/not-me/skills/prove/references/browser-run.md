# Browser run

## When to read this

Read this at step 3 of `/not-me:prove`, once the target URL and the sorted steps
are known. It says how the browser is driven, where screenshots go, how a
screenshot becomes a result, and what to do when the browser or the target fails.

## The browser tool

1. Load the `playwright-cli` skill with the Skill tool and follow it for every
   browser action. Not available: log a `WARN` naming the missing tool, write
   nothing on the PR, end with `FAILED <ref> no browser tool` and stop.
2. Start each browser command with the tool's own name. No `cd` prefix, wrapper,
   or shell function around it, and never a disabled sandbox.
3. Use one named browser session for the run, so a crash in another session does
   not touch it. Close it at the end.
4. Open only the target URL from `notme prove-target` and links on that same
   host. A step that leads to another host is `not run (other host)`.

## Screenshots

1. The directory sits beside the run log and has the log's name without `.md`.
   Create it before the first step.
2. One screenshot per step, named by step number: `step-01.png`, `step-02.png`.
   A failing step may add one more for the error state: `step-02-error.png`.
3. Screenshots stay in that directory. They are not copied into the repository and
   not attached to the PR.

## Logging in

1. Follow `## Logging in to the target` from the prove body in the config output.
   The shipped default only says to describe the login; when it is still the
   default and the target asks for a login, every step behind it is
   `not run (no login configured)`.
2. Credentials come only from where that section points (an environment variable
   or a password manager command it names). Never type a credential into a log, a
   file in the repository, or the comment.
3. A login that fails is one `WARN` line and `not run (login failed)` for every
   step behind it. It is not a step failure.

## Running a step

For each step in order:

1. Do what the step says, the way `## How to run steps` says.
2. Take the screenshot.
3. Read the screenshot. Open the image and compare it with what the step says
   should be seen and with `## What a pass looks like`. Reading is the gate;
   capturing is not.
4. Check the console for errors when the browser tool can show them. An error that
   appears only after this step's action fails the step.
5. Log one line with `notme log action --kind CHECK`, in one of the two forms:

```
Visual check: <path of the screenshot you read>
Visual check: not run (<named reason>)
```

6. Record the result: `pass`, `fail` with what was seen against what was expected,
   or `not run` with its reason. Keep it to one line.

Writes the step asks for on the target (create a page, submit a form) are allowed:
the target is a preview, not production. Never anything a step does not ask for.

## When things break

| Situation | What happens | Result |
|-----------|--------------|--------|
| The target does not load before step 1 (DNS, timeout, server error) | `CHECK` with the status, no comment | `NONE <ref> no deploy target` |
| The target stops answering after some steps ran | Remaining steps `not run (target down)` | post the proof as usual |
| The browser crashes | Restart the session once and redo the step | |
| It crashes again | `WARN` with the error, no comment | `FAILED <ref> browser` |
| A guard block | QUESTION naming the operator, no retry, no comment, labels unchanged; run stops | `FAILED <ref> blocked - <target>` |

## Checklist

- [ ] Used the `playwright-cli` skill, one session, the target host only.
- [ ] Saved one screenshot per step beside the run log.
- [ ] Logged in only as the prove body says and wrote no credential anywhere.
- [ ] Read every screenshot and logged one `Visual check:` line per step.
- [ ] Gave each step `pass`, `fail` or `not run` with a one-line reason.
