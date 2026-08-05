# workspace-state

`wsctl` stores reusable desktop recipes for native GNOME Wayland sessions. It
coordinates three components instead of asking Chrome or an X11 window tool to
do work they do not control:

- the CLI captures recipes and orchestrates restoration;
- a Chrome extension captures browser windows, ordered tabs, the active tab,
  pinned tabs, tab groups, and window state with supported Chrome APIs;
- a GNOME Shell extension captures and applies workspace, monitor, geometry,
  maximized, and fullscreen state with Mutter's native window objects.

Terminal recipes continue to include Alacritty placement, tmux sessions,
windows, panes and layouts, plus active terminal application session IDs. Snapshot files are
human-readable JSON under `~/.local/share/workspace-state/snapshots/` and are
created with mode `0600` because they can contain private URLs.

## Install

```sh
make install
gnome-extensions enable workspace-state@sagecat.local
```

GNOME must discover a newly installed Shell extension at login, so log out and
back in once if `gnome-extensions enable` says the extension is unknown. Do not
switch the session away from Wayland.

Then install the unpacked Chrome extension:

1. Open `chrome://extensions` and enable **Developer mode**.
2. Choose **Load unpacked** and select
   `~/.local/share/workspace-state/chrome-extension`.
3. Open the extension's **Options**. Give each enabled Chrome profile a unique
   profile name. Keep `google-chrome` as the desktop app ID unless the installed
   browser uses another ID, such as `chromium` or `google-chrome-beta`.

The manifest contains a fixed public key, so the unpacked extension ID remains
`gnccboicpdhhhpdcogeleiegokieocmn`. `make install` installs a native-messaging
manifest for Google Chrome, Chrome Beta, and Chromium with that exact origin.
Restart Chrome after the first installation so its service worker connects to
the native host.

## Browser and whole-desktop workflow

The short forms use the target as the snapshot name:

```sh
wsctl snapshot browser       # saves snapshot "browser"
wsctl restore browser        # restores Chrome from snapshot "browser"
wsctl snapshot all           # saves snapshot "all"
wsctl restore all            # restores terminals and Chrome from "all"
```

An optional name keeps several recipes:

```sh
wsctl snapshot browser research
wsctl restore browser research
wsctl snapshot all evening
wsctl restore all evening
```

`wsctl restore evening` also restores every component present in `evening`.
Use `--workspace NAME` to restore one logical GNOME workspace, `--no-place` to
let the applications choose placement, or `--dry-run` to inspect the actions.
Existing Chrome windows and tabs are left alone; restoration creates additional
windows.

The original terminal-only commands remain available:

```sh
wsctl save evening
wsctl restore terminals evening
wsctl restore evening --session 4
wsctl restore evening --select
```

`save` refuses to overwrite a snapshot when terminal placement or a live terminal application
UUID is unresolved. `snapshot browser` and `snapshot all` additionally require
both companions and complete Chrome placement. Use `--allow-partial` only when
an intentionally incomplete snapshot is useful.

Other inspection and lifecycle commands are unchanged:

```sh
wsctl list
wsctl show evening
wsctl show evening --details
wsctl archive evening
wsctl archive evening --undo
```

## How restoration is matched

For every saved browser window, wsctl performs this sequence synchronously:

1. Resolve the logical workspace name against GNOME's current workspace order.
2. Resolve the display by EDID hash, then serial/connector/model, falling back
   to the current primary display if the saved display is absent.
3. Ask GNOME Shell to expect the next window for the saved Chrome app ID.
4. Ask the matching Chrome profile to create exactly one window and restore its
   tabs and groups.
5. Wait for GNOME Shell to report that the new native Wayland window was placed,
   then continue with the next browser window.

Sequential creation avoids trying to distinguish several otherwise identical
Chrome windows after the fact. The Shell companion exports the following D-Bus
operations at `org.sagecat.WorkspaceState`:

- `Capture()` and `ListWindows()`;
- `PlaceNextWindow(...)`, `PlacementStatus(...)`, and `CancelPlacement(...)`;
- `MoveWindow(...)` and the legacy terminal helper `PlaceByTitle(...)`.

Chrome communicates only with the installed native host. The host exposes a
private Unix socket per configured profile under `$XDG_RUNTIME_DIR/workspace-state/`;
wsctl never reads Chrome's undocumented internal session files.

## Snapshot shape

A Chrome profile is stored along with logical desktop placement. Monitor records
carry both the connector and a SHA-256 hash of the display EDID when Linux
exposes it:

```json
{
  "chrome": {
    "profiles": [{
      "profile": "Default",
      "app_id": "google-chrome",
      "windows": [{
        "id": "window-1",
        "workspace": "research",
        "workspace_index": 2,
        "monitor": {
          "connector": "DP-1",
          "edid_hash": "...",
          "index": 1
        },
        "geometry": {"x": 30, "y": 40, "width": 1800, "height": 1000},
        "state": "maximized",
        "tabs": [
          {"url": "https://example.org/", "pinned": true, "active": true, "group": null}
        ],
        "groups": []
      }]
    }]
  }
}
```

Chrome does not expose the operating-system profile directory through these
APIs, so the extension's profile label is user-configured. Incognito windows
are captured only when the extension is enabled for incognito. Chrome may reject
restoration of privileged internal URLs; wsctl reports those as tab warnings and
keeps a new-tab page in their place.

## Development

```sh
make test
PYTHONPATH=src python3 -m workspace_state --help
node --check chrome-extension/service-worker.js
node --input-type=module --check < gnome-extension/workspace-state@sagecat.local/extension.js
```
