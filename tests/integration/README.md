# Integration tests

Run from the workspace-state checkout. Headless tests use temporary state and a
separate GNOME session. Complete shutdown tests belong only in the explicitly
guarded disposable VM.

| Check | Command |
| --- | --- |
| Regression suite and companion protocols | `make check` |
| Lifecycle processes | `PYTHONPATH=src python3 -m unittest discover -s tests -p test_lifecycle_integration.py -v` |
| Native placement | `python3 tests/integration/run_headless.py --run --output /tmp/window-test` |
| Installed application companions | `python3 tests/integration/run_headless.py --run --companions --output /tmp/companion-test` |
| Real HUD behavior in isolated GNOME | `python3 tests/integration/run_hud_headless.py --run --output /tmp/hud-test` |

See [Testing](../../docs/testing.md) for prerequisites, isolation, exact checks,
evidence requirements, and the recorded validation results.

## Disposable VM

- [Setup and scale fixture](../../docs/testing.md#disposable-ubuntu-vm-at-desktop-scale):
  `run_vm_scale.py`, synthetic browser data and conversation workers, real signed-out apps.
- [Real GNOME power-off](../../docs/testing.md#real-gnome-power-off-and-browser-adoption):
  `run_vm_poweroff.py prepare`, `watch`, real console confirmation, then `verify` after boot.
- [Upgrade without manual save](../../docs/testing.md#retained-checkpoint-upgrade-without-manual-save):
  `run_vm_upgrade.py`, retained old recipe and exact newer native session.
- [Validated scenarios and limits](../../docs/testing.md#recorded-validation-2026-10-03):
  negative baseline, three final cold boots, delayed HTTP, cancellation and retry.

Do not run the VM setup or shutdown phases on a working desktop. They require
the harness's named KVM guest and explicit fixture consent. Keep runtime logs,
profiles and per-run identities outside Git. Missing dependencies are recorded
as skips, never as passing integration tests.
