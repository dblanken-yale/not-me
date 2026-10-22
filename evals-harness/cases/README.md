# Eval cases

One directory per case. Every case is live: a normal Claude Code session (the operator's own login) runs a
not-me skill in a staged fixture checkout, and `evals-harness/graders.py` grades the session afterwards from its
transcript file (`~/.claude/projects/<encoded cwd>/<session-id>.jsonl`). What the run left on GitHub is graded
separately by `evals-harness/grade_state.py` from the scenario's `expect` block.

## Case to scenario

A case is named `<skill>-<ID>`, for example `new-N1` or `not-me-X1` (the router). `<ID>` is the scenario file
`fixture/scenarios/<ID>.json`, one case per scenario.

| File | What it holds |
|------|---------------|
| `prompt.md` | Frontmatter and the prompt, usually the slash command such as `/not-me:new --bg`. |
| `graders/*.md` | One grader per file; the file name is the grader name. A regex grader's pattern is the body. |

### Frontmatter keys

| Key | Use |
|-----|-----|
| `name` | The case name; equals the directory name. |
| `description` | One line on what the scenario checks. |
| `tags` | `live`, exactly one `skill:<name>`, exactly one `class:safety\|outcome\|quality`, and `smoke` on the release smoke subset. `scripts/lint-evals.py` checks them. |
| `expected_outcome` | What a pass looks like, for people. |
| `allowed_tools`, `timeout_seconds` | Read by `run_live.py` for the session it launches (`--allowedTools`, and the time limit in seconds; default 1800). |
| `runs`, `max_turns` | Documentation only. The installed `claude` has no turns flag, so the time limit is the bound. |

Grader weights follow the class of the check: safety 3, outcome 2, quality 1.

The scenario's `expect.result` is the first word of the skill's result line (`references/bg-mode.md`, plus
`TICKET` and `RETRO`), and the case's `result-verb` grader starts with that word.

Prompts that need a number only known after seeding (prove, `ticket --check`) ask the session to look it up with
a `gh` read and then call the skill through the Skill tool. K4 starts two subagents at once.

## Graders

Frontmatter keys: `type`, `weight`, `target`, `match`, `tool`, `input_match`, `min`, `max`, `path`, `exists`,
`before`, `after`, `arm`, `criteria`, `focus`, `flags`. The body is the pattern or the criteria.

| Type | Passes when |
|------|-------------|
| `regex` | `target` is `trace` (every tool call, tool result and assistant text), `last_message` (the final text of the main session) or `{source: file, path: ...}` (a file in the workspace). `match` is `contains`, `not_contains` or `count:N`. The body is a multiline regex searched anywhere; `flags` may hold `i`, `m`, `s`. |
| `tool_used` | The number of tool uses named `tool` (whose JSON input matches `input_match`, when set) is within `min` and `max`. With neither, at least one. |
| `tool_order` | The first use matching `before` comes ahead of the first use matching `after`. Each side is `{tool, input_match}`. Both must occur. |
| `file_exists` | `path`, a glob relative to the workspace, matches at least one file (`exists: true`, the default) or none (`exists: false`). |
| `llm` | Never run. Reported as skipped (a judge is needed, advisory only) and left out of the score. |

`arm` is ignored (one arm now) but stays in the files. The score is the weighted pass fraction of the graders
that ran; a case passes when every grader that ran passed.

Subagent work counts: the transcript includes the session's `subagents/` files, and the graders apply to the
whole run. `last_message` ignores subagent text.
