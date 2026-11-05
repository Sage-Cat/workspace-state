# Integration tests

Run from the workspace-state checkout. Headless tests use temporary state and a
separate GNOME session. Complete power-off tests belong only in the explicitly
guarded disposable VM.

| Check | Command |
| --- | --- |
| Regression suite and companion protocols | `make check` |
| Lifecycle processes | `PYTHONPATH=src python3 -m unittest discover -s tests -p test_lifecycle_integration.py -v` |
| Native placement | `python3 tests/integration/run_headless.py --run --output /tmp/window-test` |
| Installed application companions | `python3 tests/integration/run_headless.py --run --companions --output /tmp/companion-test` |
| Real HUD startup behavior in isolated GNOME | `python3 tests/integration/run_hud_headless.py --run --output /tmp/hud-test` |

See [Testing](../../docs/testing.md) for prerequisites, isolation, exact checks,
evidence requirements, and the recorded validation results.

## Disposable VM

- [Setup and scale fixture](../../docs/testing.md#disposable-ubuntu-vm-at-desktop-scale):
  `run_vm_scale.py`, synthetic browser data and conversation workers, real signed-out apps.
- [Real power-off and the bounded final save](../../docs/testing.md#real-power-off-and-the-bounded-final-save):
  `run_vm_poweroff.py prepare`, `watch`, a real power-off, then `verify` after
  boot. Acceptance is three clean cycles (GNOME menu or `gnome-session-quit
  --power-off --no-prompt`, unprivileged `systemctl poweroff`, `sudo shutdown
  now`) plus one `prepare --abrupt` cycle (`systemctl poweroff --force` or QMP
  `quit`). A clean cycle needs exactly one final save, finished within its
  budget and before the first application unit stop, no `CancelEndSession`, no
  `shutdown-*.json`, and a restored snapshot with `source: "final-save"`. The
  abrupt cycle needs no final save and a periodic `auto-save` snapshot no older
  than five minutes plus a minute of tolerance.
- [Upgrade without manual save](../../docs/testing.md#retained-checkpoint-upgrade-without-manual-save):
  `run_vm_upgrade.py`, retained old recipe and exact newer native session.
- Native pending continuation: `run_vm_pending.py configure` arms next-boot
  loopback pages. Responses remain gated until three real Chrome restore calls
  return pending; `verify` checks the initial receipt, later content and placement,
  operation ownership and actual HTTP wait. A short delay cannot pass this check.
- Ordinary next cycle: `run_vm_ordinary.py --disposable-guest prepare`, a real
  power-off watched by `run_vm_poweroff.py`, cold boot, then `verify`.
  This observer neither mutates the fixture nor writes the canonical checkpoint.
  For a planned normal release upgrade, bind `--expected-release r-…` during
  preparation; verification requires that exact predeclared release after boot.
- [Pending content followed by placement](../../docs/testing.md#delayed-content-and-placement-2026-10-05):
  `run_vm_pending.py`, responses gated until native restoration returns pending.
- [Validated scenarios and limits](../../docs/testing.md#recorded-validation-2026-10-03):
  negative baseline, final cold boots and delayed HTTP (dated, from the retired
  shutdown transaction).

Do not run the VM setup or shutdown phases on a working desktop. They require
the harness's named KVM guest and explicit fixture consent. Keep runtime logs,
profiles and per-run identities outside Git. Missing dependencies are recorded
as skips, never as passing integration tests.
