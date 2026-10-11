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

Chrome may wait for loaded URLs before it is safe to place an original window.
A `chrome-content:` token identifies this content-gated continuation, not a
compositor request. Only the startup backend can submit its first move after
checking exact native identities and the complete URL/group catalog. The receipt
then records a native request token or verified placement. An observer must not
turn verified content into verified placement.

Checkpoint reuse is narrower still: the newer observation must match the full
native profile and window catalog, including exact URL/group content and saved
placement. An overlap, count match, or duplicate with different placement does
not authorize reuse; ambiguous catalogs remain unchanged for review.

For an ordinary full-profile startup, companion 0.5.6 also supports
`preserve_native_fragment_navigation`. This is a placement-only contract for a
complete native catalog whose HTTP(S) URLs differ only after `#`. Every ordered
URL before the fragment, pin, window type, incognito setting, group membership,
group title/color/collapsed state, and profile must match. A matching bucket may
contain duplicates only when all its saved destinations are identical. Paths,
queries, account paths, other URL schemes, partial overlap and residual window
counts never authorize this mode. Retained observations keep their exact rules.

The attempt binds current window, tab and group IDs and rechecks the full catalog
before claiming and immediately before placement. Optional `capture` with
`runtime_identity: true` supplies these transient tab/group IDs; ordinary capture
does not publish them. Existing strict claims cannot become preservation claims.
A changed or missing claimed object is preserved for review, never replaced.

Preservation does not activate lazy tabs, load pages, navigate, create, close or
regroup user tabs. Native placement still uses the existing scoped identification
marker and removes it before checking content and moving the window. A page may
continue changing its fragment naturally while the same native objects remain.
The provider verifies **current native content preserved**, explicitly reports
that saved URL fragments were not restored, and leaves `urls_restored` false.
Lazy tabs may stay lazy. The canonical checkpoint and its saved URLs stay intact;
only a separate successful capture can establish a new baseline.

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
The startup receipt records the actual tmux session name and server start identity.
Verification follows immutable pane IDs and exact conversation identities rather
than reusing saved tab numbers. If all saved identities already exist uniquely in
the intended session, restoration preserves its current layout. Partial or
ambiguous matches are reported without launching duplicates.

Supported fresh native clients can also provide a launch-configured thread-ID
title, uniquely matched to a loaded UUID and its exact creation record. Process,
terminal and title are revalidated; timestamps never select an identity. Unknown
saved panes are counted and named as attention in the HUD. Naming reconciliation
requires a current server/pane identity, exact unique UUID or an owned creation
anchor. Index/count matches alone cannot repaint a replaced pane.
See [terminal restoration](terminal-restoration.md) for configuration and limits.

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
