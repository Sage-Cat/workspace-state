# Restore Agent Deck with the desktop

Use Agent Deck as the source of truth for AI sessions on desktops that use it.
Keep the dashboard in each saved terminal and keep the registered sessions in
their original profile, group and account.

- Checkpoint the registry's session IDs and native conversation IDs. Restore
  dashboards and conversations that were open. Keep stopped and archived
  conversations available without launching them.
- Reuse a live conversation by exact identity. An incomplete inventory or an
  ambiguous, stale or missing identity must remain pending.
- Keep older checkpoints readable. In Agent Deck mode, a legacy record can be
  matched to a registered conversation by its native ID. Keep unmatched
  evidence for manual recovery.
- Retain terminal layout, shell placement, startup ownership, failure reporting
  and non-terminal providers. Use private checkpoint files and public CLI
  interfaces. Keep provider authentication and transcripts with the providers.
- Test exact-ID recovery, scoped dashboards, missing tools and legacy snapshots
  in isolated environments. Review the rules and product changes independently.
