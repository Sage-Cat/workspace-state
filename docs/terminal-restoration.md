# Terminal restoration

Save captures window names, pane labels, rename policies, active panes and layouts.
Fresh restoration appends panes in saved index order before applying their layout.
Labels belong to a proven pane identity, not whichever process occupies an index.
Swapped, replaced or additional panes are preserved and reported for review.

When a partial live layout is already present, restoration appends only saved
conversations with a uniquely verified identity that are still missing. It
does not rewrite existing live panes to make an ambiguous or incomplete match.

An ambiguous saved shell label no longer aborts restoration of every terminal
and conversation. That pane's label and its window's layout and active selection
stay unchanged; independently proven panes and windows can continue restoring.
Skipped identities do not receive fresh ownership anchors, including on a
second attempt. Additional live panes and unverified process replacement still
refuse mutation, so a naming conflict cannot authorize deleting user work.

## Canonical checkpoint and tmux-resurrect

The workspace snapshot owns the saved terminal inventory and conversation UUIDs.
At shutdown, its capture finishes before tmux-resurrect saves the matching native
layout. An authorized shutdown can refresh that artifact even before periodic
autosave is armed. It waits for an in-flight save and a fresh timestamp instead
of silently retaining an older file. Periodic autosave keeps its normal guards.

Before login runs tmux-resurrect, it compares the artifact's sessions, windows,
panes, working directories and Codex UUIDs with the canonical snapshot. Missing
or conflicting evidence skips native restoration and leaves the existing
canonical startup path to rebuild the saved workspace. It does not launch old
tabs first or close existing live panes to make them match.
An existing session name alone cannot prove completion: the saved Codex UUIDs
must actually be running. An unproven bootstrap shell stays untouched while
canonical restoration creates independent panes or an available session name.

A live Codex pane without a proven UUID cannot borrow the saved UUID merely
because its window name, indices and working directory match. Those coordinates
can be reused by a new conversation. Explicit historical artifact contraction
remains separate from live capture.

## Conversation identity

A terminal-owned rollout or an explicit `codex resume UUID` supplies its immutable
conversation identity. A shared background server's files do not identify an
individual terminal. Unknown UUIDs remain visible in the HUD; they cannot count
as verified or enable terminal autosave.

For supported fresh native clients, configure the global CLI:

```toml
[tui]
terminal_title = ["thread-id"]
```

Merge this key into an existing `[tui]` section. It changes terminal titles only.
Use `@pane_label` for human-readable pane borders. Permissions, model selection
and reasoning settings remain inherited from the global configuration.

[CLI configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference)

The adapter requires a live foreground client, composer, explicit thread-ID
title mode, a unique full UUID in the loaded catalog and that exact thread's
creation timestamp. It rechecks process and title stability. `/new`
can then replace an obsolete argv identity without guessing from nearby files.
Unrelated global configuration edits and project settings do not invalidate a
native title. Project title/profile overrides still refuse identification.

This is a deliberately narrow adapter, verified with native CLI 0.160.0 and
0.160.1. Project title overrides, profile/managed overrides,
`-C`/`--cd`, older picker-resumed threads, missing database metadata, clipped
composers and ambiguous title prefixes remain unsupported. Existing exact UUID
resumption still works; a conflicting native title vetoes stale argv. Unknown
panes require explicit UUID recovery, followed by a deliberate save when ready.
For idle native clients, `wsctl save --verify-idle-codex` can obtain their current
full UUID from a fresh `/status` response. It verifies the process, terminal,
loaded catalog and response again before publishing. It refuses active work or
draft input and does not send model requests. Automatic capture never sends
terminal input.

A verified manual status view can be reused by later captures in the same boot
and login. The private runtime record contains identity and hashes, not account
details or terminal text. Reuse requires the exact unchanged foreground process
chain, arguments, terminal, pane, title, screen, cursor and dimensions. New
output, draft input, `/new`, resizing or a changed owner invalidates it. This
also supplies exact identity to tmux-resurrect and checkpoint sealing.

Repeated status reports can fill an alternate-screen viewport and become
byte-identical. The helper refuses that case. An explicitly authorized manual
operator may temporarily increase a pane's height, collect a fresh report and
restore its exact height. Rebinding then requires the complete unchanged final
report and an exact suffix crop of the original transcript. Automatic capture
does not resize panes or renew changed views.

## Checks

```sh
make check
python3 scripts/test-tmux-names-live.py
python3 scripts/test-codex-title-live.py --codex /path/to/native/codex
```

Both native checks use private unattached tmux sockets. The title check also uses
a temporary CLI home and a loopback provider that intentionally rejects synthetic
requests. It calls no model or account service. It proves real CLI rendering,
thread capture and `/new` binding, not authenticated content recovery.

For complete guest shutdown and boot testing, see
[the testing record](testing.md) and `tests/integration/run_vm_tmux_names.py`.
