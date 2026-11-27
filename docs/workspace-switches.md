# Workspace switches

**Workspaces** in the GNOME panel lists the unique, explicitly named slots in
a static workspace configuration. Their positions stay fixed when a slot is
turned off. The same control is available with `wsctl workspace list --json`
and `wsctl workspace on|off -- NAME`.

Turning off captures the workspace, checks that every eligible window has a
recovery recipe, then requests each exact window to close. Shared browser
processes and tmux servers can continue serving other workspaces. A terminal
client detaches from its saved tmux session. Background processes can continue
running after their last window closes. Sticky windows remain available to all
workspaces.

When something blocks turning off, the message lists every cause, naming the
workspace and the window (VS Code folder or title) plus the tab or terminal.
It is cut at 240 characters and ends with `+N more` for the rest. Causes are:

- a window GNOME cannot close or archive: an X11 client, a dialog, a minimized
  window with no recipe, or an unrecognized or ambiguous window;
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
window; its windows stay in the archive. If the native windows cannot be paired
with the saved placement, they are rebuilt from the saved tabs and unclaimed
windows of the restarted profile are closed as duplicates. If VS
Code's `window.restoreWindows` is `preserve`, it can reopen parked projects on
its own. Workspace State defers a cold launch in this configuration and shows
the reason.

## VS Code

Turning off is blocked only by unsaved tabs, an untitled tab with content, and
a terminal with a running command. Diff and preview editors, settings, chat and
other webviews do not block; they are closed with the window and VS Code is
free to restore them or not. Only plain text files are reopened by
Workspace State.

A terminal is judged by its shell integration events. Without shell
integration, a local terminal is judged by its child processes. Otherwise,
including remote windows and terminals that already ran before the companion
started, it blocks and the message names it.

A checkpoint saved by an older companion verifies against the current one. The
first login after the update can still report an empty editor window that held
files as not recovered, because its identity then used the older editor list.

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
