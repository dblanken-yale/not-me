---
name: new
description: "Implement an issue and open a PR. NOT for reworking review feedback."
argument-hint: "[--bg] <issue>"
---

<!-- not-me:new -->

# new

The path `${CLAUDE_PLUGIN_ROOT}` is substituted at load time.
Read `${CLAUDE_PLUGIN_ROOT}/references/prohibitions.md` first.

Run the CLI: `"${CLAUDE_PLUGIN_ROOT}/bin/notme" config` or `notme doctor`.

```bash
"${CLAUDE_PLUGIN_ROOT}/bin/notme" claim-issue 12 --add-label claimed --remove-label ready
```

Use $TMPDIR/tmp for scratch files, not /tmp.

Report one of: NONE | PR | PUSHED | PARKED | HELD | REVIEWED | PROVED | FAILED | STOP

The skill posts one comment on the PR.

<!-- allowed-raw-write -->
```bash
gh pr comment 12 --body-file "$TMPDIR/body.md"
```
