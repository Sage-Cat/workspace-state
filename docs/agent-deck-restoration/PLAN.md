# Agent Deck restoration

1. Confirm the Agent Deck CLI and workspace-state capture, restore and shutdown
   contracts. Preserve the startup operation and its deadlines.
2. Update ai-control rules to use Agent Deck. Coordinate the switch from the
   legacy identity hooks with workspace-state verification.
3. Add testable inventory, restore and dashboard integration. Keep Agent Deck
   records separate from the physical terminal layout so one dashboard can
   display many sessions.
4. Exercise the full save and restore path with disposable state. Review each
   repository independently, apply findings and rerun its checks.
5. Integrate and publish workspace-state by its release workflow. Record the
   verified release and its activation state.
