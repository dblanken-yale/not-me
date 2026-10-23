#!/usr/bin/env python3
"""not-me UserPromptSubmit hook: the person's own prompt grants, or ends, one named merge.

A prompt that is exactly `/not-me:allow-merge owner/name#N` (optionally --squash, --merge or --rebase) writes
this session's merge grant; any other prompt in the session deletes it, so a grant lasts until the person's next
message. A malformed argument writes nothing. Reads only the hook input on stdin, NOT_ME_HOME for where the
not-me home is, and the operator token there to sign the grant (no token, no grant). Never blocks a prompt:
always exits 0.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def main(raw: "str | None" = None) -> int:
    try:
        if raw is None:
            raw = sys.stdin.buffer.read().decode("utf-8", "replace")
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from notme import merge_grant, paths

        event = json.loads(raw)
        if not isinstance(event, dict) or event.get("hook_event_name", "UserPromptSubmit") != "UserPromptSubmit":
            return 0
        sid = merge_grant.session_key(event.get("session_id"))
        if sid is None:  # no session: nothing to grant or to end
            return 0
        home = paths.home_dir()
        named, grant = merge_grant.parse_prompt(event.get("prompt"))
        merge_grant.clear(home, sid)  # first: a failed write below must not leave an older grant behind
        if grant is not None:
            try:
                merge_grant.write(home, sid, grant)
            except merge_grant.NoOperatorToken:
                print("not-me: no merge was granted: a grant is signed with your operator token, and there is "
                      "none. Create one in your own terminal with `notme doctor --init-token`, then try again.")
        elif named:
            print(f"not-me: no merge was granted. Use {merge_grant.COMMAND} owner/name#N "
                  "(optionally --squash, --merge or --rebase), nothing else on the line.")
    except Exception as exc:  # noqa: BLE001 - a broken hook must not block the person's prompt; no grant is written
        print(f"not-me merge-grant hook error: {exc.__class__.__name__}; no merge was granted", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
