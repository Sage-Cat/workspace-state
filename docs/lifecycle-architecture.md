# Lifecycle ownership and recovery

See the [component diagram](architecture.svg) ([PlantUML](architecture.puml)) and
[shutdown sequence](shutdown-flow.svg) ([PlantUML](shutdown-flow.puml)) for a visual
overview. The [user guide](visual-guide.md) connects these contracts to the HUD.

The existing GNOME session client is the coordinator. A process-held, nonblocking
runtime lock prevents a second client from taking ownership. `operations.py`
defines its immutable publisher context and legal transitions; `login_status.py`
serializes publication. There is no additional lifecycle daemon or database.

Seven lifecycle Python entry points delegate environment setup to `bin/wsctl-python`.
It resolves the immutable component path once and exports its source and binary
directories plus the user's local binary directory. Deferred login workers use
the same environment as the coordinator; systemd's shorter default PATH cannot
hide the installed GNOME placement companion.

Nemo, social applications and VS Code resolve each placement target from one
current GNOME topology observation. Monitor identity and unique workspace names
remain mandatory. Repeated observations within that calculation no longer
produce inconsistent decisions or redundant companion calls.

Every worker context contains `boot_id`, `login_generation`, `operation_id`,
`mode`, `attempt`, and `deadline`. The deadline is an absolute monotonic time,
valid only in its recorded boot. Context is propagated through subprocess
environments, systemd worker environments, thread work and callback closures.
Callbacks also carry a coordinator epoch so recycling an operation ID cannot
revive an old callback. Startup has a 25-minute total budget. Local timeouts can
only shorten it. Recovery is allowed after the work deadline; commit is not.

`current-operation.json` is the authoritative owner receipt. HUD status is an
atomic presentation/progress document. Both use the same publication lock.
A publisher must match the exact context and owner receipt before updating
status. Damaged startup presentation may be reconstructed for its existing owner. An
unbound compatibility publisher may adopt startup once. New managed workers bind
before work. Old `shutdown-*` files and `mode: "shutdown"` status documents left
by earlier versions are ignored.

## Shutdown

Shutdown is not an operation and has no owner, receipt, HUD mode or rollback.
Nothing in wsctl cancels or blocks a power-off. The floor is the periodic
checkpoint (`wsctl-checkpoint.timer`, at most five minutes old). On top of it the
session agent (the coordinator) holds one logind `delay` inhibitor and, on
`PrepareForShutdown(true)` or GNOME `EndSession`, runs one bounded
`wsctl save --auto --final` in its own process group. The budget is
`min(20 s, InhibitDelayMaxSec - 3 s)`, at least 1 s; at the deadline the group
gets `SIGTERM`, a second later `SIGKILL`. `EndSession` is answered after the save
ended or timed out, always with `True`; `QueryEndSession` is answered at once;
`CancelEndSession` and `PrepareForShutdown(false)` re-arm the agent.

The final save is a periodic checkpoint that waits for a running capture and
publishes nothing while `graphical-session.target` is not steadily `active`,
because applications closing during teardown would be recorded as the workspace.
It records `final-save` provenance, which never extends a manual acceptance.
It adds and updates but never removes: applications holding their own delay lock
or exiting on `EndSession` close while it runs, so anything the previous
checkpoint had that this capture lacks stays pending until the next login.
`wsctl-tmux-shutdown.service` finds the tmux server from the socket table and
sends it `SIGTERM`. The [reference](reference.md) lists the journal lines.

Startup problems in one category are results. `wsctl startup` and the login
finalizer exit 0 with `degraded` or `failed` stages and the overall message
"Login completed with items needing attention"; only orchestration failures,
exceptions, timeouts and lost ownership exit non-zero. A tmux session whose saved
name is taken by a different live layout is never copied: its items stay pending.
One shared deadline of at most three minutes bounds conversation verification
for the restore and the finalizer together. See the
[reference](reference.md) for details.

## Application evidence and placement

Providers retain application-specific recovery and expose common phase evidence:
identity, content and placement, plus attention and retryability. An accepted
placement has a request token. Deferred placement retains physical monitor intent
and that original token through replay; only observed completion is `verified`.
Only a request for the final saved placement may be reported as waiting. A
temporary staging move cannot prove completion of a final move that was never
submitted; a staging timeout instead reports a retryable failure.
Chrome removes its temporary identification tab before moving the window and
requires a stable frame for 400 ms before handing it to an inactive workspace.
The read-only observer checks at most four pending tokens per one-second budget.
The coordinator invokes a finite child no more often than every two seconds.
Observation never reopens applications, changes focus or resubmits moves.
For reuse-only Chrome windows whose content is still loading, an operation-scoped
continuation represents a move that has not been submitted yet. The backend
checks the complete original native inventory before submitting that first move;
read-only observers cannot execute it. Its receipt then becomes a normal native
placement token. Finalization retains ownership while this evidence is pending.
Polling ends after terminal startup evidence. At the shared deadline, outstanding
work becomes an explicit failure while desired restoration intent is preserved.

Versioned category markers distinguish attempted, waiting, failed and verified
results. Historical text markers suppress accidental relaunch but do not prove
restoration. Items that are not restored stay pending in every checkpoint
until they are restored or forgotten.
Provider-specific proof and limitations are in [provider contract](provider-contract.md).

## Checkpoint publication

Checkpoint schema 6 has explicit migrations from versions 1–5 and rejects unknown
versions. A release refuses to read or overwrite a snapshot with a newer schema,
so older code never drops semantics it cannot represent. Migration never
rewrites stored timestamps, because bindings compare them exactly. A capture uses one desktop/window snapshot, workspace-name list and
topology signature shared by its providers. Topology is checked again after
capture and immediately before publication. Changed topology rejects publication
and leaves the previous checkpoint available. This is a coherent sampling boundary,
not a claim that applications are frozen during capture.

Successful replacement retains the prior last-good checkpoint and up to eight
complete historical generations. Progress/attempt markers remain separate from
desired saved state. Existing provider preservation rules still protect missing,
ambiguous or incomplete state. Session retention additionally protects these
identities; the session cache cleaner remains opt-in and is not installed by desktop release
deployment.

## Deployment and validation

[Desktop releases](deployment.md) coordinate immutable component versions without
merging the repositories. Installation, activation and host-specific configuration
are separate actions. Import-time stamps and scoped diagnostics distinguish source,
installed and running code. A source checkout is an explicit development mode.
The maintained host recovery action is inert until a specific KMS trial is armed;
working display settings are preserved.

Unit and process integration suites exercise stale publishers, cancellation,
concurrent writes, exact identities, topology changes, deadlines, and failed
companions. The [disposable desktop harness](../tests/integration/README.md)
exercises actual GNOME placement and deferred replay. Virtual displays and
software fixtures do not establish physical GPU/hotplug reliability. No finite
test suite can guarantee the desktop will never stall; these contracts prevent
the reproduced faults from being silently retried or falsely reported as success.
