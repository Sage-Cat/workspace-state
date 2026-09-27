# Desktop releases

For pinned public components, use [Desktop Workspace](https://github.com/Sage-Cat/desktop-workspace).
Its `make stage` and `make install` first verify the submodule commits and clean
worktrees, then pass a public-component manifest to this installer. The standalone
commands below retain their existing sibling-checkout behavior.

`wsctl deployment` packages the current files in the sibling desktop repositories,
including uncommitted changes. It does not require commits, move a checkout, or
collect application profiles, logs, snapshots, `.git`, or `node_modules`.
`config/desktop-release.toml` is the explicit component/file/install inventory.
Optional components can use absolute source directories for maintained host code.

```
make install                    # stage and schedule one production release
wsctl deployment stage          # stage only; no installation change
wsctl deployment schedule r-<24 hexadecimal digits>
wsctl deployment doctor
```

Scheduling does not switch this session's installed helper paths. For existing
immutable installations that keeps the old release coherent. During the initial
migration, legacy source-linked helpers already see checkout edits; scheduling
cannot undo that exposure. A new shutdown worker refuses legacy authority before
checkpoint/profile mutation, and the old coordinator reports the refusal so the
HUD releases input. Cleanup without a journal is a no-op. Use a normal logout
and login for this first activation, rather than trying to finish a mixed-version
shutdown transaction. No legacy receipt is promoted into new authorization.

A private `pending-install.json` receipt binds the release, optional
host profile and installation roots. The immutable staged helper runs from
`wsctl-release-activate.service` before the next Shell/coordinator starts. Shell
and coordinator drop-ins use `Wants` plus ordering, so installer failure cannot
prevent graphical login. There is no `RemainAfterExit`: another login on the same
boot can apply a later schedule. Failure preserves the prior release, retains
retry diagnostics, and lets the desktop start. A live old Shell/coordinator causes
deferral; `activating` with `MainPID=0` is permitted before startup.

After publishing a release, the helper performs a bounded user-manager
`daemon-reload` before marking the receipt applied. A reload failure is reported
as `installed-pending-reload`; its retry only reloads definitions and preserves the
previous rollback release. Doctor distinguishes scheduled, installed and running
builds. Schedule the doctor-reported previous revision to roll back safely at the
next login. Immediate CLI `install`/`rollback` reject a live desktop/coordinator;
the Python filesystem APIs remain available for explicitly isolated/offline roots.

Production `make install` requires the default `PREFIX`; a nondefault prefix is
rejected before any writes. `make install-dev` is the explicit legacy source-link
and host-activation workflow. Component-specific legacy Make targets remain
explicit development/host helpers, not part of the production release switch.

The source-checkout entry point is `scripts/desktop-release`; it accepts the same
subcommands without the `deployment` prefix. `--manifest PATH` and `--source-root
PATH` precede the subcommand. Staging checks the source twice and rejects a mixed
generation. It records each component's Git HEAD, actual file digests, executable
bits, minimum protocol/capabilities, and import-time build stamp. A dirty checkout
therefore produces a different release even when HEAD did not change.

Releases live at `$XDG_DATA_HOME/workspace-state/desktop-releases/r-…` (default
`~/.local/share`). They are sealed read-only and checked against their file
inventory before installation. `current` and `previous` are atomic symlinks.
Normal executable/extension links resolve through `current`. Chrome uses a real
stable unpacked directory containing per-file release links, so its registered
path survives a release switch. An existing directory symlink is backed up without
moving or modifying its source checkout. The final pointer
switch publishes the packaged components together. No source checkout is needed
for installed execution. Original owned paths are retained in a transaction
backup directory, with the path mapping in `installation.json`. A user replacement
of a previously managed link stops installation rather than overwriting it.
Exceptions roll back changed paths and both release pointers.

The filesystem installer only updates owned user files and links. Scheduling and
pre-login application reload systemd unit definitions, without enabling or starting
services. No step runs gsettings, the cleaner, application commands, Shell Eval,
extension reloads, or session restarts. Existing processes keep their loaded build until explicitly
reloaded or the next login. If Chrome registered the checkout directory, use
**Load unpacked** to re-register the companion at
`~/.local/share/workspace-state/chrome-extension` (or its XDG equivalent) with
the unchanged manifest key/extension ID, and verify its profile mapping. Merely
reloading the old checkout registration cannot activate a staged release. Doctor
reports this exact registration mismatch. Preferences are never edited automatically. VS Code's unpacked companion lives in its normal
extension directory; profiles which have disabled that companion still require
explicit enablement in VS Code. Native messaging points at the stable installed
host. Doctor reports unknown or old runtime identity rather than calling these
processes updated.

`install … --host-integration` opts into the maintained LG edge extension and
KMS recovery script/service/90 drop-in. It does not edit
`50-nvidia-kms-compat.conf`, GPU settings, or arm a recovery trial. The cleaner is
staged for provenance only and has no default install mappings. GC-profiled code
can be installed, but neither its service nor cleanup profiles are enabled.
Existing unrelated configuration remains untouched.

The alerts inventory is created only if absent. Its migration updates only the
known first-party `input-source-popup-guard` v1 UUID and its exact built-in
`~/.local/share/gnome-shell/extensions/input-source-popup-guard@sagecat.local`
source path to v2, including entries whose UUID was already migrated. Custom
source paths, comments, fields and other entries are preserved. Explicit migration
is idempotent:

```
wsctl deployment migrate-inventory --path ~/.config/workspace-state/alerts.d/owned-systems.toml
```

`dev-link` is a separate, explicit mutable-checkout mode. It uses the same owned
path backups but links source directories, and doctor does not claim an immutable
installed build for it. Return to an ordinary release with `install REVISION`.

Doctor is read-only. It compares source, staged/installed, and loaded runtime
identity, capabilities and protocol separately; checks managed link drift; and
reports current operation identity and checkpoint schema from the data directory.
Runtime probes have an eight-second shared budget and at most two seconds per
component, including all companion endpoints. GNOME probes call only
each component's scoped `GetState`. Chrome/VS Code use their existing read-only
metadata RPCs, and Nemo exposes its scoped `GetState`. Runtime fingerprints are
loaded once with each module. An installed pointer or manifest is never evidence
that a running process loaded that build. `wsctl deployment fingerprint` and
Python `deployment.build_fingerprint()` expose the calling Python process's loaded
build. This is separate from the running coordinator: it publishes a private
receipt at registration, and doctor trusts it only while boot ID, PID and process
start tick all still match. Missing or legacy coordinator receipts mean unknown.

Public Python API: `stage(manifest_path, source_root, locations)`,
`install(revision, locations, profiles=(), development=False)`, `rollback(locations)`,
`schedule(revision, locations, profiles=(), reload_manager=False)`,
`apply_pending(locations, blocker_reader=…, reload_manager=False)`,
`doctor(manifest_path, source_root, locations, runtime_reader=…)`,
`migrate_inventory(path)`, `build_fingerprint()`, and `add_parser(subparsers)`.
`record_coordinator_build(login_generation)` publishes that coordinator receipt.
`Locations` allows isolated tests/alternate user roots. Python 3.11 or newer is
checked before staging imports and by legacy Makefile install prerequisites. Installation is serialized
with a filesystem lock; staging and rollback never activate runtime components.

The CLI sets `reload_manager=True` for scheduling/application; API defaults keep
isolated tests free of host calls. Runtime observer test seams accept fake readers.
Chrome ping reports queued/running mutations and identification markers; capture
refuses either, and the shutdown barrier waits with a shared absolute deadline
before publishing a checkpoint.
