# Provider evidence and safe retry

`provider_results.py` is the application-independent result boundary. Each item
reports identity, content, and placement separately. A phase is `unknown`,
`waiting`, `verified`, `failed`, or `skipped`; detail and retryability accompany
that state. Attention is explicit. Success requires every phase to be verified
or deliberately skipped, with no outstanding attention. `ProviderCount` retains
integer compatibility and carries `.results`; `ProviderRestoreError` carries the
same per-item evidence. Chrome's existing `BrowserRestoreResult` adds `.evidence`.
No caller should turn request acceptance or a process/window count into success.

GNOME placement acceptance (`accepted`, `deferred`, `applied`) permits observation.
It is not completion. Providers verify workspace, monitor, state, and normal
window geometry. Maximized/fullscreen dimensions belong to the compositor, so
saved rectangles are not compared in those states. Pending requests remain
waiting when the bounded observation deadline expires. Independent saved items
continue after one item fails; failure evidence remains available to the caller.

Chrome capture requires companion 0.5.4's `exact_capture_identity` capability.
The transient `runtime_window_id` binds each captured Chrome window to a private
identification marker and one stable native ID. It is removed before publication.
Titles/geometry never assign durable window identity. Identification restores the
previous tab and does not request desktop focus during capture. An inactive popup
cannot safely use the normal tab marker and fails capture explicitly; the caller
must preserve the previous verified checkpoint. Aggregate capture supplies one
shared shell/topology snapshot and validates it before publication.

Terminal session identity prioritizes a uniquely owned rollout. Multiple owned rollouts are
ambiguous and fail closed. Only the explicit positional UUID in `codex resume
[--no-alt-screen] UUID ...` is accepted as argv fallback; prompt UUIDs and unknown
option grammars are not evidence. A shell command name does not prove an empty
input buffer. Reconciliation never types a resume command into an existing shell;
new owned panes launch the resume wrapper as their process command.

VS Code launch validates the actual selected native-recovery bootstrap profile
and resources. Project/profile identity, remote/storage readiness, saved editor
URI metadata, dirty-count metadata, and native placement are separate observations.
Missing editor or dirty recovery is attention-worthy, even after the project is
reopened. Metadata verification does not claim to inspect or reproduce buffer
contents: native Hot Exit owns those contents. Social app content is similarly
application-owned and explicitly skipped rather than asserted recovered.

The Chrome native host has one request per local client. Client reads, request
waits, writes, native-output writes, connection counts, and output queues are
bounded. All client and native-output writes are nonblocking. Socket cleanup
compares the original filesystem pathname device/inode and preserves a
replacement socket. A stalled peer cannot block other local clients indefinitely.
