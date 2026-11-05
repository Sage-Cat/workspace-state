# Plan

Milestones land in order. Each one passes `make check` here, gets an
independent review when substantial, and is committed and published through
the repository route before the next one starts.

## Interfaces fixed across milestones

- **Anchor unit.** `wsctl-gnome-session.service` stays the enabled unit name
  that installations already reference. It becomes the small session agent:
  - it starts login restore once;
  - it holds a logind delay lock and runs one bounded final save;
  - it pulls in the checkpoint timer with `Wants=`.
- **Kept names.** `wsctl-workspace-restored.target` and
  `wsctl-login-finalize.service` keep their names, because drive drop-ins and
  enabled links on existing installations reference them. Their bodies become
  simpler.
- **Old tmux configurations.** These call `wsctl tmux save|begin|restore|end`
  and the `wsctl-continuum-*` wrappers. After milestone 6 those entry points
  are harmless shims that exit 0.
- **Snapshot schema 6** adds per-item `pending` records and the provider
  `conversation` field as versioned semantics. Version 5 snapshots stay
  readable. Writers refuse a snapshot newer than they understand.
- **Final save budget:** `min(20 s, InhibitDelayMaxSec - 3 s)`, at least 1 s.

## Milestones

1. **Conversation deduplication.** One live client per provider and UUID.
   - Changes:
     - build a global live index from `tmux list-panes -a` before every resume;
     - keep the `<name>-wsctl` fallback, but never start a conversation there
       that already runs elsewhere;
     - route partial adoption through the index;
     - save keeps one pane per identity.
   - Files: `restore.py`, `codex_resume.py`, `claude_resume.py`,
     `storage.py` and the capture code, plus tests.
2. **Independent save and schema 6.**
   - Changes:
     - a capture failure in any category keeps that category's last good state
       and no longer aborts the save;
     - add `save --auto`;
     - schema 6 with refusal of newer snapshots;
     - UTC provenance timestamps.
   - Files: `cli.py` (capture and save), `checkpoint.py`, `storage.py`, plus tests.
3. **Pending items and periodic checkpoints.**
   - Changes:
     - a per-login record of the items seen live;
     - pending carry-forward and `wsctl forget`;
     - `wsctl-checkpoint.timer` and `.service`;
     - tmux hooks delegate to `save --auto`;
     - remove `autosave.ready`;
     - accept the exact `claude --resume UUID` argv for panes wsctl launched.
   - Files: `cli.py`, `checkpoint.py`, `claude_identity.py`, `systemd/`, plus tests.
4. **Result semantics.**
   - Changes:
     - `needs-attention` item results with exit 0;
     - remove the `<name>-wsctl` fallback: an occupied session name becomes a
       `needs-attention` result whose items stay pending;
     - a startup incident is a warning that resolves on the next good restore;
     - fix the finalizer receipt deletion;
     - one shared conversation verification deadline.
   - Files: `provider_progress.py`, `login_status.py`, `alerts.py`,
     `login_finalize.py`, `cli.py`, plus tests.
5. **Shutdown without a transaction.**
   - Changes:
     - remove sealing, the HUD countdown, equality checks, drain, profiles and
       the shutdown finalizer;
     - replace the block lock with a delay lock and a bounded final save.
   - In parallel, a worker removes the Login HUD shutdown interface in
     `login-hud` (`extension.js`, `hudView.js`, `reports.js`, its tests and
     protocol docs). The HUD keeps login progress, hide and abort.
6. **One tmux owner.**
   - Remove tmux-resurrect and continuum from the loop: `resurrect.py`,
     `codex_launch_scope.py`, the continuum wrappers, marker helpers, adopt
     and repair paths, and the bootstrap terminal.
   - Keep compatibility shims for old configurations.
7. **Direct start.**
   - One restore entry point, started once per login by the session agent;
     categories run in parallel.
   - Remove the launcher, the `systemd-run` worker, the claim and generation
     markers, and the duplicate readiness waits.
8. **Chrome placement only.**
   - Match native windows by an overlapping multiset of URLs and place them.
   - Read-only capture identifies windows by title; a marker only for
     ambiguous windows.
   - Add `wsctl browser reopen`; remove content continuation and repair.
   - Files: `chrome-extension/service-worker.js`, `browser.py`,
     `browser_reconciliation.py`, plus tests.
9. **Deployment, privacy and test hygiene.**
   - An installation receipt written at apply time.
   - Release retention.
   - `doctor --summary`.
   - Parameterize host drive units and paths, and remove private paths.
   - Isolate every XDG directory in tests.
10. **Documentation and integration.**
    - Update README, architecture and shutdown diagrams, lifecycle, provider,
      terminal, reference, deployment, testing and contributing documents.
    - Update the parent pin, then run the parent `make check` and
      `make integration` and publish.
    - Apply the rule-source documentation updates as a separate local commit.
11. **Disposable-VM acceptance.** Three power-off cycles, one of them abrupt.
    This needs explicit consent to power on the validation guest.

## Dependencies

- Milestones 1–4 come before 5–8.
- Milestone 5 comes before 7, because the session agent replaces the
  coordinator.
- Milestone 6 comes before 7.
- Milestone 8 is independent of 5–7 except for shared status helpers.
- Milestones 9 and 10 come last; 11 waits for consent.
