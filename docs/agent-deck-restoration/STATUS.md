# Status

- The workspace checkpoint owns dashboard scopes and saved conversation
  bindings. Agent Deck owns the session registry and launches the original
  Claude Code or Codex conversation through the guarded CLI.
- The focused tests cover ambiguous bindings, duplicate prevention, group
  scope, explicit stops, and pending dashboard recovery. The isolated
  save-and-restore check used an actual Agent Deck dashboard, a temporary
  tmux server and a synthetic Claude executable.
- Production activation requires a compatible Agent Deck binary and a
  checkpoint captured while the intended dashboards are running.
