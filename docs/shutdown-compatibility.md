# Ubuntu shutdown compatibility

This opt-in OS integration handles specific service teardown compatibility
issues separately from HUD/preflight profiles. It does not suppress console or
journal output, disable services, or weaken checkpoint cancellation.

## Corrections and boundaries

| Component | Correction |
| --- | --- |
| GDM 46 | Order after AccountsService, logind and system D-Bus so GDM stops first; weakly want AccountsService. Exit 1 remains a failure. |
| GTK portal / Snap desktop integration | Bind to `graphical-session.target` and order after that target and the Wayland Shell so helpers stop before display teardown. Exit-status checks remain unchanged. |
| GPG SSH socket | An inert cleanup helper stops before the socket and user D-Bus. `PartOf` propagates explicit socket stop/restart; socket `Wants` re-arms cleanup on activation. Socket start hooks remain unchanged. |
| CUPS snap | Order after its CUPS server and Avahi; recognize only the wrapper's TERM-equivalent status 143 as clean. Other failures remain visible. |
| Livepatch v11.0.2 | Allow main exit 1 only with a mandatory `ExecStopPost` validator proving PID 1 is stopping and finding a fresh exact completion message from the same unit, invocation and boot. Missing or ambiguous evidence and runtime exit 1 fail through the stop hook. |

The Livepatch rule is not a blanket exit-1 success allowance. Systemd 255 applies
`SuccessExitStatus` to control commands too, so the mandatory shell wrapper maps
every validator failure, including interpreter errors, to exit **2**. Never
install the exit-1 allowance without its validator and exit-2 wrapper.

## Install and inspect

```sh
make check-shutdown-compat
make install-shutdown-compat-user
make install-shutdown-compat-system  # native pkexec authorization dialog
```

The normal installer does not request root or silently enable this host-specific
integration. Fixed-name drop-ins leave vendor files untouched. The installer
atomically copies definitions, backs up differing prior drop-ins, reloads unit
definitions and arms inert GPG cleanup only if its socket is already active.
It never restarts the desktop, socket, agent or system services. Repeated
installation is idempotent. The system validator is root-owned mode 0755 under
`/usr/local/libexec`, rather than executed from a mutable checkout.

Installation destinations are listed in `scripts/install-shutdown-compat.py`.
Removal must restore the GPG socket hooks and exported SSH environment together:
simply stopping cleanup would clear that environment while the socket is running.
Do not stop GDM or the SSH socket just to deploy definitions.

## Validation and limits

Repository tests cover invocation, unit, message and freshness matching; missing
or malformed evidence; runtime failures; bounded journal queries; safe
destinations; backup preservation; and idempotency. The optional
`python3 -I scripts/test-shutdown-compat-live.py` uses disposable user services to
check rejection of runtime exit 1 and acceptance of the narrow status-143 rule.
It does not perform a shutdown. Service versions and ordering must be checked on
the target system; unrelated service or hardware faults remain visible.
