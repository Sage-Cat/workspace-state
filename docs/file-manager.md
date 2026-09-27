# Default file manager (Nemo)

`wsctl save` captures Nemo alongside terminals, browsers and social apps.
The optional `file_manager` category in the private canonical checkpoint stores
each window's ordered folder URIs, active tab, named workspace, physical display,
frame geometry and normal/maximized/fullscreen/minimized state. Old checkpoints
without this category and checkpoints with no Nemo windows launch nothing.
The existing HUD renders **Default file manager** with expandable activity logs
during startup and a separate checkpoint row during shutdown.

## Installation

On Ubuntu install `nemo-python` and `gir1.2-nemo-3.0`, then:

```sh
make install-file-manager
```

The target checks the dependencies and installs the provider at
`~/.local/share/nemo-python/extensions/wsctl_nemo_bridge.py` (under `PREFIX`
when overridden). Normal `make install` includes this target. The provider loads
inside Nemo at its next launch; an already running Nemo must be closed/reopened
normally to load an update. The installer never closes user windows, changes the
default file manager, restarts GNOME or installs a persistent background service.

## Exact capture, not title guessing

Nemo's Python LocationWidgetProvider receives actual folder URIs. Invisible
provider widgets identify the URI belonging to each Gtk notebook tab. A small
session-bus bridge at `org.sagecat.WorkspaceState.Nemo1` exposes that state.
During capture a random title lease correlates a GTK window with its exact
GNOME ID and Nemo PID. Titles are restored in `finally`, or after three seconds
if the caller disappears; navigation/title changes are never overwritten.

Restoration matches existing windows by exact ordered folder lists, claims each
window at most once, and creates only missing windows. It selects the active tab
and verifies live placement after Wayland resize processing, including staging
on the active workspace before handing the window to an inactive workspace.
Repeating restoration therefore does not duplicate correctly restored windows.

The category is currently Nemo-specific. If the default directory handler has
changed, restoration reports that conflict instead of launching a different file
manager. Split-pane views or incompletely loaded tabs fail capture explicitly;
their folder paths are never guessed. URI validation rejects embedded passwords,
control characters, non-directory launch targets and unsupported schemes.

Storage-backed folders defer until after `wsctl-workspace-restored.target`
starts the configured drives, before metadata warmup. The `.deferred` marker
breaks the otherwise circular dependency between folder restoration and mounts.
Known `/home/sagecat/Drives` paths require both the matching active service and
an actual mountpoint before any directory probe or launch. Unavailable folders
produce a visible error, not an empty local directory or repeated launch loop.
Other categories and metadata warmup can still finish.

## Verification and manual retry

```sh
wsctl restore file-manager --dry-run
wsctl restore file-manager
```

The dry run validates the saved recipe without invoking Nemo, its bridge, or
window placement. The second command affects only saved file-manager windows.

Automated tests cover legacy snapshots, validation, duplicate-window matching,
bridge lifetime/leases, mount gates, partial errors and startup dependencies.
Live verification on 2026-09-13 used three disposable folders in two Nemo
windows, one with two tabs: moved windows to separate displays, selected the
saved active tab, closed/reopened only those test windows and repeated restore
to prove window reuse. Minimized and maximized states were also tested on an
inactive workspace, including capture of minimized windows. No production checkpoint or other application's state
was replaced, and no OS shutdown/logout was used.
