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

- Milestone 1, conversation deduplication: implemented, reviewed and checked
  with `make check`.
- Next: milestone 2, independent save and schema 6.

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
