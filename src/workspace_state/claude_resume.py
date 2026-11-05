"""Launch one exact saved Claude conversation with startup ownership."""
from __future__ import annotations

import os
import sys

from .capture import UUID_RE
from .codex_resume import decode_startup_context
from . import conversation_guard, operations


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if len(arguments) not in {1, 2} or not UUID_RE.fullmatch(arguments[0]):
        print("wsctl: expected Claude UUID and optional startup context", file=sys.stderr)
        return 2
    claimed = False
    try:
        context = decode_startup_context(arguments[1]) if len(arguments) == 2 else operations.current()
        inherited = operations.current()
        if context is not None:
            if context.mode != "startup" or (inherited is not None and inherited != context):
                raise ValueError("Claude launcher belongs to another operation")
            with operations.publisher(context):
                context.check()
        # One client per conversation. Exec keeps this process ID, so the
        # claim lasts for exactly as long as the Claude client runs.
        if not conversation_guard.claim("claude", arguments[0]):
            print(conversation_guard.refuse_message("claude", arguments[0], None), flush=True)
            return 0
        claimed = True
        location = conversation_guard.running_elsewhere(
            "claude", arguments[0], exclude_pane=os.environ.get("TMUX_PANE", ""))
        if location is not None:
            print(conversation_guard.refuse_message("claude", arguments[0], location), flush=True)
            return 0
        os.execvp("claude", ["claude", "--resume", arguments[0]])
    except operations.StartupAborted:
        print("wsctl: startup stopped; Claude pane preserved", file=sys.stderr)
        return 0
    except (OSError, ValueError) as error:
        print(f"wsctl: Claude startup refused: {error}", file=sys.stderr)
        return 2
    finally:
        # Reached only when exec did not happen: the client never started.
        if claimed:
            try:
                conversation_guard.release("claude", arguments[0])
            except OSError:
                pass  # The claim names this process and ends with it anyway.


if __name__ == "__main__":
    raise SystemExit(main())
