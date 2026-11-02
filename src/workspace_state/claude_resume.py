"""Launch one exact saved Claude conversation with startup ownership."""
from __future__ import annotations

import os
import sys

from .capture import UUID_RE
from .codex_resume import decode_startup_context
from . import operations


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if len(arguments) not in {1, 2} or not UUID_RE.fullmatch(arguments[0]):
        print("wsctl: expected Claude UUID and optional startup context", file=sys.stderr)
        return 2
    try:
        context = decode_startup_context(arguments[1]) if len(arguments) == 2 else operations.current()
        inherited = operations.current()
        if context is not None:
            if context.mode != "startup" or (inherited is not None and inherited != context):
                raise ValueError("Claude launcher belongs to another operation")
            with operations.publisher(context):
                context.check()
        os.execvp("claude", ["claude", "--resume", arguments[0]])
    except operations.StartupAborted:
        print("wsctl: startup stopped; Claude pane preserved", file=sys.stderr)
        return 0
    except (FileNotFoundError, TimeoutError, ValueError) as error:
        print(f"wsctl: Claude startup refused: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
