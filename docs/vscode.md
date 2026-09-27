# VS Code workspaces

`wsctl save` captures VS Code windows through the optional workspace-state
bridge. The `vscode` checkpoint is provider-tagged and contains only validated
window/workspace records; an unavailable bridge is reported as a partial
capture and never replaces an existing good checkpoint.

Use `wsctl restore vscode --dry-run` to validate a checkpoint, or
`wsctl restore vscode` to reconcile it. Startup restoration is idempotent and
storage-backed workspaces are deferred until configured cloud mounts are
available. A missing legacy `vscode` category produces no action.

Shutdown-safe saves fail closed when live Code is present but the bridge cannot
produce a complete record and no previous VS Code checkpoint exists. With a
previous checkpoint, that checkpoint is retained and the shutdown HUD reports
the degraded capture.

## Installation and operation

```
make install-vscode
wsctl restore vscode --dry-run
wsctl restore vscode
```

The narrow installer builds a local VSIX using only Python's standard library
and installs `sagecat.workspace-state-companion` with the official Code CLI.
It updates the default profile and verified existing named profiles; it never
creates profiles, changes workspace/user settings, or restarts an editor. New
installations can activate in an already-open window. If Code requires a reload
after an extension upgrade, reload that window when convenient; wsctl does not
force it. No GNOME extension update is required: HUD rows are data-driven.

`scripts/install-vscode-extension.py` also accepts `--user-data-dir`,
`--extensions-dir`, `--profile`, `--force`, and `--package-only` for explicit
isolated installations. Named profiles must exist in the local registry.

Automatic startup uses the existing bounded category worker pool. Code is
started only when there are saved windows, and existing matching windows are
reused. Native windows can appear before the companion's `onStartupFinished`
activation. Restore waits for all open Code windows to acknowledge readiness
within the existing 30-second category deadline, including already-running
windows; native recovery must then settle before another window is launched.
It no longer declares the companion missing after a fixed eight-second wait.
A short socket response timeout is also retryable inside that full readiness
budget, including the acknowledgement after opening a missing project; it does
not trigger another project launch or bypass the wait while extensions activate.
A companion that never becomes ready fails without launching duplicate windows;
permission, protocol and identity errors still fail immediately. Readiness waits
are shown in the HUD. Folder/display availability failures remain
category-local; other applications and cloud-drive initialization can finish.
Drive-backed projects defer until the post-workspace mount phase to avoid a
startup dependency cycle. Filesystem probes and companion requests are bounded.

## What is saved

Each window records its full folder URIs or `.code-workspace` URI, ordered
workspace folders, local/remote identity, verified Code profile, user-data
directory, and native placement. Native placement uses GNOME workspace names
and physical display identities, plus geometry and normal/maximized/minimized/
fullscreen state. Window IDs and process IDs are runtime-only, not durable keys.

The companion runs in the local UI extension host, including for Remote SSH
projects. It publishes metadata over a private per-window Unix socket, not a
network listener. The client verifies socket permissions, peer UID/PID, protocol
version, and window instance. It does not collect document contents, passwords,
authentication tokens, or editor databases.

To identify a window precisely, the companion briefly opens a read-only,
script-disabled webview with a random title. The adapter matches that unique
title to a GNOME window, then disposes only that temporary panel. Focus is
preserved; a three-second lease also cleans up after an interrupted client.
Custom `window.title` settings that omit the active editor can hide the marker;
this produces an explicit identification failure, never title/PID guessing.
All identification/placement sequences share the compositor gate with the other
parallel restore categories. After placement, the adapter rechecks the project,
workspace, display and native state before reporting Ready.

VS Code owns its editor layout, tabs, extensions and Hot Exit buffer backups.
wsctl never runs Save All, overwrites unsaved edits, rewrites Code's databases,
or claims that a fresh blank window recovered missing data. Unsaved edits with
disabled or unknown Hot Exit recovery block the shutdown checkpoint even when
a previous project recipe exists. Remote projects must also acknowledge actual
filesystem availability; authentication/trust prompts are never bypassed.

Empty editor windows with files/unsaved buffers and unsaved multi-folder
workspaces use native Code session recovery. If their exact identity is not
recovered, the job reports failure rather than synthesizing a different empty
window or workspace. Deleted projects, missing/renamed profiles, ambiguous
profile identities and unavailable saved displays likewise remain visible.
The current adapter targets the installed stable `code` application, not
Insiders, VSCodium, or an editor running inside a Windows VM.

## Verification

`make test` includes adapter, capture/save/startup wiring, socket/extension and
VSIX packaging tests. Node protocol checks use a private temporary runtime.

`scripts/test-vscode-live.py --root /tmp/wsctl-vscode-live.<fixture>` is an opt-in
live harness: first launch disposable Code project windows using the fixture's
separate `data` and `extensions` directories. It limits ownership by both the
native process's exact user-data argument and the companion's user-data record.
It tests saved placement and a repeated restore that must not launch duplicates.
`--restore-saved` replays the fixture recipe after a separate test process exit;
`--minimize-first` also exercises minimized inactive-workspace placement.
The harness never starts/stops the OS or writes the production checkpoint.

Live verification on 2026-09-15 used three disposable windows: two different
folders both named `demo`, and a saved multi-folder workspace. Capture, placement
across physical displays/workspaces, normal/maximized/minimized states, process
exit/reopen and repeated duplicate-free restoration passed. Existing production
windows stayed open. This was not a full OS shutdown/reboot test, nor a live
Remote SSH/unsaved-buffer recovery test.
