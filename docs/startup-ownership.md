# Startup ownership and deadlines

The shell launcher and CLI use `workspace_state.startup.startup_directory` to
select the same private runtime directory. An early boot-only directory can be
adopted by exactly one login generation. Its `generation-owner.json` is published
under `startup-generations.lock`; a later login receives a separate directory
even when the user manager and old markers remain alive.

Startup is never suspended by shutdown: an old `startup-suspended.json` from an
earlier version is ignored. Cancellation of a startup does not authorize replay
of a partial startup; an explicit repair is a separate coordinator decision.

The managed startup worker has `RuntimeMaxSec` capped at seven minutes and at
the operation's remaining budget, independent of the activation timeout.
Its inherited operation context accompanies its service
failure reporter. A late or expired worker can report a failure only for its
own still-current startup operation; it cannot adopt a newer operation.

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

If application restoration is verified and only login finalization failed,
retry it with the exact operation ID from the HUD status document:

```sh
wsctl-login-finalize --retry-operation OPERATION_ID
```

This checks every category's completion evidence, creates a new attempt, and
reruns finalization without relaunching applications. The original deadline
still applies. Pending or failed providers, stale IDs, missing proof, and an
expired deadline are refused. Old workers cannot publish into the new attempt.
A coordinator restart after completion keeps the current login status and
re-arms the final save without repeating startup.

After manually recovering and verifying every provider, an operator can give
finalization a fresh bounded attempt when the old deadline has elapsed:

```sh
wsctl-login-finalize --retry-operation OPERATION_ID --new-attempt
```

This rotates the operation ID and increments the attempt under the ownership
lock. The same transition revalidates and carries the terminal session map,
including immutable tmux server/session anchors and exact Codex pane bindings.
Changed anchors or recipes refuse the retry; failed publication restores the
previous map and markers. It does not extend the old worker's authority or
restart applications.
The same-login proof check still applies.

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
