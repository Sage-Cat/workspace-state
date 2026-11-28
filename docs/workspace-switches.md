# Workspace switches

**Workspaces** in the GNOME panel lists the unique, explicitly named slots in
a static workspace configuration, in the canonical order, including the ones
that are off. The same list is `wsctl workspace list --json`; switching is
`wsctl workspace on|off -- NAME`.

A workspace that is off is removed from GNOME: it disappears from the
overview, from `num-workspaces` and from `workspace-names`. The canonical order
of the names (for example Alpha, Bravo, Charlie, Delta, Echo) is kept in `workspaces/workspace-order.json` beside the archives. It
starts from the names GNOME has and puts a new name after its predecessor in
GNOME. Turning on inserts the workspace where the order puts it: at the index
that equals the number of present names that come before it. Only
workspaces the user named (`named: true`) enter the order; GNOME's own
"Workspace N" never does. The first named workspace of the order is the primary one: it is always on, `wsctl workspace
off` refuses it before any change ("NAME is the primary workspace and always
stays on"), `list --json` marks its row with `"primary": true`, and no cleanup
ever removes it. The last workspace cannot be switched off either. Login does not create the workspaces that are
off. Windows of other workspaces are bound by their workspace name, so the
index shifts of a removal or insertion change nothing for them. A removal or
insertion waits while another workspace has an unfinished switch, because its
retry compares window indices.

Removal and insertion need the `edit_workspaces` capability of gnome-winctl.
Without it a switched-off workspace stays in GNOME, as before. An older
release left switched-off workspaces in GNOME: repeating `off` removes such an
empty workspace, and so does any other Off or On while it holds its
locks. The end of the next login does so for all of them: it waits up to 30
seconds for the locks and for pending restoration results, and writes a
diagnostic for everything it skips. GNOME refuses to edit workspaces while
window placements are pending (`placements_pending`, printed as JSON with exit
code 1). The edit a command exists for is retried for up to 20 seconds, then
the command ends with exit code 3 and asks to retry. The cleanup of other
workspaces and the login cleanup make one attempt and stop with a diagnostic. The cleanup runs once in every run of the login finalizer, also when the
restoration has already ended the operation, writes a diagnostic for what it
removed or skipped and never changes the login result. `wsctl workspace forget --yes`
of a removed workspace puts an empty one back first.

Turning off captures the workspace, checks that every known window has a
recovery recipe, then requests each exact window to close. Shared browser
processes and tmux servers can continue serving other workspaces. A terminal
client detaches from its saved tmux session. Background processes can continue
running after their last window closes. Sticky windows remain available to all
workspaces.

## Known and other applications

Only known applications are archived and restored: Alacritty (with tmux and
Agent Deck dashboards), Chrome, VS Code, Nemo, the desktop apps from
`desktop-apps.toml` and Trilium Desktop. A known window that cannot be captured
or verified blocks turning off; a known program is never closed in silence.
A known program running as an X11 client (Alacritty, Chrome, VS Code, Nemo)
blocks as well.

Every other window is closed without a recipe and is not restored. That
includes any X11 client, also a configured app such as Steam, because an X11
window can be neither archived nor identified safely, and a dialog of such a
window. With `close_x11_window` gnome-winctl closes an X11 window on request
(`--allow-x11`) and refuses when the same client shows a window on another
workspace, because Mutter's close can fall back to killing the client. A
dialog is told by its `transient_for` parent, not by process, and is not asked
to close separately: with the `close_transient` capability one request with
`allow_transient` goes to the root of each family and GNOME closes the whole
family; a family with any window of a known application is never closed and
blocks turning off. A new gnome-winctl also lists `other_windows` (dialogs and other windows its
normal list lacks) and the `blockers` of each workspace: they are named with
their kind and title and count for the occupation of an application's scope.
The extension refuses to close on a workspace with such a window (a dialog of
a type other than normal), so each one blocks turning off before any change
and has to be closed by you first. Without the capability, or when GNOME refuses a member
(for example a dialog-type window), turning off fails naming the window. Closing runs first,
with a fresh check of each window before its request. If one stays open (it
may be asking to save), turning off fails with the window named, no scope is
signalled and no known window has been closed yet.

Only when no window of the application is left anywhere (nor one GNOME cannot
list), its own scope is ended, for an application that has left tray processes
behind (Steam stays in the tray). The scope is sent SIGTERM, and only when all
of these hold: it is an `app-*.scope`, a
`snap.*.scope` or wsctl's own `wsctl-app-social-<app>-*.service` launch unit of a
configured app, in the user manager (never another service, a session scope,
GNOME Shell, D-Bus or another wsctl unit such as an Alacritty window); a family with a window on another workspace (Mutter puts an unattached child on the active one) blocks too, naming the window and its workspace; no window on any other workspace and no known
window belongs to a process of the same cgroup; no tmux server runs in it; and
it still has processes. There is no SIGKILL and no `systemctl stop`. The
archive keeps the list of the closed applications for reference only.

Trilium Desktop is known, and each of its windows is a separate recipe that
remembers the notebook directory of its process (`TRILIUM_DATA_DIR`, the only
variable read). Windows are started one after another, each with
`trilium-repo DIR`, or the plain launcher when no directory was saved and no
such Trilium runs (otherwise it is skipped with a note). A second window of a
notebook that already runs is started with `--new-window`, because Trilium
otherwise only focuses the running one. Each window is bound to the saved
target only if it did not exist before the launch and its process shows the
same notebook; an existing window is never moved. The launch unit stops
gracefully at logout (SIGTERM to the whole group, SIGKILL only after 30
seconds). Turning on restores Trilium after the workspace is on, and a Trilium
that cannot be restored is a note, never a workspace left in `opening`.

Without `close_x11_window` (an older gnome-winctl) turning off behaves as
before: an X11 window blocks and the message asks for the update.

When something blocks turning off, the message lists every cause, naming the
workspace and the window (VS Code folder or title) plus the tab or terminal.
It is cut at 240 characters and ends with `+N more` for the rest. Causes are:

- a window GNOME cannot close or archive: a dialog of a known window, a
  minimized window with no recipe, a known window that cannot be captured, or
  an ambiguous one;
- windows GNOME cannot list (counted, for example dialogs);
- a modal dialog;
- a VS Code window with unsaved tabs, an untitled tab with content, or a
  terminal with a running command (see below);
- Chrome tabs that cannot be reopened by URL.

Save or cancel application prompts yourself.

Archives live in the user's private Workspace State data directory. The
five-minute checkpoint, ordinary login and final shutdown capture preserve
parked workspaces. An unreadable archive pauses only its own workspace: `list`
shows it as `partial` with the workspace name, ordinary login leaves that
workspace's windows alone, and every other workspace keeps working.
The archive does not say which Chrome profiles or VS Code data directories its
windows used, so for a workspace paused this way those are not protected from
native session replay: Chrome or VS Code can bring its parked windows back on
login. Turning on is not possible for an unreadable archive, so close such
windows yourself; `forget` ends the pause but does not prevent a replay.
`wsctl workspace forget --yes -- NAME` discards the archive. `on` can no longer
reopen the windows saved in it, and items the ordinary checkpoint still lists as
pending are no longer hidden, so they return at the next login unless dropped
with `wsctl forget KEY`. Without `--yes` the command only says what it would
discard. A partial transition retains the archive and its retry
direction. Save or cancel application prompts yourself. To reverse an
interrupted close, choose **Keep … on and restore closed windows**. When the
screen is unlocked, monitor recovery continues to use the existing GNOME
window controller.

When an archived workspace shares a Chrome profile or VS Code data directory
with an active workspace, cold startup opens only the active recipes. For
Chrome, the profile starts with its own session restore, which holds only the
windows of workspaces that are on, and Workspace State places those windows at
their saved workspace and display instead of rebuilding them from URLs. A
profile that has no window of an enabled workspace, or whose switch did not
finish, starts without a startup window so Chrome cannot replay a closed
window; its windows stay in the archive. A native window that cannot be paired
with the saved placement fails only its own item: it is never rebuilt from URLs
or closed, and `wsctl browser reopen` can open a saved regular window that is
missing. If VS
Code's `window.restoreWindows` is `preserve`, it can reopen parked projects on
its own. Workspace State defers a cold launch in this configuration and shows
the reason.

## VS Code

Turning off is blocked only by unsaved tabs, an untitled tab with content, and
a terminal with a running command. Diff and preview editors, settings, chat and
other webviews do not block; they are closed with the window and VS Code is
free to restore them or not.

Turning on a window that shows a folder starts VS Code with `--user-data-dir`,
`--profile` when it is not the default, and `--new-window --folder-uri <folder>`,
with no file argument, so VS Code restores that window's own editors, including
diffs and Markdown previews (webview state may be lost). Workspace State accepts
only a window that did not exist before the launch and shows exactly that
folder. It does not launch when the folder is already open in another window,
because `code` would focus that window and the switch would move it, or when
GNOME cannot list the current windows; the item needs attention. Closing that
window fixes it; moving it to the target workspace helps only when it shows
exactly the saved tabs and has no unsaved work. After a short quiet period it
reopens only plain text files that VS Code did not restore, so no tab is opened
twice.

The launched window is noted in a private file in the runtime launch directory,
keyed by the switch token and the saved window, as soon as the window is
identified. If that On then does not finish (failed placement or tab reopening,
or a deadline after the window was identified), a retry takes the same window
back while it still exists with the same window ID and companion instance,
instead of treating it as someone else's; the note is removed when the window
is verified. If the deadline passes before the window or its companion shows
up, nothing is noted and a retry reports the folder as already open. Apart from
the noted window, only a window on the target workspace that shows exactly the
saved tabs and has no unsaved work is reused; any other window with the folder
is refused.

Windows without a folder, and `.code-workspace` windows, still start with a
scoped launch marker file that is closed afterwards. Login restore never uses a
marker.

A local terminal blocks when its root process is not an interactive shell
(`ssh`, `python`, a task), when a shell integration command is running, or when
the shell has any child process, including a background or stopped job. A tmux
client (a process named exactly `tmux` or `tmux: client`), as the root process
or under the shell, does not block, because the session lives in the tmux
server; a tmux server run in the terminal, or a running shell integration
command, still blocks. Without shell integration the process checks decide alone.
Terminals of a remote window stay conservative: they always block and the
message names the terminal, as does a terminal without a process ID or one
that cannot be inspected. A helper process of the prompt can
cause a false block; close or detach that terminal.

The window must run the current companion. After updating it, reload each open
VS Code window with **Developer: Reload Window**; until then the old companion
keeps its stricter rule (any unsaved edit or non-file editor blocks).

## Chrome

The companion marks each selected regular Chrome window immediately before it
closes. On return it finds that exact window in Chrome's recently closed list
and asks Chrome to reopen it. A window on a different workspace can contain
identical URLs and still remain independent. The companion needs the Chrome
`sessions` permission for this operation. If Chrome asks for approval after
updating, open `chrome://extensions` and enable **Workspace State Companion**
in the affected profile. Run `wsctl deployment doctor` to compare the
installed and running versions.

Chrome exposes up to 25 recently closed entries per profile. If the marked
window is no longer available, Workspace State rebuilds its tabs and groups in a new
window from the archive. Archived URLs, order, pinning, selected tab, group
membership, names, colors and collapsed state are recoverable. Browser-generated
group IDs may change. Page history, form state, and in-page application memory are not part
of the URL archive. Even native window restore may omit the Back history of a
tab in a saved Chrome group. Private windows require separate handling and
are left open.

Turning off needs every tab to be reopenable by its URL: `http:`, `https:`,
`file:`, `about:blank` and `chrome://` pages (settings, extensions, history and
the like) qualify. `data:`, `blob:`, `devtools:`, `view-source:`,
`chrome-extension:` and similar pages, and `chrome://` pages that act on the
browser (`crash`, `kill`, `quit`, `restart`, `hang`), do not; the message names
their schemes so you can close those tabs first.

[Chrome sessions API](https://developer.chrome.com/docs/extensions/reference/api/sessions)
