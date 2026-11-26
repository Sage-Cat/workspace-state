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
workspaces. Unrecognized windows, ambiguous identities, unsaved Code edits and
modal dialogs can require attention before a space can close. Native guarded
close requires Wayland clients.

Archives live in the user's private Workspace State data directory. The
five-minute checkpoint, ordinary login and final shutdown capture preserve
parked workspaces. A partial transition retains the archive and its retry
direction. Save or cancel application prompts yourself. To reverse an
interrupted close, choose **Keep … on and restore closed windows**. When the
screen is unlocked, monitor recovery continues to use the existing GNOME
window controller.

## Chrome

The companion marks each selected regular Chrome window immediately before it
closes. On return it finds that exact window in Chrome's recently closed list
and asks Chrome to reopen it. A window on a different workspace can contain
identical URLs and still remain independent. The companion needs the Chrome
`sessions` permission for this operation. If Chrome asks for approval after
updating, open `chrome://extensions` and enable **Workspace State Companion**
in the affected profile. Run `wsctl deployment doctor` to compare the
installed and running versions.

Chrome retains at most 25 recently closed entries per profile. If the marked
window has expired, Workspace State rebuilds its tabs and groups in a new
window from the archive. Archived URLs, order, pinning, selected tab, group
membership, names, colors and collapsed state are recoverable. Browser-generated
group IDs may change. Page history, form state, and in-page application memory are not part
of the URL archive. Even native window restore may omit the Back history of a
tab in a saved Chrome group. Private windows require separate handling and
are left open.

[Chrome sessions API](https://developer.chrome.com/docs/extensions/reference/api/sessions)
