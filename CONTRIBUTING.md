# Contributing

`workspace-state` targets native GNOME Wayland and treats shutdown authorization
as a fail-closed protocol. Keep desktop placement in `gnome-winctl`, rendering
in Login HUD, and orchestration in `wsctl`.

Run the standalone checks before submitting a change:

```sh
make check
```

When the sibling `gnome-winctl` and `login-hud` repositories are available, run
the complete local integration suite:

```sh
make test
```

Shutdown changes must preserve operation/session binding, exact systemd
invocation verification, HUD cancellation before final GNOME `EndSession`, and
rollback of every application state mutation. Never add cloud-drive, GPU, or
GNOME teardown to a shutdown profile.
