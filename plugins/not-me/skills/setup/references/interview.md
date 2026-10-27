# Setup: the interview

Ask with AskUserQuestion, at most four questions per call, as few calls as the
answers need. Put the detected value first as the recommended option and quote the
key's comment from the shipped defaults as the explanation. Skip any question whose
doc is not in scope for this run.

## Repo layer questions

| # | Ask | Writes | Doc |
|---|-----|--------|-----|
| 1 | Is this repo where issues live, or does another repo hold them? | `tracker` | project |
| 2 | Pick work from labels or from a project board? Which board? | `queue.source`, `queue.board`, `queue.board_states`, `queue.pick_label`, `queue.opt_in_label` | project |
| 3 | Who is on the roster, with what tier? (lead, authority, member; assignable; reviewer) | `## Roster` table rows | project |
| 4 | Who is requested as reviewer by default, and per repo? | `reviewers` | project |
| 4a | Must every reviewer approve before a PR is ready to merge? If yes, each reviewer's role and pass label (both may stay empty) | `## Roles` table rows | role |
| 5 | Which sibling checkouts are companions, and in what push order? | `repos` | project |
| 6 | Per state: create, map to an existing label, or skip (see `labels.md`) | `labels` | project |
| 7 | Which branches beyond main, master and develop are protected? | `protected_branches` (add only) | project |
| 8 | Orchestrate across models, or one model for everything? | `models.strategy`, `models.single_model` | project |
| 9 | Are these the test, lint and build commands? | verify sections | verify |
| 10 | Which units need the most capable model? (paths or kinds) | `## Delegation` | standards |
| 11 | Is this the ticket format and the readiness bar? | `## Ticket format`, `## Issue readiness` | standards |
| 12 | Extra checks before review? | `## Gate additions` | verify |
| 13 | Where is the URL to test a PR found, in what order? | `target` | prove |
| 14 | A command to run when a run ends? | `notify.command` | project |
| 14a | Which layer gets the picking rules? Keep the current rules, or rank by a board field or label found? Include unassigned issues? Target epics? (see `picking.md`) | `picking.fields`, `picking.target_epics`, `queue.include_unassigned`, `## Picking work` | project |
| 15 | Add to the global docs, or replace them? (only when a global doc exists) | `extends` | each doc |
| 16 | Turn on live mode at the end if doctor passes? | none: the user runs `/not-me:mode live` | none (signed by the user) |

## Global layer questions (`--global`)

Ask only operator-level keys: `mode`, `attribution`, `limits`, `models`, `router`,
`notify`, `logs.root`. Setup writes `mode` only as `dry-run`; live is turned on by
the user with `/not-me:mode live`. Never ask `allow_repos`; the allow list is
changed only with `/not-me:allow-repo`.

The picking step (`picking.md`) may also write the global layer: `picking.fields`,
`picking.target_epics`, `## Picking work`, and `queue.include_unassigned`, which is
the only queue key the global layer may get. The repo layer may only set it to
`false`; refuse a `true` there in one line (it belongs in the global layer; the CLI
drops it), and never write it.

Refuse repo-only keys in the global layer, and say why: `tracker`, `repos`,
every other `queue` key, `labels`, `reviewers`, the roster table, the roles table, verify commands, and the
`## Ticket format` section. They describe one project and belong in its repo layer.

## Answers file (`--answers FILE`)

A JSON object. Every field is optional; a missing field keeps the detected value,
or the default when nothing was detected. With an answers file, setup asks
nothing, treats every confirmation as given except `create_labels` and
`mode_live` (both default false), and still runs every check.

```json
{
  "layer": "repo",
  "docs": ["project", "standards", "verify", "review", "prove", "role"],
  "body": "extend",
  "tracker": "owner/name",
  "queue": {"source": "board", "board": {"owner": "owner", "number": 3}},
  "roster": [{"login": "someone", "name": "Some One", "tier": "lead", "assignable": true, "reviewer": true}],
  "reviewers": {"default": ["someone"]},
  "roles": [{"login": "someone", "role": "code", "pass_label": "pass code review"}],
  "repos": [{"name": "owner/app", "role": "primary"}, {"name": "owner/app-ui", "role": "companion"}],
  "companion_docs": false,
  "labels": {"todo": "ready", "in_progress": "create", "epic": "skip"},
  "create_labels": false,
  "protected_branches": ["release"],
  "models": {"strategy": "orchestrate"},
  "verify": {"tests": "make test", "lint": "make lint", "build": null, "gate_additions": []},
  "delegation": ["src/auth/", "database migrations"],
  "ticket_format": "keep",
  "prove": {"strategy": ["deployments", "local"]},
  "notify": {"command": null},
  "picking": {"layer": "global", "fields": ["Priority"], "rules": "Prefer the highest value of the board field `Priority`. Issues without one come after. Impact breaks ties.", "target_epics": [], "include_unassigned": false},
  "mode_live": false
}
```

Field notes: the roles rows go in the role.md table; leave it out (or empty) to keep
one approval enough. `body` is `extend` (write `extends: global`) or `replace`. A `labels`
value is `create`, `skip`, or the existing label name to map to. `ticket_format`
is `keep` (the default text), `detected`, or the literal markdown to write. The
`picking` object is optional: `layer` is `global` or `repo`; `rules` is the plain-words
section body or `keep` (keep the current rules: no section and no `fields` written;
`target_epics` and `include_unassigned` are still written when given);
`include_unassigned` of `true` with layer `repo` is an error. A
global run may also carry `attribution`, `limits`, `router` and `logs` objects
shaped like the keys in the shipped `project.md`; any repo-only field in a global
run is an error that stops setup before writing.
