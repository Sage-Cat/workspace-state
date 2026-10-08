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
revive an old callback. Startup has a 25-minute total budget; shutdown retains
the existing two-hour managed-profile budget. Local timeouts can only shorten
these budgets. Recovery is allowed after the work deadline; commit is not.

`current-operation.json` is the authoritative owner receipt. HUD status is an
atomic presentation/progress document. Both use the same publication lock.
A publisher must match the exact context and owner receipt before updating
status. Damaged startup presentation may be reconstructed for its existing owner;
damaged shutdown presentation cannot turn into a new startup operation. An
unbound compatibility publisher may adopt startup once, but cannot adopt a
shutdown. New managed workers bind before work. Legacy HUD receipts never
authorize a contextual shutdown.

## Entering and leaving shutdown

1. A fresh private Shell request records user intent, not authorization.
2. The coordinator suspends startup for this login and stops/joins finite startup
   worker units, then observes completion of accepted Chrome mutations and
   identity handshakes. A lost client is not proof its companion finished work.
   Restored applications live in separate units and remain open.
3. Only after that barrier does it capture profile preflight state and initialize
   the shutdown operation. Checkpointing and profile preparation run in its
   managed service, with the existing compensation journal.
4. Worker completion, HUD paint and final countdown receipts must each match the
   complete current context. Only this sequence can authorize final handoff.
5. Cancellation immediately withdraws commit authorization. The HUD releases its
   own modal grab locally; backend delivery or rollback latency cannot retain it.
6. The coordinator retains exclusive ownership while stopping workers and
   compensating prepared jobs. A failed rollback remains `recovery-failed` and
   blocks another attempt. Ownership is released only when the journal is clear.

A coordinator restart reattaches to the current login's shutdown journal and
operation instead of replaying startup. A cancelled shutdown does not silently
restart partly completed applications. Recovery and a later explicit repair are
separate decisions. See [startup ownership](startup-ownership.md) for generation
adoption, worker watchdogs and finalizer failure receipts.

A failed HUD stage does not release shutdown ownership: the old worker must
settle and its rollback journal must be clear before another attempt. Journal
deletion is bound to the same operation as journal publication. Native
cancellation also rejects a request which has not yet been adopted.

There is no helper-release integrity gate before capture and no separate
integrity HUD stage. Capture status remains running until the worker exits
successfully. Current transient application ownership, cancellation and
recovery evidence still govern application stops and the native shutdown handoff.

Before application drain, worker completion binds a private checkpoint bundle
containing the canonical workspace recipe and exact tmux-resurrect input.
Cancellation removes shutdown authorization but keeps this recovery evidence.
Automatic saves cannot replace closed applications with empty recipes.

A retry uses fresh operation and countdown receipts. Reusing the pre-close
checkpoint requires the same boot/login, settled stop jobs, unchanged inputs,
and no newer application or terminal state. Divergent or ambiguous state is
preserved and reported for explicit recovery. A successful manual full save
accepts the user's new baseline and clears the old protection; it is refused
while application stop jobs remain pending.
Surviving editor windows with dirty buffers cannot prove unchanged content from
an unchanged dirty-buffer count; automatic reuse must refuse that uncertainty.

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
restoration. Waiting/failed markers cannot arm automatic checkpoint replacement.
Provider-specific proof and limitations are in [provider contract](provider-contract.md).

## Checkpoint publication

Checkpoint schema 5 has explicit migrations from versions 1–4 and rejects unknown
versions. A capture uses one desktop/window snapshot, workspace-name list and
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
