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
- Milestone 3, pending items and periodic checkpoints: published as
  `2a2d518`, together with the Claude resume argv commit `4e8f252`; CI and
  its `build-<sha>` release verified.
- VM harness fixes `82f58ac` (the seed waits for a compositor-owned Chrome
  placement) and `bc24695` (semantic checkpoint baseline): published; CI and
  releases verified.
- Milestone 4, result semantics: implemented, reviewed twice with every
  finding fixed, and checked with `make check`. Its tests include a repeated
  restore in one login that copies no session and starts no conversation
  twice.
- Milestone 5, Login HUD part: published in `login-hud` as `68e1ba6`; CI and
  its `build-<sha>` release verified. The VM harness part is committed on its
  task branch and waits for the core part.
- Disposable VM, milestone 3 build (`82f58ac`), proxy social windows (the
  guest's Viber snap exits in its sandbox): the seed and the logout cycle
  pass; startup completes in about 37 seconds with every stage ready and 23
  conversations resumed, and the periodic checkpoint saved after login.
  `verify` stopped on the old rule that the canonical checkpoint stays
  byte-frozen; a semantic comparison found only volatile fields changed,
  which `bc24695` now accepts. The reboot and pending scenarios rerun on the
  same build.
- Known for milestone 9: older sources and tests still name the owner's home
  directory, drives and extension domain; an existing alert inventory gains
  the checkpoint source only through a migration.
- Next: milestone 5, shutdown without a transaction, core part.

## Decisions

- **The `<name>-wsctl` fallback lasted until milestone 4.** Without pending
  carry-forward, refusing an occupied session name would have dropped its
  conversations at the next save, so milestones 1–3 kept the copy and the
  live index kept it from starting any conversation twice.
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
- **An occupied session name is a result, never a copy.** A live session with
  the saved name and a layout that no reconcile, adoption or partial append can
  use creates nothing. Its items get a failed item result, are not recorded as
  seen and stay pending; the other sessions continue. A `NAME-wsctl` copy left
  by an older build is still reconciled.
- **Pending conversations of an occupied name join the live session.** After
  the next checkpoint the occupant's name is seen, so the unseen conversations
  are carried as windows of that live session. The next restore appends them
  through the existing partial-append reconcile. Nothing is copied, and the
  live index keeps any conversation from starting twice.
- **Partly restored is degraded, nothing verified is failed, waiting wins.**
  A category with a waiting item stays `waiting` until the observer settles it,
  even when another item failed. All of these end `wsctl startup` with exit 0.
  Only orchestration failures exit non-zero: an unreadable snapshot, the
  startup lock, missing GNOME capabilities, a failed `workspace` stage, a
  cancelled or expired operation. A provider error that carries item results
  is judged by those results like any other category.
- **A degraded category is marked `attempted`.** The stage marker has no
  degraded state; `attempted` stops automatic retries the same way, and the
  observer rewrites the marker when it settles a category, so a repeated
  startup in the same login keeps the settled state.
- **The finalizer reports instead of failing.** It exits 0, marks
  `login-finalization` ready and names the stages that need attention. The
  `.outcome.json` receipt and its alert reader are gone, which replaces the
  planned fix of the receipt deletion. A crash, timeout or lost ownership still
  fails the unit; that incident is a `warning` and resolves at the next
  successful run.
- **One conversation deadline per login.** The first startup restore or
  finalizer that verifies conversations creates it in the startup directory:
  at most 180 seconds on the system-wide monotonic clock. Later users read the
  same instant; a damaged file or one from another boot never extends it. The
  deadline bounds only the wait for missing conversations: once all are
  present, the three-second stability window always completes. A restore
  whose own bound ends first leaves the stage `waiting`, and the finalizer
  verifies until the shared deadline. The finalizer skips sessions that need
  attention; their conversations are reported pending. A manual restore keeps
  its own bounded wait and writes no deadline.
- **A manual restore with items needing attention exits 3,** like a partial
  save; exit 1 still means nothing was selected.
- **The operation state stays `failed` when a stage failed,** although
  startup and the finalizer exit 0: the HUD shows the result, and no alert is
  raised for it.
- **VM baselines are semantic.** After milestone 3 the periodic checkpoint
  legitimately rewrites the canonical snapshot. The harness keeps the fixture
  and viewer digests strict and accepts a changed canonical only when every
  changed category came from `auto-save` or `final-save` and the content,
  placement, tmux intent and workspace names still match the frozen copy.
- **Parts of a later milestone may start early.** The Login HUD and VM
  harness parts of milestone 5 touch no file of milestone 4, so they were
  developed in parallel worktrees; milestones still land in order.
