# Terminal restoration

Save captures window names, pane labels, rename policies, active panes and layouts.
Fresh restoration appends panes in saved index order before applying their layout.
Labels belong to a proven pane identity, not whichever process occupies an index.
Swapped, replaced or additional panes are preserved and reported for review.

## Agent Deck

With an Agent Deck installation configured, the terminal checkpoint stores the
outer terminal layout and Agent Deck's conversation inventory separately. For
each visible dashboard, it records the profile and group filter from the actual
running Agent Deck process. For conversations, it records the Agent Deck record
ID, native UUID, provider, account label, group and whether the client is open.
The checkpoint is private and references the original Agent Deck registry and
provider transcripts. Keep those files in their existing locations across login.

Install an Agent Deck build whose `session start --help` includes
`--expected-native-id`, `--expected-native-tool`, `--expected-account` and
`--expected-group`. Put that build on the desktop session's `PATH`. The guarded
command uses the account configuration already bound to the Agent Deck record.
It leaves archived, explicitly stopped, changed and ambiguous records alone.
The workspace HUD verifies a live native foreground client for the exact record.
A failed launch or an unavailable transcript remains pending. For a filtered
workspace or terminal restore, only groups covered by its saved dashboards are
eligible. A dashboard that cannot start leaves a usable shell and reports the
missing dashboard.

For migration from native terminal tabs, import the exact existing conversation
UUIDs into Agent Deck and open the desired dashboards before the first capture.
Capture them with the new wsctl while the original dashboard processes are
still running. A checkpoint written by an older release does not identify
dashboard filters. An older wsctl preserves a checkpoint with the new schema.
`wsctl show --details` reports retained identities. When restoring an older
checkpoint, an exact UUID can be mapped to a unique existing Agent Deck record.
A saved terminal without a known dashboard scope is restored as a shell.

Once a checkpoint has captured the Agent Deck inventory and at least one
dashboard, startup keeps unmatched pending native UUIDs for manual migration.
After a successful live inventory confirms that a pending UUID is absent from
both registries, the startup summary reports it separately from conversations
expected to reopen. Its original saved recipe remains visible in
`wsctl show --details`. An unavailable inventory or a missing, changed or
unverified open Agent Deck record still needs attention.

To leave standalone native conversations for separate recovery, set the
following in `${XDG_CONFIG_HOME:-$HOME/.config}/workspace-state/agent-deck.toml`
before the next restore:

```toml
registered_only = true
```

For a verified checkpoint with a captured Agent Deck inventory and dashboard,
this also keeps previously open native UUIDs absent from both the saved and
live registries out of Agent Deck restoration. Their full saved recipes remain
available through `wsctl show --json`. Remove the setting or set it to
`false` to return to the default behavior.

A checkpoint keeps the dashboard scope of a tab from capture to capture. When a
capture cannot read the scope of a tab (an older release saved it, or the read
failed), the scope saved earlier for the same session, window index, pane index
and window name is kept while that tab runs no conversation, and the save
reports the tab. A dashboard that was seen live during this login and is gone was closed by
the user; its scope is not kept, and a kept scope never counts as seen. A dashboard starts in the home directory, whatever directory
the tab last had. A saved dashboard tab that is a shell after restore, or whose
dashboard exited, is an `attention` item named `session:window`; the terminal
stage is `degraded` (or `failed` when nothing else verified), never `ready`.
An open Agent Deck record without a native conversation ID is never resumed,
counted as restored or verified; it stays pending and is reported for
attention, also with `registered_only`.

For a legacy terminal with a confirmed profile and group, open the dashboard in
its already attached terminal with `wsctl restore terminals`:

```sh
wsctl restore terminals --workspace Example --session example \
  --deck-dashboard work project --deck-placeholder 'Old Deck'
```

Pass an empty group argument for an unfiltered dashboard. The dashboard goes to
its exact saved tab and is never created next to it:

- When `--deck-placeholder` names an empty shell in a background workspace, that
  pane becomes the dashboard in place (`respawn-pane`): its window index, name
  and neighbours stay, and the dashboard runs under a shell so its exit keeps
  the tab. Nothing is created or killed, so `renumber-windows` cannot shift the
  other tabs.
- Otherwise a new window (dashboard under a shell, so its exit keeps the tab)
  takes the saved index, or the first index after the last tab when something
  holds it; the result then reports the actual and the saved index as attention.
  The respawn is guarded inside tmux (`if-shell -F`): a shell that changed after
  the check is not killed. Gaps in the index sequence stay. Existing
  tabs and their input are preserved.
- A window name that another tab already uses is not an error: tmux windows are
  addressed by index, so both tabs keep the name and the result says so.

The next checkpoint captures the verified dashboard normally.

## Legacy native terminal restoration

This section describes the compatibility path for workspaces without an Agent
Deck installation configured. Terminal labels and layout reconciliation apply
to both paths.

When a partial live layout is already present, restoration appends only saved
conversations with a uniquely verified identity that are still missing. It
does not rewrite existing live panes to make an ambiguous or incomplete match.

An ambiguous saved shell label no longer aborts restoration of every terminal
and conversation. That pane's label and its window's layout and active selection
stay unchanged; independently proven panes and windows can continue restoring.
Skipped identities do not receive fresh ownership anchors, including on a
second attempt. Additional live panes and unverified process replacement still
refuse mutation, so a naming conflict cannot authorize deleting user work.

## Canonical checkpoint and tmux ownership

The workspace snapshot owns the saved terminal inventory and conversation UUIDs,
and wsctl alone owns the tmux layout: tmux-resurrect and tmux-continuum neither
restore nor save anything. Login restore rebuilds each saved session from the
snapshot, and periodic checkpoints keep the snapshot current. An older tmux
configuration that still calls `wsctl-continuum-restore` or `wsctl tmux
begin|restore|end` gets a harmless no-op; `wsctl-continuum-save` and `wsctl tmux
save` run only the terminal checkpoint of `save --auto`.

When the first session of a login needs a new tmux server, wsctl starts it in a
transient scope of its own (`systemd-run --user --scope`, unit
`wsctl-tmux-server-<suffix>`, slice `app.slice`) with the user manager's
environment, minus every variable that binds a process to a wsctl operation. The
server therefore outlives the restore worker and the session agent and passes no
startup authority to its panes. Only without `systemd-run` does the server start
directly, with a diagnostic. When the scope fails, wsctl never starts a server in
its own unit: it joins a server that a terminal started meanwhile and accepts an
existing session of the saved name only if it carries the nonce set by its own
`new-session` invocation. A terminal opened at that moment is not harmless
otherwise: until `terminals.done` exists, the Alacritty wrappers give it a
temporary workspace-scoped session and never create or attach `main`. A
temporary session never reuses the name of a saved tmux session, so restore can
still recreate every saved session; if the saved names cannot be read, the
temporary name carries a per-launch suffix. The wrapper addresses its session
only by exact name, so closing it never touches a restored session whose name
merely starts with the same text.

An existing session name alone cannot prove completion: the saved conversation
UUIDs must actually be running. An existing shell stays untouched while restore
creates independent panes. A live session that has the saved
name but a layout no reconcile can use is never copied: its saved items get a
failed result and stay pending. A conversation that already runs elsewhere is
never started twice. A session that cannot be restored no longer stops the others.
Running restore again in the same login creates nothing twice.

A live Codex or Claude pane without a proven UUID cannot borrow the saved UUID merely
because its window name, indices and working directory match. Those coordinates
can be reused by a new conversation. Explicit historical artifact contraction
remains separate from live capture.

### Conversation identity

Codex and Claude identities are kept separately as `(provider, UUID)`. A new
capture replaces the older identity in the same pane; closed conversations do
not return from an earlier checkpoint. Both clients resume by exact UUID.

#### One client per conversation

Each exact `(provider, UUID)` runs in at most one tmux pane.

- Save keeps one pane per conversation: the pane whose client started first.
  Later panes are saved as plain shells, and the save prints a warning.
- Restore first reads a live index of every tmux pane. A conversation that
  already runs in another session gets its saved pane back as a plain shell.
  A partly running session gets only the conversations that run nowhere.
- The index uses cheap evidence: an exact resume command, an open rollout, a
  waiting launcher, a Claude hook receipt, or the native Codex title prefix.
  When the title names another thread than the resume command, the client
  moved on or the title is stale, so both threads refuse a second client.
  Title evidence never verifies a restored conversation.
- Each resume launcher claims its conversation in the runtime directory and
  checks the live index again. A second launcher prints the reason and leaves
  its pane as a shell. The claim names the launcher's process ID and start
  time: Claude keeps it through `exec`, and the Codex supervisor holds it for
  its lifetime. Codex checks the index once more right before it starts.
- Verification accepts a conversation that runs in a different session.

Claude uses `SessionStart` and `SessionEnd` command hooks to record and retire a
private pane receipt. The receipt binds the hook's UUID to the foreground Claude
process, its start time and terminal. The UUID transcript must exist before the
checkpoint can promise a durable restore. Bare `claude`, terminal titles, recent
files and working directories are insufficient. The managed user settings owner
must register the installed `wsctl-claude-hook` executable for both events:

```json
{
  "hooks": {
    "SessionStart": [{"hooks": [{"type": "command", "command": "<installed wsctl-claude-hook path>", "args": ["start"], "timeout": 5}]}],
    "SessionEnd": [{"hooks": [{"type": "command", "command": "<installed wsctl-claude-hook path>", "args": ["end"], "timeout": 5}]}]
  }
}
```

Merge these entries into the owner-managed Claude settings. Install the matching wsctl
release before registration; keep permissions and model settings unchanged.
Existing Claude processes started before registration remain
unresolved until Claude emits another `SessionStart`. A newly opened conversation
without a valid persisted transcript record is also unresolved, and shutdown reports degraded
capture instead of claiming it can restore that conversation.

[Claude CLI reference](https://code.claude.com/docs/en/cli-reference) and
[hook reference](https://code.claude.com/docs/en/hooks#sessionstart) describe the
exact resume and lifecycle events.

#### Codex identity

A terminal-owned rollout or an explicit `codex resume UUID` supplies its immutable
conversation identity. A shared background server's files do not identify an
individual terminal. Unknown UUIDs remain visible in the HUD; they cannot count
as verified, and a saved conversation they replace stays pending.

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

A visible native composer can span multiple lines: the cursor may be on an
indented continuation below its prompt. Passive title capture recognizes that
layout without changing the draft. An explicit `/status` diagnostic still
requires an empty composer and refuses multiline drafts before sending input.

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
also supplies exact identity to checkpoint sealing.

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

## Tab environment, identity and titles

- Every tmux command that creates a pane (`new-session`, `new-window`,
  `split-window`, `respawn-*`) runs as a client with the server environment
  (`tmux_server.client_environment`): tmux 3.4 gives a pane started by a
  detached client that client's `PATH`, and `-e PATH=...` does not override it.
  Panes, dashboards and the Agent Deck tmux server therefore never inherit a
  release or checkout directory from the wsctl process. The environment is the
  user manager's. Without the manager, the worker's own environment is used
  after every `bin` directory of the launcher and the `~/.local/bin` behind it
  are removed; the user's `~/.local/bin` (existing, not world-writable) is put in front when the
  PATH lacks it, because conversation tabs run `wsctl-*-resume`, `claude` and `codex` from there.
  The manager is read once per 30 seconds, and `TMUX`/`TMUX_TMPDIR` come from the caller only.
  `bin/alacritty-tmux-session` cleans its session's PATH the same way.
- A shell tab that published `@wsctl_tab` (32 lowercase hexadecimal
  characters) is saved as the pane field `tab_id` and restored as
  `env WSCTL_TAB_ID=<id> <shell>`: the pane process is the shell and the ID is
  in its own environment only. It is never set with `new-session -e`, which would
  put it in the session environment for all later tabs. A duplicate ID, which a
  pane started from inside a tab inherits, stays with the pane whose process
  started first. The field is additive, so the checkpoint schema stays at 7;
  an older writer drops it and the shell creates a new ID.
- Alacritty is launched with the session name as its title and
  `window.dynamic_title=true`, so tmux (`set-titles '#S'`) keeps the title
  current. The new window is found by a random receipt in the environment of
  that launch (`workspace_launch_matches`), never by title, then pinned by
  window ID and PID. A saved Alacritty placement compares only ID, PID and
  workspace, so a title change is not a window change.
