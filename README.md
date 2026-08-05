# workspace-state

`wsctl` saves and restores one native GNOME Wayland desktop state. There are no
named profiles: the canonical private recipe is always
`~/.local/share/workspace-state/snapshots/current.json`.

The state currently has two application categories:

- `terminals`: Alacritty windows, tmux sessions/windows/panes/layouts, and exact
  terminal application conversation UUIDs;
- `browsers`: Google Chrome windows, tabs, pinned tabs, groups, active tabs, and
  desktop placement.

GNOME placement is applied with a Shell extension and Mutter's native Wayland
window objects. Displays are matched by EDID hash, serial, connector/model, then
the current primary monitor. Logical workspace names survive workspace reorder.

## Install

```sh
make install
gnome-extensions enable workspace-state@sagecat.local
```

Log out and back in after installing or changing the GNOME extension; GNOME
Shell caches extension modules for the current session. Keep the session on
Wayland.

Then install the Chrome companion:

1. Open `chrome://extensions` and enable **Developer mode**.
2. Choose **Load unpacked** and select
   `~/.local/share/workspace-state/chrome-extension`.
3. Open the extension options and keep the profile label unique. The supported
   browser app ID is currently `google-chrome`.
4. Restart Chrome once so its service worker connects to the native host.

The fixed unpacked-extension ID is `gnccboicpdhhhpdcogeleiegokieocmn`.

`make install` also installs the Alacritty startup wrappers. Both the desktop
launcher and the GNOME terminal shortcut can claim the first terminal launch of
a boot. That first Alacritty attaches to `main`; after tmux-continuum finishes,
wsctl places that existing window and starts/places any missing saved windows.
Later terminal launches behave normally.

## Commands

```sh
wsctl save                 # replace the one saved state
wsctl show --details       # inspect it
wsctl restore              # terminals and browsers
wsctl restore terminals    # terminals only
wsctl restore browsers     # Google Chrome only
wsctl restore --dry-run    # do not change the desktop
wsctl startup              # restore once per boot, starting missing apps
```

`restore` also supports `--workspace NAME`, repeatable `--session NAME`,
`--select`, and `--no-place`. `startup` uses markers under
`$XDG_RUNTIME_DIR/workspace-state/startup-<boot-id>/`, so the Alacritty launcher
and tmux hook can safely trigger it together. `startup --force` deliberately
runs it again during the same boot.

`save` refuses to replace the recipe if the GNOME or Chrome companion is
unavailable, a window lacks placement, or a live terminal application UUID cannot be resolved.
Use `--allow-partial` only when incomplete state is intentional. Continuum's
tmux hook can still refresh terminal state while Chrome is closed; in that case
it preserves the last saved browser category.

## tmux-resurrect and continuum contract

The installed tmux configuration uses these hooks:

```tmux
set -g @resurrect-processes '"wsctl-codex->codex resume --no-alt-screen *"'
set -g @resurrect-hook-post-save-layout '~/.local/bin/wsctl tmux save'
set -g @resurrect-hook-pre-restore-all '~/.local/bin/wsctl tmux begin'
set -g @resurrect-hook-post-restore-all '~/.local/bin/wsctl tmux restore'
set -g @continuum-restore 'on'
set -g @continuum-save-interval '15'
```

The post-save-layout hook receives the resurrect state-file path as its final
argument. `wsctl tmux save FILE` maps each pane to its live rollout UUID and
stores a compact `wsctl-codex UUID` command. Resurrect's documented `->`/`*` expansion
turns that into `codex resume --no-alt-screen UUID`. If a terminal application identity is not
provable, wsctl deliberately leaves that process unrestorable instead of
starting an unrelated new conversation.

The same hook autosaves the terminal category every 15 minutes. Continuum
restores tmux at boot, including the contracted terminal application commands, and resurrect's
post-restore hook calls `wsctl startup` to restore application windows and
Wayland placement. The Alacritty trigger is a fallback and shares the same
per-boot lock. A pre/post-restore marker also keeps the delayed launcher fallback
from touching tmux while resurrect is still rebuilding a large layout.

Automatic saves are armed only after terminals have been restored successfully
for the current boot, or after an explicit `wsctl save`. Until then, the save
hook makes continuum retain the previous `last` state. This prevents the first
partial login state from overwriting the recipe that is still needed for
recovery.

## Browser and desktop integration

Chrome communicates through a native-messaging host and a private Unix socket
under `$XDG_RUNTIME_DIR/workspace-state/`; wsctl does not parse Chrome's private
session files. At startup it uses `google-chrome --no-startup-window` when the
saved Chrome profile companion is not connected, then restores windows one at a
time so GNOME can place each otherwise indistinguishable native window.

The Shell companion exports capture, expectation/status, stable-window,
PID-based terminal, and title fallback placement methods at
`org.sagecat.WorkspaceState`.

## Development

```sh
make test
PYTHONPATH=src python3 -m workspace_state --help
node --check chrome-extension/service-worker.js
node --input-type=module --check < gnome-extension/workspace-state@sagecat.local/extension.js
```
