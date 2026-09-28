# Integration tests

Run these from the workspace-state checkout. They use temporary state and do not
restore or shut down the active desktop.

## Lifecycle processes

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -p test_lifecycle_integration.py -v
```

Checks operation ownership, stale publishers, cancellation, concurrent writes,
login generations and preservation of pending shell input. Requires Python,
Bash and tmux; the tmux test uses a separate socket.

## Native window placement

```sh
python3 tests/integration/run_headless.py --run --output /tmp/window-test
```

Checks three virtual monitors, resizing a maximized window before an inactive
workspace handoff, verified placement, deferred placement and cancellation.
Requires GNOME Shell 46 with headless Wayland support, `gdbus`,
`dbus-run-session`, `glib-compile-schemas`, Python GTK 3 bindings and the sibling
`gnome-winctl` checkout.

## Application companions

```sh
python3 tests/integration/run_headless.py --run --companions --output /tmp/companion-test
```

Adds Chrome, VS Code and Nemo checks where installed. Each uses temporary
profiles and generated content. Missing or unsupported dependencies are recorded
as skips, not passes.

## HUD cancellation

```sh
python3 tests/integration/run_hud_headless.py --run --output /tmp/hud-test
```

Checks real modal grabs, cancellation, late status, countdown and enable/disable
behavior. Requires sibling `login-hud` and `gnome-winctl` checkouts. The fixture
blocks native shutdown handoff.

## Results and isolation

- Each native run uses its own session bus, XDG directories and Wayland socket.
- `HOME` is preserved; no existing application profiles are imported.
- Deadlines and process-group cleanup bound each run.
- `results.json` records checks and failures; logs stay in the output directory.
- Virtual monitors do not test physical GPU or hotplug behavior.

[Lifecycle design](../../docs/lifecycle-architecture.md)
