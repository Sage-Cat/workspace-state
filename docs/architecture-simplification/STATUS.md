# Status

- Base: child `main` at `5b85dbbec76d815c52720d5a4784cde92e4c9626`.
- Scope: source, tests and docs only. The installed release and the live
  desktop stay untouched: no stage, schedule, install, save, restore or unit
  changes.
- Requested by the owner: implement the reviewed simplification plan in full,
  using its recommended decisions.
- Deviation from the review: `wsctl-login-finalize.service` and
  `wsctl-workspace-restored.target` keep their names, because existing
  installations enable them and drive drop-ins require them.
- Owner decisions about host configuration are tracked outside this
  repository; no milestone depends on them.

## Progress

- Milestone 1, conversation deduplication: published as `3e8e217`; CI and
  its `build-<sha>` release verified.
- Milestone 2, independent save and schema 6: implemented and checked with
  `make check`.
- Next: milestone 3, pending items and periodic checkpoints. Before the timer
  runs `save --auto`, the shutdown guard must stop failing at once on a held
  state lock: it locks without waiting, and a checkpoint holds the lock for
  its whole capture.

## Decisions

- **The `<name>-wsctl` fallback stays until milestone 4.** Without pending
  carry-forward, refusing an occupied session name would drop its
  conversations at the next save. The live index already keeps the copy from
  starting any conversation twice. Milestone 4 replaces the copy with a
  `needs-attention` result once milestone 3 keeps unrestored items pending.
- **Conversation claims are records, not held locks.** A claim names the
  launcher's process ID, start time and boot. Exec keeps the process ID, so a
  Claude client keeps its launcher's claim; the Codex supervisor holds it for
  its lifetime. An inherited lock descriptor was rejected: any long-lived
  child of a client could keep it and refuse later resumes. A claim follows a
  process, so a client that switched threads can only cause a refusal.
- **The live index uses cheap evidence only:** resume argv, open rollouts,
  waiting launchers, Claude hook receipts and native Codex title prefixes.
  Title evidence, including a title that contradicts the argv, may refuse a
  second client but never verifies a restore, so weak evidence ends visibly
  degraded rather than silently lost. App-server probes stay in capture,
  because every launcher builds the index. One `/proc` snapshot serves the
  whole index.
- **The index excludes only the target session.** A legacy copy is excluded
  only while it is the session being reconciled.
- **Unit tests never reach the live tmux server.** The runner points
  `TMUX_TMPDIR` at its temporary directory and drops `TMUX` and `TMUX_PANE`.
- **Every category saves independently.** A failed or incomplete category
  keeps its last good data; terminals keep their whole recipe (sessions,
  clients and desktop). A plain manual save publishes the complete categories
  and exits 3. `--allow-partial` also accepts an incomplete terminal capture
  and exits 0; a terminal capture that failed outright is never accepted.
  Without a last good state, an incomplete category is published marked
  failed and is never accepted.
- **Only a complete baseline resumes checkpoints.** A manual save resumes
  paused automatic checkpoints, or ends the protection of a cancelled
  shutdown, only when it set the baseline of every category that checkpoints
  rewrite. They never rewrite Chrome, so a kept Chrome recipe does not count.
  Otherwise a later checkpoint could replace a kept recipe with a desktop
  whose restore never completed. While a cancelled shutdown's checkpoint is
  protected, such a save is refused instead: the sealed checkpoint may change
  only together with its protection, or the next shutdown fails.
- **`--shutdown-safe` keeps its transaction rules until milestone 5.** Two
  failures now keep the last good state and degrade the save (exit 3)
  instead of aborting it: a desktop-app failure and a Chrome capture
  exception. An incomplete Chrome capture already did this.
- **`save --auto` keeps today's guards until their replacements land.**
  - It requires the autosave marker until milestone 3 keeps unrestored items
    pending.
  - It skips a shutdown operation until milestone 5.
  - It never captures Chrome before milestone 8; the browser recipe and its
    observation witness carry forward.
  - Any terminal problem keeps the whole terminal recipe until milestone 3
    carries forward the identity of an unchanged client.
  - Topology or login changes, a fallback display and conversations omitted
    after a cancelled startup skip the whole checkpoint with exit 0.
  - It applies the manual-save shutdown checks, because recovery and armed
    profile rollback can outlive the operation deadline, and repeats them
    before publication.
  - It never waits for the state lock and never extends an acceptance.
- **Schema 6 is written before its pending semantics exist.** Milestone 3 adds
  them. No intermediate build is activated, so no installation writes version
  6 before its semantics are final.
- **UTC only for new snapshots.** Bindings compare a snapshot's timestamp
  strings exactly, so migration never rewrites them; `show` prints local time.
