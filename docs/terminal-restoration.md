# Terminal restoration

Save captures window names, pane labels, rename policies, active panes and layouts.
Fresh restoration appends panes in saved index order before applying their layout.
Labels belong to a proven pane identity, not whichever process occupies an index.
Swapped, replaced or additional panes are preserved and reported for review.

## Conversation identity

A terminal-owned rollout or an explicit `codex resume UUID` supplies its immutable
conversation identity. A shared background server's files do not identify an
individual terminal. Unknown UUIDs remain visible in the HUD; they cannot count
as verified or enable terminal autosave.

For supported fresh native clients, configure the global CLI before launching:

```toml
[tui]
terminal_title = ["thread-id"]
```

Merge this key into an existing `[tui]` section. It changes terminal titles only.
Use `@pane_label` for human-readable pane borders. Permissions, model selection
and reasoning settings remain inherited from the global configuration.

[CLI configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference)

The adapter requires a live foreground client, composer, explicit launch-time
title configuration, a unique full UUID in the loaded catalog and that exact
thread's creation timestamp. It rechecks process and title stability. `/new`
can then replace an obsolete argv identity without guessing from nearby files.

This is a deliberately narrow adapter, verified with native CLI 0.160.0 and
0.160.1. Changed configuration after launch, project/profile/managed overrides,
`-C`/`--cd`, older picker-resumed threads, missing database metadata, clipped
composers and ambiguous title prefixes remain unsupported. Existing exact UUID
resumption still works; a conflicting native title vetoes stale argv. Unknown
panes require explicit UUID recovery, followed by a deliberate save when ready.
For idle native clients, `wsctl save --verify-idle-codex` can obtain their current
full UUID from a fresh `/status` response. It verifies the process, terminal,
loaded catalog and response again before publishing. It refuses active work or
draft input and does not send model requests. Automatic capture never sends
terminal input.

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
