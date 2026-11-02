# Workspace State

Save and restore a GNOME Wayland desktop: application windows, workspaces,
monitors, browser tabs and terminal layouts. Requires Python 3.11+ and GNOME 46.

![Restoration progress](docs/screenshots/restoration.png)

Native HUD screenshot with example data. [More screenshots](docs/visual-guide.md).

- Restore Chrome, Alacritty/tmux, VS Code, Nemo and supported social apps.
- Save a checkpoint before confirmed shutdown; recover prepared jobs on cancel.
- Keep the last good checkpoint when a new capture is incomplete.
- Show restoration progress and reported application problems in Login HUD.

## Install

Use [Desktop Workspace](https://github.com/Sage-Cat/desktop-workspace) for a
recursive clone of the tested component versions. Separate sibling checkouts
remain supported by [the release manifest](config/desktop-release.toml).
Review [setup requirements](docs/reference.md#install) before installing;
optional host integrations need local configuration.

```sh
make check
make install
```

Installation is scheduled for the next graphical login. Chrome also needs its
[companion extension in each managed profile](docs/reference.md#install).

## Use

```sh
wsctl save                 # replace the saved desktop state
wsctl show --details       # inspect it
wsctl restore --dry-run    # preview restoration
wsctl restore              # restore saved applications and placement
wsctl deployment doctor   # compare source, installed and running versions
```

Save records the currently observed desktop. Windows, panes, workspaces and
application entries absent from a complete capture are therefore treated as
closed and disappear from the next checkpoint. When a provider reports an
incomplete capture, an explicitly allowed partial save keeps that provider's
last-good data instead. Codex and Claude restoration can append only conversations with
verified identities to a partial live layout, while preserving its existing
panes; Chrome reuse likewise requires an exact, complete catalog with matching
placement, so an overlapping or ambiguous duplicate is never enough.
Claude identity capture requires the installed `SessionStart` and `SessionEnd`
hooks in the owner-managed Claude settings; see
[terminal restoration](docs/terminal-restoration.md#conversation-identity).

A waiting placement is not a completed restore. Check the HUD details before
retrying or replacing a saved checkpoint.

During login restoration, **Run in background** hides the HUD while work
continues. Reopen it from the top-bar status indicator. Tab and Enter navigate
the bounded panel without grabbing the desktop.

**Abort restoration** requests a cooperative stop for the current startup.
Already restored applications and the saved checkpoint stay intact. Chrome
acknowledges the exact startup context; pending native placements and queued
queued conversation launches stop, while already running clients remain open. The
HUD reports a stopped, partial restoration rather than completion. A partial
startup does not arm automatic checkpoint replacement.

Chrome returns window claims promptly and verifies tab URLs and groups
asynchronously from one native sample. Identification leases keep temporary
mapping markers isolated from content verification and competing activation.
Read-only observations remain available for later checkpoint and shutdown work.

## Repositories

![Repository dependencies](docs/repository-dependencies.svg)

Solid arrows show runtime integration; dotted arrows show packaging only.
[PlantUML source](docs/repository-dependencies.puml) · [Checkout layout](docs/repositories.md)

## Documentation

- [Usage and screenshots](docs/visual-guide.md)
- [Configuration and commands](docs/reference.md)
- [Terminal names and conversation recovery](docs/terminal-restoration.md)
- [Deployment and rollback](docs/deployment.md)
- [Report problems to the HUD](docs/owned-system-alerts.md)
- [Architecture](docs/architecture.svg) · [PlantUML](docs/architecture.puml)
- [Shutdown sequence](docs/shutdown-flow.svg) · [PlantUML](docs/shutdown-flow.puml)
- [Testing method and results](docs/testing.md) · [Test commands](tests/integration/README.md)
- [Releases](docs/publication.md)
