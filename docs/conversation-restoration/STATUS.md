# Status

- Base: child `main` at `0a9bba20c82944a7655aa4c3e12886785a1cd744`.
- Scope: source implementation only; live desktop and installed release untouched.
- Evidence: Claude CLI 2.1.316 supports exact `--resume UUID`; a bare Claude
  process does not expose its UUID. `SessionStart` supplies the exact ID.
- Claude receipt and exact mixed-provider restoration implemented. The
  compatibility fingerprint for existing Codex-only sessions is preserved.
- `make check`: 1,431 tests passed; mixed-provider, stale receipt, empty
  transcript, delayed hook events, delayed readiness and cancelled launch
  cases are covered.
- Next: final review, parent integration and source publication. Hook
  registration and installed-release activation remain separate host actions.
