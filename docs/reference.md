# Configuration and command reference

[Overview](../README.md) · [Visual guide](visual-guide.md)

## Install

The startup-only **Важливе** tab collects persistent critical incidents from an
explicit inventory of your own programs, daemons and plugins. Private inventory
files may also name remote services. It excludes third-party applications and has
no role at shutdown.
See [owned-system alerts](owned-system-alerts.md) for the narrow installer,
inventory, coverage limits and report/resolve API.

```sh
make install
```

Nemo integration needs Ubuntu packages `nemo-python` and `gir1.2-nemo-3.0`.
`make install-file-manager` installs only that integration without restarting
Nemo, GNOME or the session coordinator. See [file manager restoration](file-manager.md)
for behavior, safety checks and isolated verification.

`make install` stages one immutable production release from the current sibling
repositories and schedules its installation **before the next graphical login**.
It leaves this session's helper paths and loaded coordinator/HUD together until
logout. The next-login activation unit switches all owned paths transactionally;
a failed installation retains the previous release and does not block the desktop.
Only systemd unit definitions are reloaded when scheduling; no applications,
cleanup services, extensions, or desktop sessions are restarted or enabled.
Inspect source/installed/scheduled/running versions with `wsctl deployment doctor`.
See [deployment and rollback](deployment.md).

Python 3.11 or newer is required. Production `make install` rejects a nondefault
`PREFIX` before writing anything; isolated tests/install roots use the documented
`deployment.Locations` API. `make install-dev` explicitly retains the former
mutable-source setup and host/service activation. Component-specific legacy
install targets, including `install-session`, are explicit development/host helpers.
Keep GNOME on Wayland; enable any first-time GNOME extensions explicitly.

Branded Google Chrome does not permit command-line installation of unpacked
extensions. Install the companion once in every Chrome profile that wsctl should
manage:

1. Open `chrome://extensions` and enable **Developer mode**.
2. Choose **Load unpacked** and select
   `~/.local/share/workspace-state/chrome-extension`.
3. Open the extension options, keep the profile label unique, and enter its
   Chrome profile directory. The supported browser app ID is `google-chrome`.
4. Restart Chrome once so its service worker connects to the native host. After
   applying a scheduled release, use **Reload** on `chrome://extensions` (or
   restart Chrome) so the running service worker uses the new protocol. For a
   signed-in profile, the native host can resolve its real Chrome directory (for
   example `Default` or `Profile 1`) automatically; the options remain the
   fallback for unsigned profiles.

If Chrome currently registered the checkout directory, re-register **Load unpacked**
from the stable managed directory after the scheduled installation, using the
unchanged key/ID and checking the profile mapping. Reloading the checkout path
cannot activate the immutable release. Doctor flags this mismatch; preferences
are never rewritten automatically.

The fixed unpacked-extension ID is `gnccboicpdhhhpdcogeleiegokieocmn`.

Chrome restoration reuses native tab groups and never creates replacement groups.
Groups are recognized by their member tabs, so unnamed groups with the same color
remain separate. Retries preserve their existing group IDs.

Chrome's extension API cannot reopen closed saved groups from the bookmarks bar.
If Chrome has not restored an original group, its tabs can be restored ungrouped;
the HUD reports the missing group for manual reopening. No duplicate saved group
is created. Group membership and names are left as Chrome restored them.
See the [Chrome tab-group API](https://developer.chrome.com/docs/extensions/reference/api/tabGroups).

`make install` also installs the Alacritty startup wrappers. Both the desktop
launcher and the GNOME terminal shortcut can claim the first terminal launch of
a login. That first Alacritty attaches to `main`. wsctl alone restores tmux from
its snapshot: it reuses a live session that matches the saved one, creates only
what is missing and then places the windows. Later terminal launches behave
normally.

Tmux checkpoints also preserve each pane's explicit pane-local `@pane_label`
and its `pane_title`. The visible label is authoritative: restoration sets it
on the mapped live pane and initializes the title to the same name. Unlabeled
panes retain their captured title; applications can subsequently update that
title normally. No global border format, title hook, or window name is changed
to implement pane naming, including for panes inside `DEBUG_WINDOW`. Names are
reapplied whether wsctl reuses a live session or recreates it.
Older checkpoints without these fields leave existing pane names untouched.
If additional live panes make the saved index mapping ambiguous, reconciliation
fails before mutation rather than assigning a saved name to an inserted pane;
save the updated layout before restoring it. Pane names do not change the exact
terminal session UUIDs used for resumption.

The enabled `wsctl-gnome-session.service` makes this recovery automatic after a
GNOME login, without waiting for the first manually opened terminal. The unit is
ordered after GNOME Shell's Wayland service and still verifies the compositor,
workspace names, and stable display topology before publishing the first HUD
status or launching restoration. The HUD and wsctl do not configure, restart,
or delay NVIDIA; display readiness is observational only.

The same coordinator registers as a GNOME session client and holds one logind
`delay` inhibitor while the graphical session is active. Shutdown is not a
transaction: wsctl never cancels, blocks or delays a power-off beyond logind's
delay window, and a plain `sudo shutdown now` is treated like the menu.

- The floor is the periodic checkpoint (`wsctl-checkpoint.timer`, at most five
  minutes old).
- On logind `PrepareForShutdown(true)` or GNOME `EndSession`, whichever comes
  first, the coordinator starts one bounded `wsctl save --auto --final` in its
  own process group. GNOME's `QueryEndSession` is answered at once, and
  `EndSession` is answered after the save ended or timed out, always with
  `True`. `CancelEndSession` and `PrepareForShutdown(false)` re-arm the agent
  and take the inhibitor again and remove the session-ending marker. `Stop` and
  `SIGTERM` defer the agent's exit until a running final save ends.
- The agent's first end signal writes `$XDG_RUNTIME_DIR/workspace-state/session-ending.json`
  (boot ID, agent PID and start time); while it is valid a periodic checkpoint
  is skipped with exit 0 ("the final save owns this checkpoint"). A marker of a
  dead process or another boot is ignored; the periodic checkpoint also skips
  publication while `graphical-session.target` is not steadily `active`, and
  `wsctl-checkpoint.service` is `PartOf=` that target.
- The budget is `min(20 s, InhibitDelayMaxSec - 3 s)`, at least 1 s
  (`InhibitDelayMaxSec` is read from logind, 5 s if unreadable). At the deadline
  the save's process group gets `SIGTERM`, one second later `SIGKILL`, and the
  inhibitor is released.
- The final save is the periodic checkpoint with two differences: it waits up to
  15 s for a running capture instead of skipping, and it publishes nothing while
  `graphical-session.target` is not steadily `active` (the previous checkpoint
  stays). It records `final-save` provenance, which like `auto-save` never
  extends a manual acceptance to content you did not review, and it does not
  capture Chrome.
  It adds and updates but never removes: an item of the previous checkpoint
  that is missing from this capture stays pending until the next login, because
  applications close while the final save runs.
- The session agent writes these lines to its journal (`wsctl-gnome-session.service`):
  `wsctl: final save started; trigger=<prepare-for-shutdown|end-session>; budget=<N.N>s`,
  `wsctl: final save finished; rc=<n>; elapsed=<N.N>s`,
  `wsctl: final save stopped at deadline; elapsed=<N.N>s`,
  `wsctl: logind delay lock released` and `wsctl: GNOME <signal>`.

A privileged forced power-off can override the delay window; only the periodic
checkpoint covers it.

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

`make install-tmux-lifecycle` installs a separate Ubuntu-owned tmux stop hook.
Its start action is inert; it never changes live sessions. Only when the
**system** manager reports `stopping` does its stop action find the tmux server
from the kernel socket table (the process of the current user that holds the
listening `/tmp/tmux-<uid>/default` socket) and send it `SIGTERM`, which tmux
handles as `kill-server`, then wait up to five seconds. It never asks a busy
server over its socket. The journal tells a timeout, a missing server and an
ambiguous one apart. Normal unit stop/uninstall/relogin is a no-op for tmux. The
helper refuses nonstandard sockets and another user's processes and does not
change the vendor system timeout.

Separate opt-in Ubuntu service shutdown-order/exit-status corrections are
documented in [the shutdown compatibility guide](shutdown-compatibility.md).
Use `make check-shutdown-compat` to verify staged definitions without changing
the live system. They are not installed by default.


## Commands

```sh
wsctl save                 # replace the one saved state
wsctl save --auto          # periodic checkpoint; reads Chrome by window titles after a verified restore
wsctl save --auto --final  # the bounded final save the session agent runs at power-off
wsctl show --details       # inspect it, including pending items
wsctl forget KEY...        # drop pending items that should not be restored
wsctl restore              # terminals, browsers, visible social apps, file manager, VS Code
wsctl restore terminals    # terminals only
wsctl restore browsers     # Google Chrome only
wsctl restore social-apps  # built-in and locally configured desktop apps
wsctl restore --dry-run    # do not change the desktop
wsctl startup              # restore once per login, starting missing apps
wsctl tmux configure       # obsolete no-op: wsctl never edits the tmux configuration
```

To check saving separately from power off, run `wsctl save` and inspect
`wsctl show --details`. A normal save captures the current desktop and explicitly
accepts each completely captured category as the new baseline. It does not
request shutdown, close applications or clear failed restore markers. Each
category is captured independently: a failed or incomplete category keeps its
last good state and the others still save. The save then exits with status 3
and names each kept category; exit status 0 means every category was captured
completely. `--allow-partial` is not a complete-save verification. Without a
last good state, an incomplete category is published, marked failed in its
provenance and not accepted.

For unresolved older native clients, `wsctl save --verify-idle-codex` explicitly
allows a bounded `/status` query in each affected pane. The client must be idle
with an empty composer. Draft input, active work, copy mode, ambiguous output or
changed ownership refuses the save; input is never cleared or interrupted.
The query makes no model request. Leave those composers untouched during the
check. This option is never used by automatic capture or the final save.
Unsupported rendering, pane resizing or a byte-identical repeated report refuses
the save and preserves the checkpoint. A refusal may leave the exact `/status`
literal in the composer; it never submits or clears an unrelated draft. Use a
larger visible pane for a separately authorized retry, or resume the known exact
UUID. The flag does not make ambiguous terminal evidence acceptable.

`restore` also supports `--workspace NAME`, repeatable `--session NAME`,
`--select`, and `--no-place`. `startup` uses markers under
`$XDG_RUNTIME_DIR/workspace-state/startup-<boot-id>-<login-id>/`, so the Alacritty launcher
and tmux hook can safely trigger it together. `startup --force` deliberately
runs it again during the same login.

New and reused Alacritty windows share the same verified placement path. For an
inactive destination workspace, the exact window is first placed on the current
workspace, its monitor/state/frame are allowed to settle, and only then is it
sent to the saved workspace. A GNOME `placed: true, deferred: true` response is
not proof of completion: the observed final workspace, display and state must
remain correct for one second before the HUD counts that terminal as restored.
Each window has a bounded 12-second placement wait with expected/observed details
on failure. A known but disconnected saved display is an error, not silent
success on the primary monitor. Already-correct windows are verified without
moving them, and this procedure never restarts tmux or its panes.

`save` keeps the last good recipe of a category whose capture is incomplete:
the GNOME or Chrome companion is unavailable, a window lacks placement, a live
conversation UUID cannot be resolved, or the provider failed. Terminals keep
their whole recipe: sessions, Alacritty clients and the desktop they were placed
on. `--allow-partial` accepts an incomplete terminal capture instead, saving an
unresolved conversation pane as a plain shell, and exits 0. Use it only when
incomplete state is intentional. A terminal capture that failed outright is
never accepted. The terminal checkpoint, also run by the old tmux save hook, can
refresh terminal state while Chrome is closed; in that case it preserves the last
saved browser and social-app categories.

### Pending items

Every save keeps a saved item that is not live, unless this login already saw
it live: then the user closed it. Items are tmux sessions, AI conversations,
VS Code windows, Nemo windows and windowed desktop apps; a plain shell window
is not an item. A kept item stays in its category's data and is listed in the
checkpoint's `pending` index until it is restored or forgotten. A conversation
saved in a session that is live now keeps its saved window, or gets a window of
its own when that index is taken. Restore adds such windows to the live session
and never starts a conversation that already runs. A session closed or renamed
after it was live in this login loses its windows and terminal client, but its
conversations that were never seen stay pending in windows of their own. A
conversation that appears anywhere in the current capture is never carried a
second time.

Restore never creates a second tmux session for a name that is taken. When a live
session has the saved name and a layout that no reconcile or partial
append can use, restore creates nothing. That session's items get a failed item
result with the reason `tmux session NAME is in use with a different layout; its
saved items stay pending`, and the restore is not recorded as seen. After the
next checkpoint the occupying session's name is seen, because it is live, and
the conversations that were never seen are carried in windows of that live
session. The next restore appends them to it through the existing partial-append
reconcile: no copy is made and no conversation starts twice, because the live
index refuses to launch a conversation that already runs. A `NAME-wsctl` copy made
by an older build is still reconciled.

This login's record of seen items lives in the startup directory under
`$XDG_RUNTIME_DIR`. Each capture and each verified restore adds to it. A waiting
resume wrapper, a supervised Codex resume that has not verified its client and
a claimed Claude client without its hook receipt are not seen yet. Without a
login identity the record is unavailable and every unseen item stays pending.
`wsctl show` lists pending items with their key and the time since which they
are pending; `wsctl forget KEY...` drops them and their saved data. An unknown
key exits 2 and changes nothing. VS Code and Nemo keep at most their saved
window limit; a save names any pending window it had to drop.

A conversation client whose evidence is briefly missing, for example while it
starts, compacts or awaits its Claude hook receipt, keeps its saved identity
when it is the same process in the same tmux pane. Contradicting evidence
saves the pane as unresolved and keeps the saved conversation pending.

### Periodic checkpoints

`save --auto` writes a periodic checkpoint. `wsctl-checkpoint.timer` runs it two
minutes after login and then five minutes after each run ends, while the
graphical session is active. It captures every category. Chrome windows are
identified by their titles alone, so the capture never activates a tab or marks
or focuses a window; a title that is ambiguous or changes during the capture
keeps the previous Chrome recipe. Page titles, focus and the active tab alone do
not republish. The final save keeps the Chrome recipe of the last periodic
checkpoint, because Chrome closes while it runs. An unresolved conversation pane
is saved as unresolved while its saved identity stays pending; any other
terminal problem keeps the whole terminal recipe. It publishes only when a
category's content, state or pending items changed, never accepts a category
as a new baseline, and exits 0 for category problems.

A checkpoint never waits. It skips, with exit 0 and a reason, while another
capture or the login restore runs, when the desktop topology or the login
changes during capture, and when a fallback display would replace a physical
monitor layout. `save --auto --final` differs only in waiting up to 15 s for a
running capture and in publishing nothing while `graphical-session.target` is
not `active` or has a pending job. It captures
without the state lock and takes that lock only to publish. A save published
meanwhile makes the capture stale, so the checkpoint skips.

A successful explicit `save` accepts each fully captured application category as
the baseline for the current login. Later periodic and final checkpoints keep that
acceptance only while the content is unchanged; saving again re-accepts new
tabs and intentional closes even if that application's earlier startup failed.
The failed startup result remains visible. A failed or incomplete category
retains its previous recipe and is not newly accepted. Acceptance is bound to
the boot, login and exact saved category data; neither a terminal-only autosave
nor `save --auto` accepts a category.

`show` distinguishes the checkpoint update time from each category's capture
time and prints both in local time. Snapshots store new timestamps in UTC;
timestamps of older snapshots stay exactly as stored. The JSON `category_provenance` records carry capture evidence, content
digests, explicit acceptance and retention reasons. Terminal-only autosaves
preserve this evidence and any preservation warnings. Capture times for legacy
categories without trustworthy evidence remain unknown.


## Desktop app visibility and placement

The HUD has a separate **Desktop apps** job for Slack, Discord, Telegram, Viber
and locally configured apps
with per-app activity logs and progress. The checkpoint saves the same apps'
visibility and placement as part of the durable workspace save.
Each app records whether its process was running and one of three modes:

- `windowed`: at least one non-minimized normal GNOME window, on any workspace.
  Launch only if needed; reuse existing windows and restore their named workspace,
  physical display, geometry and window state. Windows on an inactive workspace
  still count; a window need not be focused or unobscured.
- `background`: running with no such window (including tray-only/minimized apps).
  Do not launch or raise it.
- `stopped`: not running. Do not launch it.

Restore never kills an independently running app and never sends messages.
It uses installed desktop launchers and independent graphical service ownership.
If a launcher replaces its splash/updater window (as Discord does), restoration
follows the replacement instead of waiting on the vanished window ID. Newly
launched windows must keep their verified placement for three seconds; retries
share the original per-app deadline and never relaunch the app. Activity logs
identify window replacements and report the observed placement on timeout.
One app's failure remains visible in the HUD but does not skip the other apps or
block the subsequent drive handoff. An absent saved monitor/workspace or missing
secondary app window is an explicit failure, not a guessed new destination or
false success. Unsupported app-specific pop-out creation is not fabricated.
Old checkpoints without `social_apps` launch none of these apps until a full
save records their actual state. Terminal-only autosave preserves this recipe.

For a non-disruptive inspection or isolated reconciliation (no host power action):

```sh
wsctl show --details
wsctl restore social-apps --dry-run
wsctl restore social-apps
```

Additional desktop apps can be registered in the private file
`~/.config/workspace-state/desktop-apps.toml` (`XDG_CONFIG_HOME` is respected):

```toml
[[apps]]
id = "notes"
label = "Notes"
aliases = ["org.example.notes"]
desktop_ids = ["org.example.Notes"]
executables = ["notes"]
```

Use the app IDs reported by `gnome-winctl windows`. Desktop IDs name installed
`.desktop` launchers without the suffix; executable names identify background
processes. IDs must be unique and must not overlap a built-in app. No commands
or application credentials belong in this file.

The next save captures these windows using the same visibility and placement
rules. Adding an entry alone does not launch it: an older checkpoint with no
saved entry is skipped. Removing configuration for an app still present in the
checkpoint reports an error instead of guessing a launcher. App content and
sign-in remain the application's responsibility. The HUD lists this category
as **Desktop apps**; the stable CLI name remains `social-apps`.

Install the matching runtime before saving additional app records. Older
releases that only support the four built-in apps cannot read those records.

This policy governs wsctl's launches. Independently configured application or
desktop autostart is a separate launch source; disable any conflicting autostart
entry if it should follow the checkpoint instead.


## tmux ownership and old resurrect hooks

wsctl alone owns the tmux layout. tmux-resurrect and tmux-continuum restore and
save nothing, and wsctl no longer edits the tmux configuration: `wsctl tmux
configure` only says so, and `make install` does not call it.

Older tmux configurations may still point at wsctl. These entry points stay
installed as shims that always exit 0 and never run resurrect:

```tmux
set -g @resurrect-hook-post-save-layout '~/.local/bin/wsctl tmux save'
set -g @resurrect-hook-pre-restore-all '~/.local/bin/wsctl tmux begin "$$"'
set -g @resurrect-hook-post-restore-all '~/.local/bin/wsctl tmux restore'
set -g @resurrect-save-script-path '/home/example/.local/bin/wsctl-continuum-save'
set -g @resurrect-restore-script-path '/home/example/.local/bin/wsctl-continuum-restore'
bind C-s run-shell '/home/example/.local/bin/wsctl-continuum-save'
```

- `wsctl-continuum-restore` and `wsctl tmux begin|restore|end|save-allowed|contract`
  do nothing, so Continuum's boot restore recreates nothing. The prefix+C-r key
  is different: it runs resurrect's own `restore.sh`, not the configured
  restore script, and replays the last resurrect state file, which this release
  no longer updates. Use `wsctl restore` instead. Removing the plugin lines
  from the tmux configuration is the owner's choice; wsctl never edits it.
- `wsctl-continuum-save` and `wsctl tmux save [STATE_FILE]` run only the
  terminal-only checkpoint of `save --auto`, never resurrect's save, and never
  report a failure to the calling plugin. They ignore and never write a
  resurrect state file.
- `wsctl tmux unconfigure` (run by `make uninstall`) still removes the
  `@resurrect-processes` mapping that older releases wrote.

An old `@resurrect-processes` mapping is harmless without resurrect. The resume
wrappers `wsctl-codex-resume` and `wsctl-claude-resume` are what restored panes
run; they resume the exact session inside the shell wsctl created for the pane.
When the resumed application exits or cannot resume, the wrapper returns to that
same shell instead of spawning a nested second shell, so the restored tmux
pane/window remains usable without a `zsh -> zsh` process chain.
Automatic resume inherits the terminal application's global configuration and
shared background service. A per-application-data-directory startup
lock serializes initialization, releasing
after thread ownership is verified rather than holding it for the session's
lifetime. Only an early SQLite initialization lock error from the current attempt
is retried, at most three times. Other exits return to the existing shell.
Missing saved directories wait outside that lock; cloud-backed directories
require their mount first. Desktop restoration can then proceed to mount startup,
and the login finalizer rechecks the deferred sessions until the shared deadline. A slow live terminal
process is left intact and reported unverified, never killed to force a retry.
If a terminal session identity is not provable, wsctl deliberately
leaves that process unrestorable instead of starting an unrelated session.

The first session of a login starts the tmux server in a transient scope of its
own (`systemd-run --user --scope --collect --quiet
--unit=wsctl-tmux-server-<suffix> --slice=app.slice`), so the server lives in
neither the restore worker's unit (`RuntimeMaxSec`) nor the session agent. It
receives the user manager's environment (the `Environment` property of
systemd's user manager, so no release `PATH` or `PYTHONPATH` binds the panes to
one release) without `WSCTL_*` operation variables or unit variables such as
`INVOCATION_ID`, plus `COLORTERM=truecolor` when unset, which terminal
applications use to enable truecolor. If the manager environment cannot be
read, the worker's own environment is used minus what `wsctl-python` added, with
a diagnostic. Only when `systemd-run` is missing does the server start
directly. If the scope fails, wsctl never starts a server in its own unit: it
joins a server that a user terminal started meanwhile. A session of the saved
name counts as restored only when it carries the `@wsctl-created` nonce that the
same `new-session` invocation set; a session the user's shell created meanwhile
is never adopted and its items stay pending. Otherwise reports the item as failed, so it stays
pending for the next restore. Restoring again in the same login reuses the live sessions and creates
nothing twice.

The terminal checkpoint runs every five minutes (`wsctl-checkpoint.timer`) from the session agent, with the
rules of `save --auto`, with no tmux client needed. Login restore is the
single `wsctl startup` of the bounded worker, which restores tmux sessions and
then application windows and Wayland placement. No bootstrap terminal is
started. The Alacritty wrappers claim the same startup lock; until this login's
`terminals.done` exists, a terminal they launch without arguments never creates
or attaches `main` (the user's shell would create an empty one before wsctl) and
gets a temporary workspace-scoped session instead.

The GNOME session client starts only after the Wayland Shell service is active,
and it does not launch Alacritty or Chrome during compositor startup. Login
restore starts only after `graphical-session.target` is active,
`gnome-winctl` reports valid physical display identities with no active monitor
recovery, every saved workspace name exists, and the complete monitor/workspace
topology has remained unchanged through a short post-handoff settle interval.
The same readiness gate is enforced again inside the serialized startup
transaction, so neither terminal placement nor browser restoration can bypass
it. The launcher performs exactly one full transaction. It never automatically
replays a failed pass whose application windows may already be live.

Category problems are results, not errors. A category whose items partly failed
publishes `degraded` with the number of items that need attention and why; a
category whose job raised publishes `failed`. Either way `wsctl startup` exits 0
and the other categories continue. Only an orchestration failure exits non-zero:
an unreadable snapshot, the startup lock, unavailable GNOME shell or placement
capabilities, a failed `workspace` stage, or a cancelled or expired operation.
Stage markers still keep a repeated startup from retrying a finished category.
A manual `wsctl restore` exits 0 when everything selected was restored, 1 when
nothing matched the selection, 2 on an error, and 3 when some item needs
attention and stays pending (the same code as a partial `save`).

Conversation verification has one shared deadline per login, at most
`CONVERSATION_DEADLINE_SECONDS` (3 minutes). The first startup restore or
finalizer to verify creates it in the login's startup directory and every later
user reads the same instant (system-wide monotonic clock). A startup restore
verifies until all conversations are verified and stable, or until its own bound
(at least 15 seconds, five seconds per saved session, capped by `--wait`). If its
bound ends first, the `codex` stage is `waiting` and the login finalizer continues
verifying until the shared deadline, then reports `degraded`. A manual restore keeps
its bounded wait and writes no deadline. Check the panes of unverified sessions
for startup errors or prompts. A live process with a UUID on its
command line alone is not proof of a successful resume: verification requires
its open rollout, or a ready TUI with the exact thread loaded in the local daemon.
A repeated startup pass preserves this session-verification result rather than treating the
terminal-layout completion marker as proof of a successful resume.
A conversation whose resume failed was not seen live, so every checkpoint
keeps its saved identity pending instead of a plain shell. A still-waiting
wrapper keeps the identity in its own pane.
Its worker and direct restore fallback run in
separate transient user services. Every restored Alacritty or Chrome process
tree gets its own
graphical-session service with `ExitType=cgroup`, so finishing a bounded restore
worker cannot kill an application's tmux client, renderers, or GPU process; the
application units still stop normally with the graphical session.
After terminal and browser restoration finish, wsctl starts
the static `wsctl-workspace-restored.target`. Services that must not compete with
login can
use `WantedBy=wsctl-workspace-restored.target`; the target is never enabled at
boot and is stopped with the graphical session. Restore journals are scoped to
the current GNOME Session Manager instance, so a same-boot re-login cannot reuse
the preceding session's completion state.

`wsctl-login-finalize.service` is ordered after that target. Failed or degraded
stages are needs-attention results, not a service failure: the finalizer marks
itself ready, the overall message reads "Login completed with items needing
attention" and it exits 0. Only an exception, a timeout or lost operation
ownership exits 1. Such a failure of the unit is recorded as a `warning`
incident, which the HUD does not show, and resolves when the unit next succeeds. It starts Google
Drive, the private Nextcloud drive, and Proton Drive together, waits for each
mountpoint to be verified with `findmnt`, then runs the first metadata warm-up
and starts its hourly timer. Explicit legacy `make install-session` host setup removes those four units from
`default.target` and gives them a `Requisite`/`After` gate on the workspace
milestone. Consequently a fresh boot never lets network-backed mounts compete
with GNOME, tmux, Alacritty, terminal sessions, or Chrome restoration. The mount services
remain persistent after they have started; a later same-boot login can report
an already-mounted drive immediately.

The HUD presents GNOME/Wayland plus display/workspace readiness as one job,
tmux session reconciliation as one job, and the three
mounts plus metadata warm-up as one **Cloud drives and metadata** job. Every HUD
job can be expanded to show its bounded, timestamped activity log and internal
substeps. The HUD shows no shutdown mode.

Terminal capture failures, dangling client/session references, and an
unexpected empty autosave are rejected. Each successful replacement keeps a
private rolling copy at
`~/.local/share/workspace-state/recovery/current.last-good.json`.


## Browser and desktop integration

Chrome communicates through a native-messaging host and a private Unix socket
under `$XDG_RUNTIME_DIR/workspace-state/`; wsctl does not parse Chrome's private
session files. After the one-time companion installation, startup launches each
missing saved profile with its exact `--profile-directory`,
`--restore-last-session` and `--hide-crash-restore-bubble`, then waits until
Chrome's own window set settles. The paired switches request one native recovery
for an owned startup, including a profile whose previous process crashed. Chrome
restores its own tabs, session data and groups before wsctl begins placement;
wsctl does not edit profile exit flags or run a browser restart loop.
The settle check tracks window IDs and ordered tab identities; page titles,
loading flags, focus, and geometry cannot prolong this structural wait.

For each saved window, the companion first claims an unclaimed live window by
its full ordered tab URL/pinned-state fingerprint and window type/incognito
identity. A homepage never substitutes for a saved session path, query, or
fragment. Newly created and reused windows must expose matching committed URLs.
Chrome's discarded and unloaded tabs count as present when their saved URLs are
committed and no navigation is pending; restoration leaves them parked. Loaded
tabs still need completed loading, and conflicting pending URLs do not prove
readiness. New/reused results require two successful observations within a
bounded polling period. This verifies saved tab state, not HTTP content or a
future redirect when a parked tab is selected. Failed windows retain their
restore token and ID for inspection and retry without navigating user tabs or
creating duplicate windows. A claimed window can still be placed when its URL
verification fails; an unclaimed grouped window with different tabs remains
untouched. Restoration continues with later windows. A group's collapsed state
can change during native identification; that alone does not block placement of
the same claimed tabs and group membership. The HUD reports the verified count
and retains the failure;
duplicate cleanup requires every saved window to succeed. `wsctl` temporarily activates a
private marker tab in that Chrome window, focuses it through `chrome.windows`,
and observes the one active marker-titled native window to obtain its exact
stable `gnome-winctl` ID. That existing window is moved and verified against the
saved workspace, physical monitor, geometry, and state. Only a saved fingerprint
that Chrome did not restore causes a new window to be created through the
sequential expectation mechanism. When wsctl starts Chrome at login, it freezes
the IDs returned by Chrome's native session restore. After every saved window
has one distinct, verified live keeper, wsctl closes only fully loaded windows
from that frozen set whose full fingerprint matches a keeper. Unrelated windows,
unverified/loading tabs, already-connected profiles, and windows created after
the frozen set was taken are excluded from automatic cleanup.

The CLI requires companion protocol version 2 and all focus/identification
capabilities plus `exact_url_restore` and `lazy_tab_restore` before it starts
placement. Completed
login journal entries are rechecked against exact live URLs before being reused.
The explicit `repair_restored_tabs` companion RPC is available for manual
recovery of a redirected saved URL. It requires the inspected live window ID
and full signature, validates unchanged tab identities before each write, and
uses the saved snapshot URLs. Startup never invokes URL repair automatically.
After an extension reload clears session claims, manual adoption also requires
the inspected signature and saved window, and cannot replace another claim. Per-window login journals make a
partial browser retry idempotent. Periodic terminal autosaves preserve the
durable browser recipe unchanged; Chrome is captured only by explicit/full
saves. When a category's capture is incomplete, the checkpoint keeps that
category's last-good data and reports the retention as partial state on stderr.
A successful startup or explicit acceptance of a complete capture in the same
login permits later intentional closes to be saved normally.
Full saves and tmux autosaves also refuse to replace a saved physical-monitor
layout with an explicit fallback output such as `None-1`; the existing checkpoint
remains available until the display driver recovers.

`gnome-winctl` exports window capture, stable-window placement, and sequential
expectation/status methods at `org.sagecat.GnomeWinCtl1`. `wsctl` uses its JSON
CLI as the integration boundary so the placement service remains independently
useful to other tools.
