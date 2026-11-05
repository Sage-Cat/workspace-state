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
- Milestone 2, independent save and schema 6: published as `7acdd05`; CI
  and its `build-<sha>` release verified.
- Milestone 3, pending items and periodic checkpoints: implemented and
  checked with `make check`. Its exact Claude resume argv part is the local
  commit `4e8f252`, published together with the rest of the milestone.
- Known for milestone 9: older sources and tests still name the owner's home
  directory, drives and extension domain.
- Next: milestone 4, result semantics. Its tests must cover a repeated
  restore in one login: while the `<name>-wsctl` fallback exists, a second
  restore can copy a live session whose carried window already started when
  one of its other conversations is missing. Milestone 4 removes the fallback.
- Disposable-VM validation runs alongside, on candidates built from
  committed sources. The `4e8f252` candidate could not seed real social apps:
  the guest's Viber snap exits in its sandbox before opening a window. The
  rerun uses the harness's proxy windows for those four apps.

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
- **`save --auto` keeps the guards whose replacements have not landed.**
  - It skips a shutdown operation until milestone 5.
  - It never captures Chrome before milestone 8; the browser recipe and its
    observation witness carry forward.
  - Topology or login changes and a fallback display skip the whole
    checkpoint with exit 0.
  - It applies the manual-save shutdown checks, because recovery and armed
    profile rollback can outlive the operation deadline, and repeats them
    before publication.
  - It never extends an acceptance.
- **Pending data stays in its category.** A carried session, window or app
  record keeps its normal place and format; the top-level `pending` index only
  names it. Restore therefore needs no merge, and the capture formats of
  VS Code, Nemo and social apps stay unchanged.
- **Seen means live during this login.** A key is seen when a capture found
  it live or a restore verified it. Attempts are not seen: a waiting resume
  wrapper, a supervised Codex resume that has not verified its client, and a
  claimed Claude client without its hook receipt. A sign-in, update or startup
  prompt can hold either client in that state indefinitely. The cost is that a
  working client whose verification never succeeds stays pending until it is
  forgotten. A client started by hand counts by its evidence. The record lives
  in the login's startup directory. Without a login identity there is no
  record and every unseen item stays pending, because dropping an item is the
  irreversible direction. Plain shell windows are not items.
- **One conversation, one saved pane.** An identity anywhere in the current
  capture, whatever its evidence, is never carried a second time.
- **A closed session keeps only its unseen conversations.** A session that was
  live during this login and is gone now was closed or renamed: its shell
  windows and terminal client go, while windows holding conversations never
  seen stay pending under the old name. Forgetting the last of them removes
  the session.
- **An unchanged client keeps its identity.** A conversation pane that
  momentarily lacks evidence (`unknown` for Codex, `missing-hook-receipt` for
  Claude) keeps its saved identity when the tmux server, pane, process ID and
  start time all match the last capture. Contradicting evidence instead holds
  the conversation pending. Shutdown saves do not carry identities, because
  the seal compares saved identities with live ones until milestone 5.
- **A terminal capture whose only gaps are unresolved conversations is
  saved.** Those panes become shells and their saved identities stay pending;
  any other terminal problem keeps the whole terminal recipe.
- **Checkpoints publish optimistically.** A checkpoint takes the capture lock
  and the login's restore lock without waiting and captures without the state
  lock. It takes the state lock without waiting only to compare the saved
  bytes with its baseline and publish. A manual save waits up to 60 seconds
  for a running capture. This removes the milestone 2 prerequisite: a
  checkpoint no longer holds the state lock while it captures, and the
  shutdown guard retries that lock for up to two seconds.
- **The autosave marker is gone.** Pending carry-forward protects unrestored
  items, so checkpoints no longer wait for startup to finish. The timer starts
  two minutes after the session anchor and repeats five minutes after each
  run ends. The tmux hook runs a terminal-only checkpoint and keeps the
  previous tmux state file unless that checkpoint saved the current terminals.
- **A shutdown save carries pending items before the cancellation check.**
  Only a conversation seen and then closed during this login can still trip
  it. Milestone 5 removes the check with the transaction.
- **Schema 6 carries the pending index.** No intermediate build is activated,
  so no installation writes version 6 before its semantics are final.
- **Checkpoint failures raise alerts.** `wsctl-checkpoint.service` is in the
  owned-systems inventory, so a failing periodic checkpoint becomes an
  incident instead of going unnoticed. Installation copies the inventory only
  when it is absent, so an existing inventory gains the source only through a
  migration; milestone 9 adds it.
- **Startup tests reset what they bind.** Three startup tests left a status
  file and an operation binding behind; later tests passed only because of
  that order. They now isolate the runtime directory and unbind. The deferred
Codex tests stub the live conversation index: it ran tmux, whose exit timing
reached their patched global sleep.
- **UTC only for new snapshots.** Bindings compare a snapshot's timestamp
  strings exactly, so migration never rewrites them; `show` prints local time.
