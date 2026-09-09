# workspace-state

`wsctl` saves and restores one native GNOME Wayland desktop state. There are no
named snapshot profiles: the canonical private recipe is always
`~/.local/share/workspace-state/snapshots/current.json`.

The restore transaction has three application categories:

- `terminals`: Alacritty windows, tmux sessions/windows/panes/layouts, and exact
  terminal application conversation UUIDs;
- `browsers`: Google Chrome windows, tabs, pinned tabs, groups, active tabs, and
  desktop placement;
- `virtual-machines`: a Windows QEMU VM that was active at the last confirmed
  managed shutdown, including its exact GNOME workspace and physical display.

GNOME placement is delegated to the reusable `gnome-winctl` service, whose
Shell extension uses Mutter's native Wayland window objects. Displays are
matched by EDID hash, serial, connector/model, then the current primary monitor.
Logical workspace names survive workspace reorder.

When a saved physical display is absent, primary-monitor placement is temporary:
the saved display remains the immutable monitor intent, so a later topology
event can return the window when that display becomes uniquely available.

The same extension preserves physical-display intent across lock, wake, and
monitor topology churn. GNOME monitor-move shortcuts, window drags, Overview
drags, and wsctl placements are explicitly authoritative. Other stable-topology
moves are accepted so the policy cooperates with tiling extensions; old anchors
are enforced only during bounded lock/topology recovery. The former standalone
Desktop Window Monitor Guard is disabled during installation and retained only
as an inert rollback copy.

## Install

```sh
make install
```

The sibling `gnome-winctl` and standalone `login-hud` projects are installed
and enabled automatically. Log out and back in after their first installation
or an extension update; GNOME Shell caches extension modules for the current
session. Keep the session on Wayland.

When upgrading from the former embedded wsctl extension, the installer keeps
its already-loaded service active until GNOME recognizes the standalone UUID.
`gnome-winctl` uses that service as a reduced in-session bridge, so inspection
and existing-window placement continue without a disruptive logout. Sequential
next-window reservations become available after the standalone extension loads
at a later normal login.

Branded Google Chrome does not permit command-line installation of unpacked
extensions. Install the companion once in every Chrome profile that wsctl should
manage:

1. Open `chrome://extensions` and enable **Developer mode**.
2. Choose **Load unpacked** and select
   `~/.local/share/workspace-state/chrome-extension`.
3. Open the extension options, keep the profile label unique, and enter its
   Chrome profile directory. The supported browser app ID is `google-chrome`.
4. Restart Chrome once so its service worker connects to the native host. After
   updating the companion source, use **Reload** on `chrome://extensions` (or
   restart Chrome) so the running service worker uses the new protocol. For a
   signed-in profile, the native host can resolve its real Chrome directory (for
   example `Default` or `Profile 1`) automatically; the options remain the
   fallback for unsigned profiles.

The fixed unpacked-extension ID is `gnccboicpdhhhpdcogeleiegokieocmn`.

`make install` also installs the Alacritty startup wrappers. Both the desktop
launcher and the GNOME terminal shortcut can claim the first terminal launch of
a login. That first Alacritty attaches to `main`. If all saved tmux sessions are
already running (for example after working in a TTY), wsctl recognizes them
immediately; otherwise it gives Continuum a bounded start window before using
the canonical fallback. It then places the existing window and starts/places
only missing saved windows. Later terminal launches behave normally.

The enabled `wsctl-gnome-session.service` makes this recovery automatic after a
GNOME login, without waiting for the first manually opened terminal. The unit is
ordered after GNOME Shell's Wayland service and still verifies the compositor,
workspace names, and stable display topology before publishing the first HUD
status or launching restoration. The HUD and wsctl do not configure, restart,
or delay NVIDIA; display readiness is observational only.

The same coordinator registers as a GNOME session client. For ordinary Power
Off and Restart, GNOME first shows its native confirmation dialog. Only after
the user confirms does the HUD defer GNOME's exact final signal and hand
preparation to a separate managed service. GNOME's earlier `QueryEndSession`,
emitted while the native confirmation is open, is passive and cannot start
work. While the graphical session is active, the coordinator holds a logind
`block` inhibitor. It releases that descriptor only after the exact managed
worker, painted HUD, countdown, and private authorization markers have all been
verified. This also prevents an ordinary unprivileged `shutdown now` or
`systemctl poweroff` from bypassing the checkpoint. A privileged forced
shutdown can still override logind inhibitors and must be treated as an
emergency path.

The worker refreshes tmux-resurrect, runs `wsctl save` while Chrome and
Alacritty are still available, and then executes configured shutdown profiles.
An isolated unresolved live terminal application UUID becomes a visible degraded checkpoint
instead of discarding every other current window. If current Chrome capture is
incomplete, shutdown retains the previous verified browser category. Missing
GNOME placement, broken tmux capture, an absent last-good browser category, or a
failed critical profile still fails closed and leaves the inhibitor active. The
worker does not stop GNOME, mounts, cloud drives, NVIDIA, or unrelated user
services. If the Shell extension is unavailable, GNOME's own `EndSession`
request releases the coordinator normally and follows Ubuntu's shutdown path.

The Login HUD appears as always-on-top, non-modal GNOME Shell chrome only once
per OS boot: during the first GNOME login after power-on or restart, and only
after GNOME Shell is active and the coordinator publishes a complete valid
status for that exact GNOME Session Manager instance. Logging out and back in
during the same boot still runs workspace reconciliation in the background but
publishes `show_startup_hud: false`, so no startup HUD chrome is created. The
persistent claim under `~/.local/state/workspace-state/` stores only the kernel
boot ID and claiming GNOME session ID. A coordinator restart in that same first
session retains the visible HUD, while a changed kernel boot ID permits the next
startup HUD.
Until then the extension remains hidden and installs no Shell chrome, stage
listeners, timer, or input grab. `wsctl-gnome-session`, the startup transaction,
and the post-workspace finalizer atomically publish real milestone states and counts to
`$XDG_RUNTIME_DIR/workspace-state/login-hud-status.json`. The private full-login
diagnostic log is `login-hud.log` in the same directory. Successful completion
shows only an **OK** button; any unrecovered failure remains visible with a
**Show full error log** button. Status publication is best-effort and can never
block restoration.

After native confirmation, the same Shell HUD switches to a GNOME system-modal
shutdown mode. It is raised above regular, popup, and fullscreen windows and
takes the full keyboard/pointer grab used by system dialogs. GNOME's exact
original action remains deferred while a managed worker checkpoints
tmux-resurrect, captures the final Alacritty/Chrome desktop recipe, and prepares
each applicable profile. Cloud drives, metadata workers, GNOME, NVIDIA, and
other system components remain untouched for Ubuntu to stop normally after
handoff. The coordinator verifies that exact systemd invocation, waits for the
ready HUD to be physically painted, enforces a visible three-second countdown,
and only then publishes the final authorization marker. The extension calls
GNOME's saved confirmation exactly once. A checkpoint or critical-profile
failure keeps the HUD and full-log button visible, releases the modal grab, and
cancels the pending GNOME action.

**Cancel shutdown** and `Esc` remain available throughout the active modal phase.
The request is bound to the current random shutdown operation ID. One action
cancels GNOME's original dialog exactly once and stops the exact managed worker.
Before every profile mutation, the worker persists private, operation-bound
rollback instructions. The systemd unit's `ExecStopPost` finishes recovery even
if the worker was interrupted or had already exited. The journal is disarmed
only when GNOME emits final `EndSession`; a cancelled or rejected handoff first
restores every prepared job. The HUD reports recovery and becomes terminal only
after the rollback journal is empty.

## Commands

```sh
wsctl save                 # replace the one saved state
wsctl show --details       # inspect it
wsctl restore              # terminals, browsers, and a pending Windows VM
wsctl restore terminals    # terminals only
wsctl restore browsers     # Google Chrome only
wsctl restore virtual-machines  # retry a pending post-shutdown VM restore
wsctl restore --dry-run    # do not change the desktop
wsctl startup              # restore once per login, starting missing apps
wsctl tmux configure       # install the resilient terminal application restore mapping
wsctl shutdown-profiles list --probe
```

`restore` also supports `--workspace NAME`, repeatable `--session NAME`,
`--select`, and `--no-place`. `startup` uses markers under
`$XDG_RUNTIME_DIR/workspace-state/startup-<boot-id>-<login-id>/`, so the Alacritty launcher
and tmux hook can safely trigger it together. `startup --force` deliberately
runs it again during the same login.

`save` refuses to replace the recipe if the GNOME or Chrome companion is
unavailable, a window lacks placement, or a live terminal application UUID cannot be resolved.
Use `--allow-partial` only when incomplete state is intentional. Continuum's
tmux hook can still refresh terminal state while Chrome is closed; in that case
it preserves the last saved browser category.

## Shutdown profiles

Shutdown profiles are TOML files in
`~/.config/workspace-state/shutdown-profiles.d/` (and optionally
`/etc/workspace-state/shutdown-profiles.d/`). Files must be regular, owned by
the current user or root, and not writable by group or others. Profile IDs are
unique and become expandable HUD jobs. A critical profile must prepare and
verify successfully before Ubuntu receives the saved Power Off or Restart
action.

The installed Windows workspace VM uses the structured
`qemu-windows-hibernate` adapter. Install or refresh it with:

```sh
wsctl shutdown-profiles install-qemu-windows \
  /absolute/path/to/windows-vm --id windows-word-vm --force
wsctl shutdown-profiles list --action poweroff --probe
```

The adapter is active only when its exact QEMU process is live. It verifies the
PID, executable, disk and QMP socket, requires QMP `running` plus a responsive
QEMU Guest Agent, sends Windows `shutdown.exe /h`, and waits for the original
QEMU process to exit. Cancellation never kills hibernation halfway through. It
lets Windows finish, then runs the VM's protected `launch.sh` and waits until
both QMP and QGA are ready again.

Before hibernation, the adapter also resolves the one verified `remote-viewer`
process to its stable `gnome-winctl` window ID and records the named workspace,
window geometry/state, and physical monitor EDID/serial identity in the private
write-ahead journal. The coordinator captures this placement into a private,
operation-bound preflight record before publishing status that lets Shell make
the HUD modal. The worker verifies the same QEMU and viewer process identities
and therefore never depends on GNOME placement RPC while the system-modal HUD
owns input. That record becomes a durable startup receipt only at
GNOME's final `EndSession` point of no return. A cancelled or failed shutdown
removes it while rolling the VM back. On a later kernel boot, wsctl launches the
VM once per kernel boot, remaps the saved workspace name and physical display,
synchronizes the VM viewer's connector placement, and verifies the exact GNOME
window. The durable active intent remains valid across an unexpected reset, so
the VM is recovered again on the following boot; a later clean managed shutdown
replaces it with the VM's then-current active or inactive state. It never runs
during a same-boot relogin, and a VM that was not active at the confirmed clean
shutdown is not started. If the saved physical display is absent, VM launch
fails visibly instead of silently using another monitor; terminal/browser
restoration and cloud-drive startup still continue.

Arbitrary integrations use the `command` adapter. Commands are exact argv
arrays; the first element must be an absolute executable path and no shell is
used. Exit status 3 from `probe` means “not active”; zero means applicable. The
`prepare`, `verify`, and idempotent `rollback` commands must all succeed with
zero. Example:

```toml
schema_version = 1
id = "example-service"
label = "Example service checkpoint"
adapter = "command"
actions = ["poweroff", "restart"]
critical = true
timeout_seconds = 120
rollback_timeout_seconds = 120
cancel_policy = "terminate-then-rollback"
probe = ["/absolute/bin/examplectl", "is-active"]
prepare = ["/absolute/bin/examplectl", "checkpoint"]
verify = ["/absolute/bin/examplectl", "checkpoint-status", "--ready"]
rollback = ["/absolute/bin/examplectl", "resume"]
```

Use `finish-then-rollback` only when interrupting `prepare` could corrupt the
managed application. Profile configuration is snapshotted into the private
runtime rollback journal before `prepare`, so later edits cannot change an
in-flight recovery command. Profiles never replace Ubuntu's ordinary service
teardown; they prepare only applications that need explicit pre-shutdown state.

## tmux-resurrect and continuum contract

The installed tmux configuration uses these hooks:

```tmux
set -g @resurrect-processes '"wsctl-codex->wsctl-codex-resume *"'
set -g @resurrect-hook-post-save-layout '~/.local/bin/wsctl tmux save'
set -g @resurrect-hook-pre-restore-all '~/.local/bin/wsctl tmux begin "$$"'
set -g @resurrect-hook-post-restore-all '~/.local/bin/wsctl tmux restore'
set -g @continuum-restore 'on'
set -g @continuum-save-interval '15'
set -g @resurrect-save-script-path '/home/sagecat/.local/bin/wsctl-continuum-save'
set -g @resurrect-restore-script-path '/home/sagecat/.local/bin/wsctl-continuum-restore'
```

The post-save-layout hook receives the resurrect state-file path as its final
argument. `wsctl tmux save FILE` maps each pane to the root conversation UUID
recorded in its live rollout metadata. Open child session rollouts are normalized to
that same root UUID, and conflicting rollout identities make the pane
unrestorable rather than selecting an arbitrary child. The hook stores a compact
`wsctl-codex UUID` command. Resurrect's documented `->`/`*` expansion turns
that into `wsctl-codex-resume UUID`. The wrapper resumes the exact conversation
and then replaces an exited or deliberately closed terminal application TUI with the user's
shell, so a resume failure can never destroy its restored tmux pane or window.
`make install` updates this one mapping both in the persistent tmux config and
in the live tmux server; `make uninstall` removes only that managed mapping.
If a terminal application identity is not provable, wsctl deliberately
leaves that process unrestorable instead of starting an unrelated conversation.

The same hook autosaves the terminal category every 15 minutes. Continuum
restores tmux at boot, including the contracted terminal application commands, and resurrect's
post-restore hook calls `wsctl startup` to restore application windows and
Wayland placement. The Alacritty trigger is a fallback and shares the same
startup lock. Bootstrap Alacritty uses a main-process-only transient unit, so
systemd cannot adopt and later force-kill the long-lived tmux server during
graphical teardown. Continuum uses a small wrapper that owns an explicit lifecycle
marker, so the launcher fallback cannot touch tmux while resurrect is still
rebuilding a large layout.

The GNOME session client starts only after the Wayland Shell service is active,
and it does not launch Alacritty or Chrome during compositor startup. Login
restore starts only after `graphical-session.target` is active,
`gnome-winctl` reports valid physical display identities with no active monitor
recovery, every saved workspace name exists, and the complete monitor/workspace
topology has remained unchanged through a short post-handoff settle interval.
The same readiness gate is enforced again inside the serialized startup
transaction, so neither terminal placement nor browser restoration can bypass
it. The launcher performs exactly one full transaction. It never automatically
replays a failed pass whose application windows may already be live. terminal application UUID
verification has its own 15-second bound; conversations that are still starting
are reported as degraded while Chrome, placement, and cloud-drive startup
continue. Its worker, bootstrap terminal, and direct restore fallback run in
separate transient user services. Every restored Alacritty or Chrome process
tree gets its own
graphical-session service with `ExitType=cgroup`, so finishing a bounded restore
worker cannot kill an application's tmux client, renderers, or GPU process; the
application units still stop normally with the graphical session.
After terminal, browser, and conditional VM restoration finish, wsctl starts
the static `wsctl-workspace-restored.target`. Services that must not compete with
login can
use `WantedBy=wsctl-workspace-restored.target`; the target is never enabled at
boot and is stopped with the graphical session. Restore journals are scoped to
the current GNOME Session Manager instance, so a same-boot re-login cannot reuse
the preceding session's completion state.

`wsctl-login-finalize.service` is ordered after that target. It starts Google
Drive, the private Nextcloud drive, and Proton Drive together, waits for each
mountpoint to be verified with `findmnt`, then runs the first metadata warm-up
and starts its hourly timer. Installation removes those four units from
`default.target` and gives them a `Requisite`/`After` gate on the workspace
milestone. Consequently a fresh boot never lets network-backed mounts compete
with GNOME, tmux, Alacritty, terminal application, or Chrome restoration. The mount services
remain persistent after they have started; a later same-boot login can report
an already-mounted drive immediately.

The HUD presents GNOME/Wayland plus display/workspace readiness as one job,
tmux-resurrect plus Alacritty/tmux reconciliation as one job, and the three
mounts plus metadata warm-up as one **Cloud drives and metadata** job. Every HUD
job can be expanded to show its bounded, timestamped activity log and internal
substeps. A separate **Windows VM restoration** job reports whether a committed
hibernated VM was skipped, restored, or could not reach its exact saved display.
Shutdown adds each applicable profile between the desktop/browser checkpoint
and the final checkpoint-integrity proof.

The save wrapper holds a process-lifetime lock around resurrect's complete save
and suppresses duplicate saves in the same second. This closes the filename
collision where resurrect could point `last` at a candidate and then delete that
same file. Rebind resurrect's manual save key to the wrapper after TPM loads so
manual and continuum saves share the lock:

```tmux
bind C-s run-shell '/home/sagecat/.local/bin/wsctl-continuum-save'
```

Automatic saves are armed only after terminals have been restored successfully
for the current login, or after an explicit `wsctl save`. Until then, the save
hook makes continuum retain the previous `last` state. This prevents the first
partial login state from overwriting the recipe that is still needed for
recovery.

Terminal capture failures, dangling client/session references, and an
unexpected empty autosave are rejected. Each successful replacement keeps a
private rolling copy at
`~/.local/share/workspace-state/recovery/current.last-good.json`.

## Browser and desktop integration

Chrome communicates through a native-messaging host and a private Unix socket
under `$XDG_RUNTIME_DIR/workspace-state/`; wsctl does not parse Chrome's private
session files. After the one-time companion installation, startup launches each
missing saved profile with its exact `--profile-directory` and
`--restore-last-session`, then waits until Chrome's own window set settles.

For each saved window, the companion first claims an unclaimed live window by
its full tab URL/pinned-state fingerprint, falling back to an ordered site
fingerprint when URLs changed during shutdown. `wsctl` temporarily activates a
private marker tab in that Chrome window, focuses it through `chrome.windows`,
and observes the one active marker-titled native window to obtain its exact
stable `gnome-winctl` ID. That existing window is moved and verified against the
saved workspace, physical monitor, geometry, and state. Only a saved fingerprint
that Chrome did not restore causes a new window to be created through the
sequential expectation mechanism. When wsctl starts Chrome at login, it freezes
the IDs returned by Chrome's native session restore. After every saved window
has one distinct, verified live keeper, wsctl closes any unclaimed window from
that frozen set as a duplicate. Windows created after the frozen set was taken
are never part of automatic cleanup.

The CLI requires companion protocol version 2 and all focus/identification
capabilities before it starts placement. Per-window login journals make a
partial browser retry idempotent. Periodic tmux/Continuum autosaves preserve the
durable browser recipe unchanged; Chrome is captured only by explicit/full
saves, including the strict GNOME end-session checkpoint.

`gnome-winctl` exports window capture, stable-window placement, and sequential
expectation/status methods at `org.sagecat.GnomeWinCtl1`. `wsctl` uses its JSON
CLI as the integration boundary so the placement service remains independently
useful to other tools.

## Development

```sh
make test
PYTHONPATH=src python3 -m workspace_state --help
node --check chrome-extension/service-worker.js
node tests/test_chrome_extension.js
```
