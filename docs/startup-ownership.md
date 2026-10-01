# Startup ownership and deadlines

The shell launcher and CLI use `workspace_state.startup.startup_directory` to
select the same private runtime directory. An early boot-only directory can be
adopted by exactly one login generation. Its `generation-owner.json` is published
under `startup-generations.lock`; a later login receives a separate directory
even when the user manager and old markers remain alive.

`startup-suspended.json` binds the shutdown barrier to one boot and login
generation. The launcher refuses automatic restoration for that generation.
Cancellation does not remove this barrier or authorize replay of a partial
startup; an explicit repair is a separate coordinator decision.

The managed startup worker has `RuntimeMaxSec` capped at seven minutes and at
the operation's remaining budget, independent of the activation timeout.
Its inherited operation context accompanies its service
failure reporter. A late or expired worker can report a failure only for its
own still-current startup operation; it cannot adopt a newer shutdown.

Entering shutdown runs the finite `wsctl-startup-barrier` helper. It stops only
the startup worker/finalizer units within 25 seconds and then allows at most ten
seconds to observe Chrome's accepted mutations and identification leases finish.
The companion exposes queued as well as executing mutations. A missing or old
companion cannot be treated as idle when native Chrome windows are present.
Failure prevents checkpoint capture; it does not close browsers or editors.

Chrome identification tabs have a 60-second ownership lease. The companion
records the token before creating the tab, cleans up failed identification,
and retains ownership across worker or browser restarts. Expiry cleanup removes
only the exact owned temporary page; it preserves user navigation, pinned or
grouped tabs, and the last tab in a window. It does not steal a live lease.
After stopping workers, the shutdown barrier can request expired cleanup once
per companion and then independently verify that Chrome is idle. Unknown
markers and unsuccessful cleanup still block capture. The HUD distinguishes
this failure from a worker that could not stop; details are recorded in the
full error log without waiting for a busy telemetry lock.

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
resumes shutdown coordination without repeating startup.

After manually recovering and verifying every provider, an operator can give
finalization a fresh bounded attempt when the old deadline has elapsed:

```sh
wsctl-login-finalize --retry-operation OPERATION_ID --new-attempt
```

This rotates the operation ID and increments the attempt under the ownership
lock. It does not extend the old worker's authority or restart applications.
The same-login proof and shutdown-suspension checks still apply.

VM completion receipts also record the login generation. Reusing a receipt
requires a live guest, a responding guest agent, and the saved viewer placement.
A new login can reconcile missing state. During the same login, a stopped or
moved VM requires an explicit `wsctl restore virtual-machines`; periodic checks
do not reopen or reposition it. Legacy receipts gain a generation only after
their live state is verified.

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
live GNOME session, application profile, cloud mount, or shutdown transaction.
