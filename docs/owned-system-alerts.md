# Owned-system incidents (HUD “Важливе”)

`wsctl` collects and persists incidents. Login HUD only renders them. **The tab
exists only in startup/restoration mode**, never in shutdown. A scan cannot open
or reopen the HUD, take a modal grab, delay restoration, or authorize shutdown.
Closing the startup HUD still closes it; the passive collector remains separate.

## Installation and scope

```sh
make install-alerts
systemctl --user start wsctl-alerts.service
wsctl alerts sources
wsctl alerts scan --lookback-hours 24
wsctl alerts list
```

This narrow installer installs the CLI link, collector service/timer and initial
inventory. It does not restart the coordinator, applications, drives or GNOME.
Repeated installation preserves the existing inventory. Full `make install`
also includes it. Uninstall removes the collector units but preserves incident
history and the inventory. Python 3.11+ and systemd are required; GNOME error
ledger parsing uses the desktop's PyGObject/GLib. Missing dependencies or SSH
access produce incomplete coverage, never automatic installation or repair.

The personal inventory is
`$XDG_CONFIG_HOME/workspace-state/alerts.d/*.toml` (default `~/.config`). Each
source requires `ownership = "first-party"` and `source_ref` identifying owned
code. Files must be regular, owned by the current user and not group/world
writable. Duplicate IDs, wildcards, shell commands and unrecognized adapters
are rejected. This is an explicit reviewed allowlist, **not heuristic discovery
of all installed services**. New own applications must be registered here.

The bundled inventory contains ten local first-party components: the workspace
coordinator/finalizer, cloud metadata check, cache cleaner, browser companion,
and five GNOME extensions. Personal applications and remote services belong in
private inventory files under the configuration directory above. Existing
installed inventories are preserved; publishing this template does not replace
them.

Remote entries can use an existing SSH alias with strict host-key checking and
noninteractive bounded requests. No server agent is installed, and collection
does not change remote applications, configuration or services.

Nextcloud itself, rclone, Docker, SSHFS, QEMU, Chrome, the Discord client and
Ubuntu/third-party GNOME extensions are not monitored sources. A registered own
application can report that its dependency is unavailable, but that does not
enroll the third-party dependency. Disabled daemons remain disabled; an inactive
successful one-shot job is not classified as a missing daemon.

## Collection and honest coverage

The collector runs after `wsctl-workspace-restored.target` and every five minutes
via `wsctl-alerts.timer`, including while the HUD is closed. It uses up to four
workers, per-command deadlines, a 55-second queue budget and a 90-second service
timeout. Shutdown never starts it; it has only normal stop ordering against
`shutdown.target`, no inhibitor or repair actions. Explicit ordering after the
workspace target prevents slow checks from delaying that target or drives. Network work
never holds the incident database lock. Overlapping scans are refused.
An execution condition skips automatic scans without an active graphical
session, so intentionally dormant GNOME session services do not become false
incidents in a TTY. The next graphical scan catches retained remote/local logs.

- `systemd`: exact owned-unit state and its journal, local or remote. Read the
  retained interval since the last successful scan, across boots. The initial
  lookback is 24 hours; `--lookback-hours 1..168` explicitly rechecks history.
  Journal-side filtering precedes limits, so routine log chatter cannot hide a
  critical entry in the last 100 ordinary lines. Collect priority 0–2 entries
  and recognized authentication/uncaught-exception markers, not every warning
  or `ERROR` line. Each query has a 100-matching-entry bound; hitting it is shown
  as `bounded-history`, not complete coverage. Access errors retain the cursor.
- `gnome-extension`: state and `GetExtensionErrors` for exactly the registered
  UUID. No general Shell log sweep. This observes errors GNOME records for that
  extension, not exceptions swallowed privately by application code.
- `browser-companion`: protocol/capability `ping` on existing own native-host
  sockets. Does not start Chrome or read tabs/content. A disconnected companion
  is unknown; Chrome may simply be closed. Protocol health does not imply that
  every extension feature has passed a functional test.
- `events`: explicit reports/resolutions from an own program, without probes.
  CLI diagnostics state `events-only`, not independently verified health.

Unavailable hosts, journals, plugins and missing units remain unverified in CLI
diagnostics; Important shows only a neutral incomplete-verification notice.
A report without a completed scan in 15 minutes is stale. Logging cannot recover
events that an application never emitted or that its host already discarded;
first-party journal retention is a prerequisite for catching failures while this
PC is off. The collector deliberately does not change journal retention policy.

## Incident lifecycle and application integration

Incidents are identified by `(source, code)` and persist an explicit severity:
`blocker`, `critical`, `error`, or `warning`. Repeated event IDs deduplicate;
actual recurrence after recovery becomes a new unread episode. **Acknowledged
does not mean resolved.** Active blocker/critical incidents stay visible even
after acknowledgement. Resolved incidents remain available through the CLI but
are not shown in Important. Successful
service/plugin state can resolve a proven state failure; a running process alone
does not prove that an authentication error or internal exception was fixed.

Example registration for a new own application (`kind = "events"` can also be
used by an application not managed by systemd):

```toml
schema_version = 1
[[sources]]
id = "my-owned-app"
label = "My own application"
kind = "events"
ownership = "first-party"
source_ref = "~/Projects/my-owned-app"
```

After registering it, report an incident with an explicit severity (the report
default is `critical`; use `blocker`, `critical`, `error`, or `warning`):

```sh
wsctl alerts report my-owned-app drive-auth 'Потрібна повторна авторизація Google Drive' --severity blocker --event-id auth-attempt-42
wsctl alerts ack my-owned-app drive-auth
# Only after the application has verified recovery:
wsctl alerts resolve my-owned-app drive-auth
wsctl alerts list --history
```

Use stable event IDs for retries. An application can call these fixed CLI argv
directly without a shell; reporting errors must not crash that application. The
local report API is intended for the current user's programs. Server sources
currently use their existing scoped journals, not a remote writable API.

Do not pass tokens, OAuth responses, full URLs containing credentials, private
message contents or raw exception dumps. Known sensitive lines are replaced;
journal collection stores only a fixed category and unit/host/time diagnostics,
never raw log messages. Redaction is defense in depth, not permission to send
secrets. The UI expands diagnostics inline and executes no actions from reports.
Its only incident action is the fixed `wsctl alerts ack SOURCE CODE` command.

## Storage and UI deployment

Durable SQLite state: `$XDG_STATE_HOME/workspace-state/alerts/incidents.sqlite3`
(default `~/.local/state`). Directory mode 0700; database/runtime snapshot 0600.
Transactions are durable before publishing
`$XDG_RUNTIME_DIR/workspace-state/alerts.json`. Acknowledged history remains in
the database; the HUD transport contains only active blocker/critical incidents
and their source metadata, so older loaded HUDs cannot list healthy systems.
The HUD caps its displayed list at 200 entries. `wsctl alerts list`
shows all active/unread entries; `--history` also shows resolved acknowledged
entries. Old event-deduplication records expire after 90 days; active and unread
incidents do not expire. Older databases gain the severity column automatically;
known codes are migrated conservatively and unknown codes remain `warning`.

Install Login HUD 16 with `./scripts/install.sh --no-enable` in its repository.
On native Wayland the currently loaded JS remains cached until the next normal
GNOME login. Do not restart GNOME, force logout or toggle extensions to simulate
a live upgrade. Existing once-per-boot HUD eligibility remains unchanged, so
the new tab is naturally presented on the next eligible post-boot restoration.

## Verification

`make test` includes temporary private-database CLI tests, persistence across
reopens/boots, deduplication/recurrence, acknowledgement vs resolution, registry
rejection, scoped read-only SSH and browser calls, log filtering/redaction,
disabled/one-shot lifecycle handling, timeouts, permission failures and incomplete
coverage. Login HUD's Node tests exercise actual tab methods, expansion and
acknowledgement callbacks, report bounds, stale data, shutdown exclusion and
layout budgets without touching a live desktop or shutdown protocol.
