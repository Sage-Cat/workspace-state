# Startup ownership and deadlines

The session agent and the CLI use `workspace_state.startup.startup_directory` to
select the same private runtime directory, `startup-<boot id>-<login key>`.
Every login gets a directory of its own, even when the user manager stays alive
and lingering keeps the runtime directory. Nothing is adopted from an earlier
directory: the files that older releases left in `startup-<boot id>` (the
`*.done` and `*.deferred` markers, `browser-items`, `terminal-session-map.json`,
`generation-owner.json` and `launcher.claimed`) are ignored and stay on disk
until the runtime directory is cleared.

A login has two identifiers, because two readers need different things:

- The session id is the first 16 hex digits of SHA-256 over the unique D-Bus
  name that owns `org.gnome.SessionManager`. It is in
  `$XDG_RUNTIME_DIR/workspace-state/login-generation`, in the status document
  as `session_id` and in every operation context as `login_generation`. The
  login HUD computes the same value itself and shows a status only when its
  `session_id` matches and an abort only for a matching `login_generation`, so
  this value must stay the bus-name hash.
- The login key is the same hash over `<boot id>:<pid>:<start time>` of the
  session manager process. The PID comes from `GetConnectionUnixProcessID`
  and the start time is field 22 of `/proc/<pid>/stat`. It is written to
  `login-key` beside `login-generation`, with the same private mode.

The session bus restarts at logout while lingering keeps the runtime
directory, so the next login in the same boot can get the same unique name and
therefore the same session id. The login key tells such logins apart. It names
the startup and seen-item directories, the native status evidence scope and the coordinator receipt, and it
is the top-level `login_key` field of the status document, next to
`login_process`, the process hash itself. The coordinator reattaches to a
completed status, and the startup HUD claim survives a coordinator restart,
only for the same login key. The HUD ignores the extra field. If the process
cannot be identified, the login key is the session id and a `login identity`
diagnostic is logged.

A release without login keys names a login's directory, status and claim by the
session id. When a newer release starts in a login that such a release began
(same boot and session id, no `login_key` in the status, and the session
manager process started no later than the status did, with 2 seconds of
tolerance), that login keeps the
session id as its key until it ends: it reattaches, the HUD stays visible, and
the restore service keeps the same directory and `restore.lock`.
A status without a key from a session manager that started later is an earlier
login, and a fresh operation starts.

`login-key` is written after `login-generation`. A key older than
`login-generation` was left by an earlier login (for instance by a rolled-back
release) and is ignored, as is a missing one; the session id is the key then.
The restore service is named `wsctl-startup-restore-<login-key>.service`. The
session agent records it in the status document (top-level `restore_unit` and
`restore_login_key`, never inside `operation_context`) before it starts the
service; an agent restarted in the same login finds the record and starts no
second restore, because a second `wsctl startup` in one login falsely marks
live Codex conversations failed.

Startup is never suspended by shutdown: an old `startup-suspended.json` from an
earlier version is ignored. Cancellation of a startup does not authorize replay
of a partial startup; an explicit repair is a separate coordinator decision.

The managed restore service has `RuntimeMaxSec` capped at seven minutes and at
the operation's remaining budget, independent of the activation timeout.
Its inherited operation context accompanies its service
failure reporter. A late or expired worker can report a failure only for its
own still-current startup operation; it cannot adopt a newer operation.

When the service ends without success (for example at `RuntimeMaxSec`), the
reporter fails every restore stage that is still running or pending, and every
waiting stage that has results but no compositor-owned placement request. A
stage that GNOME still owns, a stage waiting for the cloud drives, and the
finalizer-owned stages (drives, warm-up, login finalization) are left alone.
The reporter then starts `wsctl-workspace-restored.target` without blocking, so
the finalizer mounts the drives and closes the operation. A superseded or
previous-boot operation changes nothing and starts nothing; a successful
service result does nothing.

Within one Chrome profile, the first window whose request fails because the
native host does not answer fails the profile's remaining windows with the same
reason and no further request, so a dead host costs one deadline per profile,
not one per window. Other Chrome errors still fail only their own window.

The periodic and final saves wait for no startup helper. Chrome's accepted
mutations and identification leases belong to the startup worker and end with
it.

Chrome identification tabs have a 60-second ownership lease. The companion
records the token before creating the tab, cleans up failed identification,
and retains ownership across worker or browser restarts. Expiry cleanup removes
only the exact owned temporary page; it preserves user navigation, pinned or
grouped tabs, and the last tab in a window. It does not steal a live lease.
An expired lease can be cleaned up once per companion; unknown markers are
left alone.

No checkpoint writes a `latest_observation` any more. One that an older
checkpoint already carries is validated against its bound capture evidence and
is never restored automatically; `wsctl save` explicitly adopts the current
desktop, and the previous checkpoint remains in the private recovery history.

A page that finishes loading at the verification deadline remains waiting
until its stable completion is observed. It is not reported as a permanent
failure merely because a second confirmation sample was still required.

The login finalizer binds the startup operation before starting work. Commands
inherit that authority and use the lesser of their local timeout and the
operation's remaining monotonic budget. The shared startup budget is 25 minutes;
the systemd finalizer watchdog is 26 minutes, leaving time for failure reporting.
An invocation-specific receipt lets `ExecStopPost` report a killed or timed-out
finalizer without taking ownership from a replacement operation. No recovery
journal is removed when startup work expires.

A finalizer that failed is not retried by a command: it reports instead of
failing, so a result that needs attention is shown by the HUD and a new login
starts again. A coordinator restart after completion keeps the current login
status and re-arms the final save without repeating startup.

A repeated `wsctl startup` in the same login is a reconcile, not a retry. While
the startup operation is `running` it compares the live desktop with the saved
snapshot and restores what is missing; a category whose placements the
compositor still owns (a `waiting` stage with a pending request) is left alone,
so no second request is queued, and skips Chrome once this operation restored
its windows. A storage category (file manager, VS Code) that waits for the cloud
drives is deferred again until the login finalizer starts, belongs to the
finalizer while it runs (a repeat leaves it as it is) and is restored directly
by a repeat after the finalizer ended. Once the operation has completed, failed,
been cancelled or expired, the command is refused with a message and changes
nothing; `wsctl restore` is the manual reconcile. A reconcile returns a window
that the user moved to its saved place, which matters only for a manual repeat
or a development install in the middle of a login. `--force` is accepted and
ignored.

Owned-system alert scans run up to four read-only probe process groups. At the
scan deadline, unfinished groups are killed and joined; only the parent writes
the incident database. An unfinished probe preserves its prior journal cursor
and incidents, and reports `scan-timeout`. This avoids the unbounded executor
shutdown wait caused by cancelling a Python thread future whose probe is already
running. Kernel-uninterruptible I/O cannot be made immediately killable by user
space, so the supervisor also bounds its reap wait; these probes do not traverse
cloud mount contents.

Focused regression coverage lives in `test_startup_ownership.py`,
`test_alert_probe_deadline.py`, and `test_login_finalize_boundary.py`. These use
temporary runtime/state directories and synthetic probes, without touching a
live GNOME session, application profile or cloud mount.
