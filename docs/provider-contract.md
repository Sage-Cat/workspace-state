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

For an ordinary full-profile startup, companion 0.5.6 and later also support
`preserve_native_fragment_navigation`. This is a placement-only contract for a
complete native catalog: Chrome owns tabs and groups, so each native window keeps
its current content and moves to the destination of the saved window it pairs
with. Windows whose HTTP(S) URLs differ only after `#` pair by their ordered
navigation; such a bucket may contain duplicates only when all its saved
destinations are identical. Otherwise identical navigation with one saved
destination pairs first, and then a saved and a native window pair when each
gives the other the strictly largest shared multiset of tab URLs (HTTP(S)
fragments ignored), recomputed after every pair. A tie pairs only interchangeable
native windows bound for one destination. The last window left on each side
pairs by elimination. Window type, incognito setting and profile must match.
Windows left ambiguous or unmatched stay unpaired: they leave the attempt
catalog, are listed in the receipt's `unpaired_windows`, cause no native call
and fail only their own item, while paired windows restore and verify as usual.
Unequal window counts and a profile where no window pairs use the strict
restore. Identical native windows bound for different destinations never
qualify. Retained observations keep their exact rules.

After pairing, the attempt binds each item only to its native window ID, which
Chrome never reuses within a browser session, plus the window `type`,
`incognito` flag and profile identity. Tab URLs, tab and group IDs, tab counts
and the total window inventory are not compared, so pages that load, redirect or
open tabs during the restore, and new native windows, do not block placement
(companion 0.5.8). A missing (or type/incognito-changed) paired window fails
only its own item, and final verification marks only that item's evidence as
failed, so the category is classified per item. A retry in the same Chrome
session reuses an earlier preservation claim when its restore token (derived from
the saved recipe) is unchanged, checking only window ID, kind and saved
destination; tab and group content or runtime IDs recorded by an earlier run
never conflict with a later pairing. A claim under a different token on the same
window still requires review. A retry in which an unpaired window already holds
such a claim uses the strict restore, which reuses it. Optional `capture` with `runtime_identity: true` supplies
transient tab/group IDs for the pairing; ordinary capture does not publish
them. Existing strict claims cannot become preservation claims.
A changed or missing claimed object is preserved for review, never replaced.

Preservation does not activate lazy tabs, load pages, navigate, create, close or
regroup user tabs. Native placement still uses the existing scoped identification
marker and removes it before checking content and moving the window. A page may
continue changing its fragment naturally while the same native objects remain.
The provider verifies **current native content preserved**, explicitly reports
that saved tab URLs were not restored, and leaves `urls_restored` false.
Lazy tabs may stay lazy. The canonical checkpoint and its saved URLs stay intact;
only a separate successful capture can establish a new baseline.

Chrome capture requires companion 0.5.4's `exact_capture_identity` capability.
The transient `runtime_window_id` binds each captured Chrome window to a private
identification marker and one stable native ID. It is removed before publication.
Geometry never assigns window identity. Identification restores the previous
tab and does not request desktop focus during capture. An inactive popup cannot
safely use the normal tab marker and fails capture explicitly; the caller must
preserve the previous verified checkpoint. Aggregate capture supplies one shared
shell/topology snapshot and validates it before publication.

The periodic checkpoint uses a read-only variant without markers, and only
after this login's Chrome restore verified every item or a complete manual save
accepted the live windows; until then the saved recipe is kept, so windows that
were not restored are never dropped. Each window's
active tab title must name native Chrome windows, titled
`<title> - Google Chrome` (a note such as a profile or media state may precede
the suffix), and every native Chrome window must be paired with one companion
window. The pairing is a bipartite matching solved in polynomial time with no
search limit: a window is decided only when every complete pairing gives it the
same native window (or natives with the same workspace, display, state and
frame). Among windows that stay ambiguous, state, size (within 8 px) and focus,
which the companion reports without touching tabs, focus or pages, decide only
when one signal set leaves a complete pairing in which every window is decided
(one native window, or natives with the same placement) and every ambiguous
window's pair agrees with all of that set's signals; interchangeable windows
elsewhere do not stop a signal set from deciding. A size that fits nobody (for example
because of frame shadows) therefore decides nothing. The Chrome-reported size
against Mutter's frame rectangle has not been measured yet; until it is, size
rarely decides.

A window that stays ambiguous is saved with its tabs and `placement_ambiguous`.
It keeps a last known placement only when all of these hold: its tab URLs name
exactly one window in this capture and one in the previous recipe of the same
profile; that previous window's placement (observed, or itself carried earlier) is the
placement of one of the native windows this window could be; and the
carries of every ambiguous window in the profile give each its own native window.
A carried placement is a heuristic: it recognises a window by its tab URLs, so
two windows with different tabs and the same title that swap workspaces between
two captures are carried to each other's old workspace until a capture tells them
apart. Otherwise nothing is carried and the window has no placement. The other windows
are captured normally and the capture is published as `captured`, so ambiguity
never blocks later captures. At restore, an unplaced window is opened with its
tabs; its placement item is `skipped` with advisory attention ("placement
unknown"). Two compositor samples around a fresh companion window list, including
the signals, must agree. A title that matches no window, windows that cannot be
paired one to one, or a change between the samples fails the capture, and the
previous Chrome recipe is retained.

Advisory attention (text starting `Advisory: `) follows one rule everywhere: item
success, `waiting_only`, the recreate-window check, the stage state, the restore
marker (including its re-publication when startup is entered again) and the HUD
counters. It never makes an item unsuccessful and never counts as an error, so a real error
or failed item is neither hidden by it nor changed by it. A category whose items
are otherwise all restored is shown as `degraded` in the status and HUD, never
`failed`, and its restore marker is `ready`, so later captures stay open. The age note is never an item and never counts as a restored one, so windows that
all failed stay `failed` beside it. Items with advisory attention count as
restored in the HUD counters of a degraded stage (not of a failed one) and as needing
attention in the message; the status hint to run `wsctl save` appears only for
real problems.

A retained Chrome recipe whose last good capture was more than 30 minutes (six
periodic checkpoints) older than the moment the snapshot was published is
reported at restore. The lag is measured at publication, so a night between a
logout and the next login does not count. The browsers stage gets an item
`recipe-age` with advisory attention, so the status and HUD show `degraded`; the
item is not counted among the stage's items, and it is added even when a window
failed. The first successful periodic capture replaces the recipe and the note
disappears. `wsctl show` prints a `STALE` line.

Social apps installed as snaps (launcher `<snap>_<app>` exported by snapd) wait,
within the restore deadline and at most 15 s, for unfinished snapd changes of
that snap (`/v2/changes?select=in-progress&for=<snap>`) before launching. If the
launch shows no window of this launch (for a workspace switch, none in its own
process tree) within 2 s and such a change began meanwhile, it is repeated once
after the change ends. A visible window is never launched again. Steam windows
record their title (`steam_title`); restore moves only a window whose title and
frame still match the saved one, and reports `verified` only after observing the
saved placement. A recipe saved before `steam_title` existed keeps the earlier
frame-only rules, so it can still match a window by size alone until the next
save records the titles.

Schema version 7 is unchanged: `placement_ambiguous` and `steam_title` are
optional extra fields. A release without them ignores `steam_title`, and treats a
window that has `placement_ambiguous` and no placement as one with no saved
placement and does not restore it ("not restored: no desktop placement was
saved"); its own next capture replaces both fields.

Agent Deck supplies conversation identity through its private registry. Exact
restore pins the profile, record ID, native UUID, tool, account and group. The
CLI must atomically validate those fields before launching. CLI acceptance starts
verification. A matching positive observation binds the record to a live native
foreground client on the correct tmux socket and pane. An idle status, registry
record or dashboard alone cannot verify a conversation. The startup continuation
can retry only exact bindings still pending in the original startup operation.

For installations without Agent Deck, terminal session identity prioritizes a uniquely owned rollout. Multiple owned rollouts are
ambiguous and fail closed. Only the explicit positional UUID in `codex resume
[--no-alt-screen] UUID ...` is accepted as argv fallback; prompt UUIDs and unknown
option grammars are not evidence. A shell command name does not prove an empty
input buffer. Reconciliation never types a resume command into an existing shell;
new owned panes launch the resume wrapper as their process command.
Claude uses the exact UUID in a `SessionStart` hook receipt bound to the live
foreground client and tmux pane. `SessionEnd` retires that receipt. A transcript
file proves durable storage, but its path or modification time does not assign a
conversation to a pane. `claude --resume UUID` restores that exact conversation;
`--continue`, names and titles cannot replace the saved UUID.
The startup receipt records the actual tmux session name and server start identity.
Verification follows immutable pane IDs and provider-qualified conversation identities rather
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
