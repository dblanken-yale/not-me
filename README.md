# not-me

not-me is a project-agnostic autonomous dev loop for Claude Code, shipped as a plugin and marketplace. It picks up ready GitHub issues, implements them, opens PRs, reworks PRs after review, reviews PRs, and proves PRs on a deploy target.

It is configured per repo by markdown docs in `.claude/not-me/` (layered: plugin defaults, then `~/.claude/not-me/`, then the repo). A stdlib-only Python CLI, `notme`, does all GitHub mechanics. A PreToolUse guard hook hard-blocks only what breaks the process or cannot be undone (commits, merges and pushes on protected branches, force-pushes, destructive git) and logs the rest, such as writes to other repos, for the skills to guide. Works in the Claude Code CLI and desktop.

## Quickstart

Install:

```
/plugin marketplace add dblanken-yale/not-me
/plugin install not-me@not-me
```

Then, in the repo you want to automate:

```
/not-me:doctor
/not-me:setup
/not-me --dry-run
```

`--dry-run` reads from GitHub for real and records every write in a ledger instead of posting it. Read the ledger and the run log, then run `/not-me:mode live` and follow its steps in your own terminal (a repo file can never set live, and a hand-edited `mode: live` stays dry-run). Then schedule it:

```
/loop 2h /not-me --bg
```

Other useful runs: `/not-me:new 42` to implement one issue, `/not-me:update owner/repo#57` to rework one PR, `/not-me:review --bg` to review the queue, `/not-me:status` to see what is waiting on a person.

Update with:

```
/plugin update not-me@not-me
```

## How a run works

Solid boxes always run. Dashed boxes are optional: they run only when you call them, configure them, or a condition holds. A `⚙` names the setting or config doc that changes the step (`project.md` keys unless another doc is named; see [Configuration](docs/config.md)). Rounded orange boxes are a person's job and nothing in not-me does them. Labels are shown by their default names. For the label state machine see [Workflow](docs/workflow.md).

```mermaid
flowchart TD
    trigger(["/not-me, a single leg, or /loop 2h /not-me --bg"])
    gate["Gate: enabled, not paused, no active run, guard self-test passes"]
    stopped["STOP, nothing written"]
    router["Router runs the legs in order, stops at the first that does work
    ⚙ router (default: update, then new)"]

    trigger --> gate
    gate -- fails --> stopped
    gate -- passes --> router

    subgraph update ["update leg: rework a PR"]
        u1["Pick a 'needs work' PR assigned to you, claim it"]
        u2["Read trusted findings only, decide each:
        refuse, defer, implement, or disagree"]
        u3["Implement through a delegated subagent
        ⚙ models, standards.md Delegation"]
        u4["Verify and gate: targeted tests during the work,
        the full set once before the branch goes up
        ⚙ verify.md, Targeted tests"]
        u5["Merge base in (the predecessor's branch on a stacked PR),
        send the branch up, reply on every thread, swap to 'needs review'"]
        u3b["Baseline, skipped when the base CI is green
        ⚙ verify.md baseline: auto"]
        u5b["Wait for CI
        ⚙ limits.ci_wait_minutes, 0 skips it"]
        u1 --> u2 --> u3b --> u3 --> u4 --> u5 --> u5b
    end

    subgraph new ["new leg: implement an issue"]
        n1["Pick a ready issue and claim it; an issue that waits on one open PR
        is ready when its trusted 'Stack on:' line names it
        ⚙ queue, queue.stacking, picking, Picking work rules"]
        n2["Branch from the base: the default branch, an epic's branch,
        or the open PR's branch for a stacked issue
        ⚙ standards.md branch, protected_branches, queue.stacking"]
        n3["Implement through a delegated subagent, failing test first
        ⚙ models, standards.md Delegation"]
        n3b["Baseline, skipped when the base CI is green
        ⚙ verify.md baseline: auto"]
        n4["Verify and gate: targeted tests during the work, simplify,
        code review, security review, then the full tests, lint
        and build once (CI parity)
        ⚙ verify.md, Targeted tests, limits.fix_loop"]
        n4b["Gate additions and cross-model check
        ⚙ verify.md Gate additions, models.verifier"]
        n5["Commit and push through the CLI
        ⚙ standards.md commit, attribution"]
        n6["Open PR, move issue to 'needs review'
        ⚙ standards.md pr_title, pr_template, reviewers"]
        n7["Wait for CI on the PR
        ⚙ limits.ci_wait_minutes, 0 skips it"]
        n1 --> n2 --> n3b --> n3 --> n4 --> n4b --> n5 --> n6 --> n7
    end

    router -- "first" --> update
    router -- "no 'needs work' PR" --> new

    review["review leg: review PRs you are asked to review,
    approve, request changes, or ask a person
    ⚙ review.md, role.md, add review to router"]
    prove["prove: run testing steps on the preview,
    post a proof comment, fail sends PR to 'needs work'
    ⚙ prove.md"]
    retro["retro, after a router leg that did work:
    up to three edits to .claude/not-me/ docs, never commits"]
    notify["notify.command"]
    writes["Every GitHub write goes through the CLI write gate and the guard hook.
    Dry-run records it in the ledger; live posts it
    ⚙ mode (default dry-run), guard, allow_repos"]
    live(["A person turns on live mode
    in their own terminal"])
    merge(["A person approves, if review did not, and merges"])

    router -.-> review
    n6 -. "preview URL found" .-> prove
    u5b --> retro
    n7 --> retro
    review --> retro
    retro -.-> notify
    update --> writes
    new --> writes
    live -.-> writes
    review -- "'ready to merge', only with review.auto_approve" --> merge

    classDef optional stroke-dasharray: 5 5
    classDef person fill:#fde7c8,stroke:#c77700,color:#000
    class review,prove,notify,n4b,n3b,u3b,n7,u5b optional
    class live,merge person
```

## Commands

| Command | What it does |
| --- | --- |
| `/not-me [--dry-run] [--bg]` | Router: runs the legs in `router` order (default `update`, then `new`) and stops at the first that does work |
| `/not-me:new [issue#] [--base B] [--dry-run] [--bg]` | Pick up a ready issue, implement it through a delegated implementer, gate it, open a PR |
| `/not-me:update [PR ref] [--dry-run] [--bg]` | Decide each review finding on a `needs work` PR, fix, push, reply, swap to `needs review` |
| `/not-me:review [owner/repo#N ...] [--dry-run] [--bg]` | Review `needs review` PRs where you are a reviewer: approve, request changes, or ask a person. Never merges |
| `/not-me:prove <pr-ref> [--dry-run] [--bg]` | Run a PR's testing steps in a browser on its preview and post a proof comment |
| `/not-me:ticket [--check N] [--title T] [--parent N] [--dry-run]` | Create a ready issue, or check an existing one against the readiness rules |
| `/not-me:suggest [--dry-run]` | List ready issues nobody is assigned to, ranked by the picking rules, and assign the ones you pick to you. Run by a person, never by a loop |
| `/not-me:retro [log path] [--bg]` | Turn evidence in a finished run log into at most three small config edits |
| `/not-me:status` | Show state, the active run, items waiting on a person, stale claims, last results |
| `/not-me:setup [--global] [doc] [--check] [--upgrade] [--answers FILE]` | Guided setup of `.claude/not-me/`, or the global layer with `--global` |
| `/not-me:config [--layer default\|global\|repo]` | Explain the effective config and where each value came from |
| `/not-me:doctor` | Check tools, GitHub auth, config, the guard self-test and subagent depth |
| `/not-me:allow-repo <owner/name> [--everywhere]` | Show the allow list and print the command a person runs to change it (also `--board`, `--list`, `--remove`) |
| `/not-me:mode [dry-run [--global] \| live]` | Show dry-run or live and where it is set, switch to dry-run, or get the steps to go live in your own terminal |

## Requirements

git >= 2.30, gh >= 2.40, python3 >= 3.9.

## Repository layout

```
.claude-plugin/marketplace.json   marketplace listing
plugins/not-me/                   the plugin (skills, hooks, notme CLI, defaults, tests)
scripts/                          validation and test runners used by CI
docs/                             documentation
.github/                          CI, release, and contribution files
```

## Documentation

- [Requirements](docs/requirements.md)
- [Install](docs/install.md)
- [Configuration](docs/config.md)
- [Guards](docs/guards.md)
- [Workflow](docs/workflow.md)
- [CLI](docs/cli.md)
- [Testing](docs/testing.md)
- [Releases](docs/release.md)

## License

MIT. See [LICENSE](LICENSE).
