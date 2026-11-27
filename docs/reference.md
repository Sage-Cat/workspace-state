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

Ordinary login restoration reuses native tab groups. Explicitly [reopening a parked workspace](workspace-switches.md) can reconstruct groups from its archive.
Groups are recognized by their member tabs, so unnamed groups with the same color
remain separate. Retries preserve their existing group IDs.

Chrome's extension API cannot reopen closed saved groups from the bookmarks bar.
If Chrome has not restored an original group, its tabs can be restored ungrouped;
the HUD reports the missing group for manual reopening. No duplicate saved group
is created. Group membership and names are left as Chrome restored them.
See the [Chrome tab-group API](https://developer.chrome.com/docs/extensions/reference/api/tabGroups).

`make install` also installs the Alacritty startup wrappers. Both the desktop
launcher and the GNOME terminal shortcut start no restore; only the session
agent does. A terminal opened while this login's terminal restore is still
pending gets a temporary session, never `main`. wsctl alone restores tmux from
its snapshot: it reuses a live session that matches the saved one, creates only
what is missing and then places the windows.

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
`--select`, and `--no-place`. Add `--from-history PATH` to recover from a
verified checkpoint generation. The current checkpoint stays in place.
Use `wsctl restore terminals --workspace NAME --session SESSION --terminal-placement-only`
to restore the display and frame of one attached Alacritty window already on that
workspace. Its running tmux panes are preserved.
When one saved editor belongs to a workspace, `wsctl restore vscode --workspace NAME --maximize`
places it maximized on its saved monitor. See [terminal restoration](terminal-restoration.md)
for restoring a named Agent Deck dashboard.

### Workspace switches

`wsctl workspace list [--json]` shows each named static GNOME workspace as
`on`, `off` or `partial`. `wsctl workspace on|off -- NAME` switches one. The
[workspace switch guide](workspace-switches.md) covers what blocks turning off.

`wsctl workspace forget [--yes] -- NAME` deletes the switch archive of one
workspace. It needs no readable archive and no existing GNOME workspace of that
name. Without `--yes` it only states what would be discarded and exits with an
error. Once confirmed, the windows saved in that archive cannot be reopened by
`on`. Items the ordinary checkpoint still lists as pending (`wsctl show`) are no
longer hidden and return at the next login unless dropped with
`wsctl forget KEY`. Use it for an archive that `list` reports as unreadable, which
pauses only that workspace, or to abandon a switch that cannot finish. Other
workspaces are never touched.

`startup` keeps its per-login state under
`$XDG_RUNTIME_DIR/workspace-state/startup-<boot-id>-<login-id>/`; the session
agent's restore and a manual `wsctl startup` are serialized by the startup
lock. A repeated run in the same login is a reconcile: each category finds what
is live and restores what is missing, and a conversation that is live is never
marked failed. It leaves alone a category whose placements the compositor still
owns, and skips Chrome once this operation restored its windows. It may return
a window that the user moved to its saved place. After the operation ended or
expired the command is refused (use `wsctl restore`). `--force` is accepted and
ignored.

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
neither the restore service's unit (`RuntimeMaxSec`) nor the session agent. It
receives the user manager's environment (the `Environment` property of
systemd's user manager, so no release `PATH` or `PYTHONPATH` binds the panes to
one release) without `WSCTL_*` operation variables or unit variables such as
`INVOCATION_ID`, plus `COLORTERM=truecolor` when unset, which terminal
applications use to enable truecolor. If the manager environment cannot be
read, the restore service's own environment is used minus what `wsctl-python` added, with
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
single `wsctl startup`, which the session agent runs once per login in the
transient service `wsctl-startup-restore-<login-key>.service`, bounded by the
operation deadline; it restores tmux sessions and then application windows and
Wayland placement. No bootstrap terminal is started. The agent records the
launch in the status document (`restore_unit`, `restore_login_key`) before it
starts the service, so a restarted agent in the same login never restores a
second time. The Alacritty wrappers start nothing. A terminal they launch
without arguments never creates or attaches `main` (the user's shell would
create an empty one before wsctl) and gets a temporary workspace-scoped session
instead unless `python3 -m workspace_state.startup terminals-settled` exits 0;
a crash, a timeout or a missing Python therefore count as unfinished. The helper
exits 1 (pending) while this login's startup is running, its terminals stage is
not final (`ready`, `degraded`, `failed`, `skipped` or `waiting`), the deadline
has not passed, and the restore can still run. That is the restore service once
the agent recorded it, otherwise the session agent. A unit counts as dead only
when `systemctl --user show -p ActiveState` says `inactive` or `failed`; a
timeout or any other answer means it may be running. A missing status is
settled; an unreadable one is pending while the agent or the restore service
may run. An agent restarted in the middle of a login starts no second restore when
the status records the launch or `systemctl --user show -p ActiveState` reports
the fixed service live; an unclear answer does not block the launch, and a
failed `systemd-run` next to a live service is only logged.
`bin/wsctl-startup-launch` and `bin/wsctl-startup-worker` remain as shims that
exit 1, so an older agent still running during an upgrade falls back to its
direct restore; the worker shim still forwards `--service-result` to the failure
report of a unit an older release started.

The GNOME session client starts only after the Wayland Shell service is active,
and it does not launch Alacritty or Chrome during compositor startup. Login
restore starts only after `graphical-session.target` is active,
`gnome-winctl` reports valid physical display identities with no active monitor
recovery, every saved workspace name exists, and the complete monitor/workspace
topology has remained unchanged through a short post-handoff settle interval.
The same readiness gate is enforced again inside the serialized startup
transaction, so neither terminal placement nor browser restoration can bypass
it. The restore service performs exactly one full transaction. It never automatically
replays a failed pass whose application windows may already be live.

Category problems are results, not errors. A category whose items partly failed
publishes `degraded` with the number of items that need attention and why; a
category whose job raised publishes `failed`. Either way `wsctl startup` exits 0
and the other categories continue. Only an orchestration failure exits non-zero:
an unreadable snapshot, the startup lock, unavailable GNOME shell or placement
capabilities, a failed `workspace` stage, or a cancelled or expired operation.
A repeated startup reconciles the categories again; the status document, not a marker, tells which stage is waiting for the compositor.
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
Verification identifies a conversation by its live client, wherever its pane is, and
a repeated startup needs no record of the first run. A client that is live but not
ready (it is busy, or shows a prompt) counts when this login already saw it live;
one that this run just started must be ready. A repeated run after the shared
deadline waits for what it starts on a bound of its own.
A conversation whose resume failed was not seen live, so every checkpoint
keeps its saved identity pending instead of a plain shell. A still-waiting
wrapper keeps the identity in its own pane.
Its restore runs in a transient user service. Every restored Alacritty or Chrome process
tree gets its own
graphical-session service with `ExitType=cgroup`, so finishing the bounded restore
service cannot kill an application's tmux client, renderers, or GPU process; the
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

Startup never creates, closes, navigates or regroups a Chrome tab or window.
Chrome's own session restore brings the windows back; wsctl reads them
(`capture` with transient tab and group IDs) and pairs each native window with a
saved window by an overlapping multiset of tab URLs. Windows with the same
navigation and one saved destination pair first; otherwise a pair needs the
strictly largest overlap from both sides, and the last window left on each side
pairs by elimination. Each paired window is placed, with its current tabs and
groups, at the saved workspace, physical monitor, geometry and state. A page
that loads, redirects or opens tabs during the restore does not block placement.
Only a native window that disappears or changes type or incognito state fails,
and only its own item.

Saved windows that stay unpaired, every window of a profile where nothing
pairs, and every window of a profile whose companion is not connected or is
outdated fail only their own items; the others are placed. Native windows
without a pair are left as they are. The HUD reports the verified count and
keeps each failure. `wsctl` temporarily activates a private marker tab in the
Chrome window, focuses it through `chrome.windows`, and observes the one active
marker-titled native window to obtain its exact stable `gnome-winctl` ID. That
existing window is moved and verified against the saved destination.

`wsctl browser reopen [--profile NAME] [--window ID ...] [--dry-run]` is the
only way to rebuild a missing window, and only a user starts it. It reads the
saved recipe and the live windows, and offers the saved regular windows that no
live window pairs with. It refuses at once, without waiting, while a restore
holds the restore lock; it never starts Chrome (open it first so that it
restores its own windows); it refuses a grouped window with "Open it in Chrome;
grouped windows are not reopened automatically", a window whose tabs overlap a
live window or when a live window of a saved profile has no pair, because it may
already be open, a window that holds a live claim, and a profile with an
interrupted workspace switch. Before creating a window
it rereads every live profile. A new window opens through the companion's
`recreate_window` action, is placed through the expectation mechanism, and stays
open if placement fails. The command changes neither the checkpoint nor the
login status document. `--dry-run` only names what would be reopened. Exit
status is 3 when any selected window could not be reopened.

`wsctl restore browsers` retries the pairing and placement with an already
connected companion and can place the paired windows again; it never creates a
window. Limit: the claim token derives from the saved tabs, so after a periodic
checkpoint changes them, an earlier claim on a window carries another token and a
manual restore of that window fails with "another token claims" until Chrome is
restarted or the claim is forgotten. A retry in the same Chrome session with an
unchanged recipe reuses an earlier placement claim,
including the claim of a window that `wsctl browser reopen` opened (it binds to
the native window ID and saved destination). Content a user changed in a paired
window after the login never blocks reopening another window. A retained
observation recipe is paired like an ordinary one outside startup.

The CLI requires companion protocol version 2 and the focus, identification,
`exact_url_restore` and `lazy_tab_restore` capabilities before it starts
placement. Companion 0.7.0 no longer has the manual `repair_restored_tabs`,
`inspect_original_window`, `recover_original_window` and `close_restored_window`
actions; an older companion that still has them works with this CLI, which never
calls them. Per-window login journals make a partial browser retry idempotent.
Periodic checkpoints capture Chrome read-only by window title once the login
verified its Chrome restore; a window that cannot be told apart from another by
title keeps its tabs and is marked placement-unknown. When a category's capture is
incomplete, the checkpoint keeps that category's last-good data and reports the
retention as partial state on stderr. A successful startup or explicit acceptance
of a complete capture in the same login permits later intentional closes to be
saved normally.
Full saves and tmux autosaves also refuse to replace a saved physical-monitor
layout with an explicit fallback output such as `None-1`; the existing checkpoint
remains available until the display driver recovers.

`gnome-winctl` exports window capture, stable-window placement, and sequential
expectation/status methods at `org.sagecat.GnomeWinCtl1`. `wsctl` uses its JSON
CLI as the integration boundary so the placement service remains independently
useful to other tools.
