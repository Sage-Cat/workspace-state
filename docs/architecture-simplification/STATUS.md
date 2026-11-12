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
- Milestone 4, result semantics: published as `d79f4a2`; CI and its
  `build-<sha>` release verified.
- Milestone 5, shutdown without a transaction: the core, VM harness and
  Login HUD parts are implemented, independently reviewed in three rounds with
  every finding fixed, and checked with `make check`. The Login HUD part was
  published earlier in `login-hud` as `68e1ba6`; CI and its `build-<sha>`
  release verified. The parent still pins Login HUD 18, which cancels a menu
  power-off next to this core; milestone 10 moves both pins together.
- Disposable VM, milestone 3 build (`82f58ac`), proxy social windows (the
  guest's Viber snap exits in its sandbox): the seed and the logout cycle
  pass; startup completes in about 37 seconds with every stage ready and 23
  conversations resumed, and the periodic checkpoint saved after login.
  `verify` stopped on the old rule that the canonical checkpoint stays
  byte-frozen; a semantic comparison found only volatile fields changed,
  which `bc24695` now accepts.
- Disposable VM rerun with `bc24695`: the semantic baseline check passed. The
  run found two older defects, now requirements of milestones 6 and 7:
  - a tmux server that the restore worker starts lives in the worker's
    transient unit, and the unit's seven-minute runtime limit killed it with
    every conversation. On this desktop the bootstrap terminal starts tmux
    first, so the server lives in that terminal's unit instead;
  - a new login after a logout passed for the previous one and skipped
    restore. The login identity hashes the session manager's bus name, and
    the session bus restarted at logout handed out the same name; lingering,
    as on this desktop, keeps the runtime directory across logouts. The
    failed verification of that cycle follows from it.
  A third seed stopped on a readiness timeout that has not recurred yet.
- Known for milestone 9: older sources and tests still name the owner's home
  directory, drives and extension domain; an existing alert inventory gains
  the checkpoint source only through a migration.
- Milestone 8, first two parts, developed early at the owner's request after
  two logins in a row reported changed grouped Chrome windows as needing
  attention and left one on the wrong workspace:
  - startup pairs each native Chrome window with the saved window whose tab
    URLs single it out and places it with its current content;
  - periodic checkpoints capture Chrome read-only by window title, but only
    after the login verified its Chrome restore or a manual save accepted the
    live windows; the final save still keeps the last periodic recipe;
  - placement-only restore rechecks the content of its own window alone, so a
    page that changes during the login restore no longer blocks the others;
  - VS Code windows on folders under `/tmp` or the runtime directory are no
    longer saved or kept pending.
  Still open in milestone 8: `wsctl browser reopen` and the removal of content
  continuation and repair. Unequal window counts with a changed grouped window
  still use the strict restore and end needing attention. Title capture fails,
  and keeps the recipe, when two windows show the same active tab title or an
  Chrome window that the companion does not list is open; the planned marker
  fallback for such ambiguous windows is not implemented. The VM harness
  Chrome oracle (`--mutate-chrome`) has not been rerun with periodic Chrome
  capture.
- Next: publish milestone 5, check it on the disposable guest, then
  milestone 6.

## Decisions

- **URL overlap may pair Chrome windows for placement.** The fragment-only
  rule refused overlap because overlap is not an identity. Placement is
  reversible and Chrome owns tab content, so a mutual unique best overlap,
  and elimination for the last pair, now decide only where a native window
  goes; content is never adopted into the recipe by restore.

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
- **Only a complete baseline resumed checkpoints** while an unfinished
  startup or a cancelled shutdown could pause them: a kept recipe would
  otherwise have been replaced by a desktop whose restore never completed.
  Milestones 3 and 5 removed both pauses, together with the autosave marker
  and the shutdown guard.
- **`--shutdown-safe` kept its transaction rules until milestone 5,** which
  removed the option with the transaction.
- **`save --auto` keeps the guards whose replacements have not landed.**
  - Until milestone 8 it never captured Chrome. It now reads Chrome by window
    title once this login verified its Chrome restore or a manual save
    accepted the live windows; until then, and after a failed or ambiguous
    read, it retains the recipe and its observation witness. The final save
    never reads Chrome.
  - Topology or login changes and a fallback display skip the whole
    checkpoint with exit 0.
  - It never extends an acceptance.
  - Until milestone 5 it also skipped a shutdown operation and applied the
    manual-save shutdown checks; both went with the transaction.
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
  the conversation pending. The final save carries identities like any
  checkpoint; the seal that compared saved identities with live ones is gone.
- **A terminal capture whose only gaps are unresolved conversations is
  saved.** Those panes become shells and their saved identities stay pending;
  any other terminal problem keeps the whole terminal recipe.
- **Checkpoints publish optimistically.** A checkpoint takes the capture lock
  and the login's restore lock without waiting and captures without the state
  lock. It takes the state lock without waiting only to compare the saved
  bytes with its baseline and publish. A manual save waits up to 60 seconds
  for a running capture, and the final save up to 15 seconds. This removes
  the milestone 2 prerequisite: a checkpoint no longer holds the state lock
  while it captures.
- **The autosave marker is gone.** Pending carry-forward protects unrestored
  items, so checkpoints no longer wait for startup to finish. The timer starts
  two minutes after the session anchor and repeats five minutes after each
  run ends. The tmux hook runs a terminal-only checkpoint and keeps the
  previous tmux state file unless that checkpoint saved the current terminals.
- **A shutdown save carried pending items before the cancellation check**
  until milestone 5 removed the check with the transaction.
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
- **Shutdown is not an event.** The periodic checkpoint is the floor. The
  session agent holds a logind delay lock and adds one bounded final save; it
  never blocks or cancels a power-off, so the menu, `systemctl poweroff`,
  `sudo shutdown now` and a logout all keep at least the last checkpoint.
- **The first end signal starts the final save.** Whichever comes first,
  `PrepareForShutdown(true)` or GNOME `EndSession`, starts it once per
  shutdown attempt. `QueryEndSession` is answered at once, so the agent never
  delays the logout dialog. `EndSession` is answered after the save, and
  always with `True`. A cancelled shutdown re-arms the agent and takes the lock
  again.
- **The final save never removes an item.** VS Code and other Electron
  applications hold their own delay lock and quit on the same signal, and
  GNOME session clients quit on `EndSession`, so applications close while the
  save captures. Whatever is missing from that capture stays pending in its
  category. The cost: an item closed after the last checkpoint returns at the
  next login, where `wsctl forget` removes it.
- **The final save always publishes while the session is active,** even an
  unchanged desktop, so the snapshot the next login restores carries the
  `final-save` provenance and time. It publishes nothing once
  `graphical-session.target` is stopping, and it never extends an acceptance.
- **The final save owns the end of the session.** Ordinary checkpoints
  publish nothing from the first end signal until a cancellation, and nothing
  while `graphical-session.target` is not steadily active; a running
  checkpoint stops with the session. A periodic capture that overlaps the
  shutdown could otherwise publish a desktop whose applications are closing
  and leave the final save nothing to carry. The marker names the agent
  process, so a new login or a restarted agent ignores a stale one.
- **The agent outlives its final save.** `KillMode=mixed` sends `SIGTERM` to
  the agent alone, and neither that nor GNOME's `Stop` ends it while a final
  save runs within its budget; systemd then kills what remains.
- **Login HUD 19 or newer is required.** HUD 18 intercepts the GNOME
  power-off and, with no transaction left to answer it, cancels the shutdown
  after 15 seconds. Staging refuses an older HUD; milestone 10 moves both
  parent pins in one commit.
- **Virtual machine profiles are gone** (owner decision 6), with their
  startup stage and commands. Startup suspension went with the transaction.
- **Old shutdown files stay on disk.** The new code ignores them; removing
  them is left to the owner.
- **The tmux stop hook finds the server by its socket.** On 1 November the
  hook timed out asking a busy server and left tmux running. It now reads the
  listening socket's inode from the kernel, finds the one tmux process that
  holds it and sends `SIGTERM`, which tmux treats as kill-server.
- **Older defects go to the milestones that own them.** The tmux server's
  unit belongs to milestone 6, which removes the bootstrap terminal that now
  starts it; the login identity belongs to milestone 7, whose single restore
  entry point must run once per login.
