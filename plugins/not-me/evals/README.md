# Eval cases

One directory per `claude plugin eval` case. Every case is live: it runs a not-me skill against the throwaway
fixture repos, and its graders check the session. What the run left on GitHub is graded outside the case by
`evals-harness/grade_state.py` from the scenario's `expect` block.

## Case to scenario

A case is named `<skill>-<ID>`, for example `new-N1` or `not-me-X1` (the router). `<ID>` is the scenario file
`fixture/scenarios/<ID>.json`, one case per scenario.

| File | What it holds |
|------|---------------|
| `prompt.md` | Frontmatter (`name`, `description`, `tags`, `runs`, `max_turns`, `timeout_seconds`, `allowed_tools`, `expected_outcome`) and the prompt, usually the slash command such as `/not-me:new --bg`. |
| `case.yaml` | Only `schema_version` and `context.scaffold_script: scaffold.sh`. prompt.md frontmatter has no scaffold key, so the case is "mixed": Claude Code merges the two files. |
| `scaffold.sh` | A copy of `evals-harness/scaffold/case-scaffold.sh`. It calls `scaffold.py stage --case <dir name>`. |
| `graders/*.md` | One grader per file; the file name is the grader name. A regex grader's pattern is the body. |

Tags: `live`, exactly one `skill:<name>`, exactly one `class:safety|outcome|quality` for the case's main purpose,
and `smoke` on the release smoke subset (N1, N6, U1, U4, R1, R2, K1, M1). Grader weights follow the class of the
check: safety 3, outcome 2, quality 1.

The scenario's `expect.result` is the first word of the skill's result line (`references/bg-mode.md`, plus
`TICKET` and `RETRO`), and the case's `result-verb` grader starts with that word.

Prompts that need a number only known after seeding (prove, `ticket --check`) ask the session to look it up with
a `gh` read and then call the skill through the Skill tool. K4 starts two subagents at once.

## Scaffold contract

Claude Code 2.1.x runs `scaffold_script` with bash, the run's working directory as the current directory, and an
environment of only `PATH`, `HOME` (the sandbox home), `TMPDIR` and `TERM`. It resolves the path inside the case
directory and refuses `..` or an absolute path, so each case carries its own `scaffold.sh`.

The session itself gets the operator's environment filtered to an allow list: standard variables, proxies,
`ANTHROPIC_*`, most `CLAUDE_CODE_*`, cloud SDK prefixes and `EVAL_*`. A case's `env` may set only `EVAL_*` keys.
`GH_TOKEN` and the `NOT_ME_*` variables the harness sets do not reach the scaffold or the session. The skills need
none of the `NOT_ME_EVAL_*` values; what they do need (gh auth, git identity, config) the scaffold writes into the
workspace and the sandbox home. `CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH` passes through, so export it in your shell
for the `--bg` cases.

So staging has two halves, both in `evals-harness/scaffold/scaffold.py`:

1. `scaffold.py prepare`, operator side, after seeding and before `claude plugin eval`. It reads
   `NOT_ME_EVAL_MANIFEST`, `NOT_ME_EVAL_BACKEND`, `NOT_ME_FIXTURE_OWNER` and `GH_TOKEN` (else `gh auth token`),
   clones the fixture repo, and the companion when the scenario has one, at the manifest's `main` SHAs into
   `evals/.state/<RID>/<ID>.stage/`, keeps a copy of the token there (mode 600), and writes
   `evals/.state/current-stage.json`.
2. `scaffold.py stage --case <name>`, run by `scaffold.sh` inside the eval. It refuses a stage prepared for
   another scenario, then:
   - copies the clone into the working directory; a companion goes to `.not-me-companion/<name>/`;
   - adds `/.claude/not-me/`, `/.not-me-companion/` and `/.not-me-eval/` to `.git/info/exclude`, so the checkout
     stays clean;
   - sets the git identity to the operator's login and noreply address;
   - writes the config layers, pause file, allow list and extra files from the scenario's `config` block;
   - writes `~/.config/gh/hosts.yml` for the sandbox home and runs `gh auth setup-git`;
   - records what it wrote, and the `NOT_ME_*` values for the record, in `~/.not-me-eval.json`.
3. `scaffold.py clean`, operator side, after grading. It removes the stage and the token copy.

`scaffold.py check` validates every scenario's `config` block.

### The scenario `config` block

```json
"config": {
  "none": true,
  "global": {"project.md": "---\nschema: 1\nenabled: false\n---\n"},
  "repo": {"project.md": "...", "verify.md": "..."},
  "pause": "global",
  "allow_companions": true,
  "files": {".not-me-eval/answers.json": "..."},
  "git_files": {"not-me/logs/new/<run id>.md": "..."},
  "commit_repo_layer": true
}
```

- With no block, or no `none`, the global `project.md` gets `mode: live` (github backend) or `mode: dry-run`
  (ledger backend) unless the scenario's own text sets `mode`.
- `none`: write no layer at all. That is a fresh install, which runs in dry-run.
- `global` and `repo`: whole doc texts for `~/.claude/not-me/` and `.claude/not-me/`. Placeholders `{repo}`,
  `{companion}`, `{companion_name}`, `{companion_path}`, `{operator}` and `{owner}` are filled in.
- `pause`: write `PAUSED` in the global dir or the project's `<git-common-dir>/not-me/`.
- `allow_companions`: confirm the companion for the fixture repo in the global allow list.
- `files` and `git_files`: extra files under the workspace or the git directory (an answers file, a finished run
  log for retro).
- `commit_repo_layer`: commit `.claude/not-me/` locally so `git diff` sees later edits (retro).

## Run one case

You need what `evals-harness/README.md` lists (fixture repos, `gh` logged in, `claude` on the path).
`run_live.py` does not call `prepare` and `clean` yet, so for now run the steps by hand:

    export NOT_ME_FIXTURE_OWNER=OWNER NOT_ME_FIXTURE_REPO=OWNER/not-me-fixture
    export NOT_ME_FIXTURE_COMPANION=OWNER/not-me-fixture-companion
    python3 fixture/seed_fixture.py --reset --run-id RID
    python3 fixture/seed_fixture.py --scenario N1 --run-id RID
    NOT_ME_EVAL_MANIFEST=evals/.state/RID/N1.json NOT_ME_EVAL_BACKEND=github \
        python3 evals-harness/scaffold/scaffold.py prepare
    claude plugin eval plugins/not-me --case new-N1 --runs 1 --scaffold --trust-plugin --ablation none \
        --allow-tools Bash,Write,Edit --max-cost-usd 6 --no-publish
    python3 evals-harness/grade_state.py --manifest evals/.state/RID/N1.json --scenario N1
    python3 evals-harness/scaffold/scaffold.py clean
    python3 fixture/seed_fixture.py --teardown RID

Add `--keep-temp` to keep the run's workspace and `trace.jsonl`. Static checks with no cost:
`python3 scripts/lint-evals.py` and `python3 -m unittest tests.test_eval_cases` from `plugins/not-me`.

## Limits of the in-case graders

- A `--bg` run does its work in a subagent. The graders see the parent session's tool calls; whether the trace
  includes the subagent's calls has not been checked. Safety on GitHub is graded by `grade_state.py` either way.
- `file_exists` matches only files the run created (the harness compares the workspace before and after), so
  edits to existing files are checked with a regex grader on the file.
- The dry-run ledger stays in the run's workspace (`.git/not-me/logs/intended-writes.jsonl`). The ledger backend
  of `grade_state.py` needs it copied out (`--keep-temp`).
- T1 creates an issue; no `expect` key reads issues the run created, so the case checks it from the trace.

## Cost

A live case costs about one skill run, a few dollars. 43 cases at one run each is roughly $100 to $250; the
smoke subset is about $15 to $50. Pass `--max-cost-usd` and check `cost_usd` in the results after the first pass.
