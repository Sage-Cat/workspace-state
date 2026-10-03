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
| Remmina snap SSH agent | Before the vendor service starts, check only `ssh-agent.socket` in its numbered revision's working directory. Remove it only after private socket/type/owner checks, no kernel binding, `ECONNREFUSED`, and unchanged inode metadata plus a second kernel check. Active, bound, replaced, symlinked, or uncertain paths are preserved and startup remains failed. |

The Livepatch rule is not a blanket exit-1 success allowance. Systemd 255 applies
`SuccessExitStatus` to control commands too, so the mandatory shell wrapper maps
every validator failure, including interpreter errors, to exit **2**. Never
install the exit-1 allowance without its validator and exit-2 wrapper.

The Remmina hook fixes a stale pathname left after an agent dies without
unlinking its socket. A leftover pathname alone causes the next bind to fail
with `EADDRINUSE`; it does not prove an agent is still running. Conversely,
`ECONNREFUSED` alone is insufficient because a live bound socket need not be
listening yet. The hook requires both forms of evidence, never sweeps another
revision, and leaves the vendor command and failure statuses unchanged.

Workspace-state graphical app launches also carry a package-pinned `ExecStop`
helper. Chromium can migrate its main process to a separate application scope
while leaving renderers in the launching service. With `KillMode=mixed` and no
main PID in that service, stopping it would immediately kill those renderers
before the real application main receives TERM. The helper first proves the
migrated main from a direct retained-child relationship, matching executable
inode and UID, process start ticks, and the exact PID-named Chromium scope.
It pins that PID with a pidfd, rechecks the ownership evidence, sends TERM, and
waits at most four seconds before the normal service teardown continues.
Ambiguous ownership and an unresponsive main remain visible failures. This
does not extend `user@.service` deadlines or signal an unrelated application;
it applies to newly launched workspace-state app units.

## Install and inspect

```sh
make check-shutdown-compat
make install-shutdown-compat-user
make install-shutdown-compat-system  # native pkexec authorization dialog
# Or install only Remmina's fixed helper/drop-in pair, from a verified release:
pkexec /usr/bin/python3 -I /path/to/sealed/workspace-state/scripts/install-shutdown-compat.py --system --component remmina
```

The normal installer does not request root or silently enable this host-specific
integration. Fixed-name drop-ins leave vendor files untouched. The installer
atomically copies definitions, backs up differing prior drop-ins, reloads unit
definitions and arms inert GPG cleanup only if its socket is already active.
It never restarts the desktop, socket, agent or system services. Repeated
installation is idempotent. The system validator is root-owned mode 0755 under
`/usr/local/libexec`, rather than executed from a mutable checkout.
The desktop release seals the integration sources and installer without adding
root installation bindings. `--component remmina` copies only its helper and
drop-in, then reloads definitions; it does not run the helper or repair the live
socket. Repair takes effect on the vendor service's next authorized start.

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
