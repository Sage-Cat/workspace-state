# Conversation restoration

## Goal

Save and restore the exact interactive Codex and Claude conversations that are
open in tmux panes. A newly created conversation replaces any older conversation
that was closed in that pane at the next successful save.

## Boundaries

- A conversation is identified by its provider and immutable UUID, not by a
  window name, title, working directory, latest transcript, or prompt text.
- Restore only an exact saved UUID in the intended tmux pane. Never launch a
  fresh client when an identity is missing or ambiguous.
- Keep old Codex snapshots readable. Do not alter a live client, credentials,
  model settings, or the desktop during source development.
- Store Claude hook receipts in private local state. Never publish transcripts,
  local UUIDs, or machine profiles.

## Acceptance

Synthetic tests prove fresh and resumed Claude identity capture, mixed-provider
save/restore, native tmux checkpoint equivalence, stale/closed process rejection,
duplicate prevention, and startup readiness. Existing Codex and desktop tests
continue to pass. The project and parent integration checks pass.
