---
name: mode
description: "Show whether not-me is in dry-run or live mode and where that is set, switch to dry-run, or walk a person through going live. Going live is a human action: this skill never runs it, it gives the exact steps for the person's own terminal. Use when the operator runs /not-me:mode, asks what mode not-me is in, wants to stop real writes, or wants to go live. NOT for editing other settings (use /not-me:config to see them), NOT for allowing a repo (use /not-me:allow-repo), NOT for checking the install (use /not-me:doctor)."
argument-hint: "[dry-run [--global] | live]"
disable-model-invocation: true
---

# /not-me:mode

Shows the current mode, switches to dry-run, or hands a person the steps to go live.
Going live needs a person at a terminal, so this skill never runs `notme mode live`,
never reads the operator token, and never sets `NOT_ME_OPERATOR_CONFIRM`.

`${CLAUDE_PLUGIN_ROOT}` below is substituted at load time with this plugin's
directory. Never put it in a command you hand the user: their own terminal does not
have it. Use the absolute `notme` path that `notme mode show` reports.

Arguments: `$ARGUMENTS`.

## Steps

1. No argument: run `notme mode show` and keep the JSON. Tell the user:
   - the current mode, and where it is set (layer, file and line);
   - a repo-layer dry-run pin (file and line), if one is reported, because it keeps
     the project in dry-run even when the global layer says live;
   - what the mode means. `dry-run`: reads are real, every write goes only to the
     local ledger and nothing reaches GitHub. `live`: real writes, still through the
     gated path and the guard;
   - an unconfirmed live, when `layers.global.mode` is `live` but `live_confirmed` is
     false: the file says live, yet not-me runs in dry-run. Live counts only after a
     person runs the live step below, which signs it with the operator token; a
     hand-edited `mode: live` or a new token leaves it unconfirmed. Offer the live
     steps (step 3).
2. `dry-run`: run `notme mode dry-run` and report the result. Add `--global` only when
   the user asked for it globally (that also removes the live signature). Without it,
   it sets the repo layer. This is always allowed.
3. `live`: do not run it. Run `notme mode show` first, then give these steps in plain
   words, for someone who may not use the command line much:
   1. Operator token. Say whether the token file exists (the `mode show` boolean). If
      it does not, say in one sentence that it is a private secret that proves a
      person, not a program, is confirming, and give the one command to create it:
      `"<launcher>" doctor --init-token`, with `launcher` from `mode show`. It shows the token once; they
      should save it in a password manager.
   2. Go live. Give one command to copy into their own terminal app, not the terminal
      panel inside Claude Code: the `live_cmd` string from `mode show`, exactly as
      reported. `shell` `git-bash` means Windows: say to paste it into Git Bash, not
      PowerShell or cmd. Otherwise any terminal app works (for example macOS Terminal). Say it asks
      for the token and hides what they type.
   3. Confirm. Tell them to run `/not-me:mode` again; it should show `live` with
      source `global`. If they later make a new operator token, they repeat step 2.
4. Anything else: print the argument hint and stop.

## Rules

1. Never run `notme mode live`, even when asked. A refusal (kind `human_only`) is the
   design; do not retry it or look for a way around it.
2. Never read, print or ask for the operator token.
3. Never edit a config file to set the mode, and never touch `mode-live.sig` or
   `mode-live.epoch`. The commands above do that.

## Checklist

- [ ] Ran only `notme mode show` and, for dry-run, `notme mode dry-run`.
- [ ] For live: gave the token step (when missing), the terminal command with the
      absolute path, and the re-check step; ran nothing that goes live.
- [ ] No `${CLAUDE_PLUGIN_ROOT}` in any command printed for the user.
