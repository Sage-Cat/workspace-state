# Plan

1. Establish provider-qualified snapshot identity and a Claude hook receipt
   tied to the live foreground process. Validate with isolated fixtures.
2. Extend canonical and native tmux restoration to exact Claude UUIDs while
   retaining old Codex snapshots. Validate mixed-provider and stale-process
   behavior.
3. Extend save, shutdown, startup, HUD, and diagnostics to both providers.
   Run `make check` in this component.
4. Review the complete diff, run parent `make check` and `make integration`,
   publish through the repository route, and verify remote commits and CI.

One worker owns the new Claude identity/receipt module and its focused tests in
an isolated worktree. The main agent owns all existing modules, docs, integration,
and publication. The new module exposes `claude_for_pane`, `resumed_session`, and
the hook entry point; the main agent will use those interfaces.
