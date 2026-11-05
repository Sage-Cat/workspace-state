# Architecture simplification

## Goal

Workspace restoration converges without manual repair, whichever way the
computer was powered off:

- checkpoints are periodic and independent per category;
- every fact has one owner;
- restore is an idempotent per-item reconcile of the saved state with the live desktop;
- shutdown never waits for, blocks or cancels on workspace state.

## Principles

1. **Crash-only.** Freshness comes from periodic checkpoints. Shutdown adds at
   most one bounded final save and never blocks or cancels power-off.
2. **One owner per fact:**
   - Chrome owns tabs and groups;
   - wsctl and gnome-winctl own window placement;
   - the Codex and Claude CLIs own conversation content;
   - wsctl alone owns the tmux layout.
3. **Independent categories and items.** A category or item problem is a result,
   not a failed login, and never disables checkpoints.
4. **Risk-proportional verification:**
   - reversible actions (placement, labels) are best effort;
   - content-creating actions (resume, launch) need exact identity and global
     deduplication;
   - destructive actions need an explicit command.
5. **Reconcile instead of transactions.** Running restore again is safe and
   creates nothing twice.
6. **Unrestored items persist.** A saved item never seen live since login is
   carried forward as `pending` until it is restored or explicitly forgotten.
7. **Version safety.** New persisted semantics bump the snapshot schema. Older
   code refuses to overwrite a newer snapshot.
8. **Observability:**
   - one status document, which the HUD only presents;
   - startup incidents are warnings that resolve on the next good restore;
   - `doctor --summary` prints one line and returns a meaningful exit code.

## Non-goals

- No new features outside the milestones in `PLAN.md`.
- No changes to the VS Code, Nemo or social-application capture formats.
- No change to gnome-winctl placement semantics or to immutable releases and
  next-login activation.
- No live-desktop actions during development:
  - no save, restore or startup on the working desktop;
  - no stage, schedule or install;
  - no unit start or stop.

## Constraints

- **Public repository.** Sources, tests and docs carry no private paths,
  identifiers, logs or machine profiles.
- **Checks per milestone.** Every milestone leaves the product consistent and
  passes `make check`. Login HUD changes pass `make release-artifacts`. Before a
  pin update, the parent runs `make check` and `make integration`.
- **Existing installations keep working:**
  - enabled unit names that installations already reference stay valid, or the
    release migrates them;
  - hooks in an older tmux configuration that call removed interfaces become
    harmless shims.
- **Snapshot compatibility.** The new snapshot schema reads every older schema.
  Writers of older code refuse newer snapshots.
- **Test isolation.** Unit tests run through `scripts/run-tests.py` with every
  XDG directory isolated.

## Acceptance

Synthetic tests prove each item below; the project and parent checks pass:

- **Conversations.** A saved conversation is never resumed into a second pane:
  - an identity already running anywhere is reported, not launched;
  - an occupied session name is reported, never copied to a `-wsctl` session;
  - save keeps one pane per identity.
- **Checkpoints:**
  - a failed or partial category keeps its last good state;
  - other categories still save;
  - unrestored items stay pending;
  - checkpoints run periodically without a tmux client.
- **Startup:**
  - one entry point;
  - category problems end with exit 0 and per-item results;
  - repeated restore creates nothing twice.
- **Shutdown:**
  - no transaction, drain or cancellation;
  - a logind delay lock bounds one final save;
  - unprivileged power-off is not refused.
- **Chrome:**
  - matched windows are placed;
  - capture and restore never create, close, move or regroup tabs;
  - missing windows reopen only through an explicit command.
- **Deployment:**
  - an unambiguous installation receipt;
  - bounded release retention;
  - `doctor --summary`;
  - no private paths in published sources.
- **Disposable VM.** Acceptance (three power-off cycles, including an abrupt
  one) runs only with explicit consent and is recorded separately.
