"""Private, persistent incidents for an explicit inventory of first-party systems.

Collection is read-only. This module never controls a monitored service, runs an
arbitrary probe command, changes credentials, or participates in power-off.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import multiprocessing
from multiprocessing.connection import wait as wait_connections
import os
import re
import sqlite3
import signal
import stat
import subprocess
import threading
import time
import tomllib
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .util import CommandError, atomic_json

IDENTIFIER = re.compile(r"[a-z0-9][a-z0-9_.-]{0,79}\Z")
UNIT = re.compile(r"[A-Za-z0-9_.@-]+\.service\Z")
HOST = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}\Z")
PLUGIN = re.compile(r"[A-Za-z0-9_.@-]{1,150}\Z")
MAX_SOURCES = 64
MAX_OUTPUT = 1024 * 1024
JOURNAL_LIMIT = 100
SCAN_BUDGET = 55
VARIANT_LOCK = threading.Lock()  # PyGObject's lazy GI namespace initialization is not thread-safe.
LOG_PATTERN = (r"invalid_grant|invalid token|authentication failed|unauthorized|token has expired|"
               r"tokenexpired|loginfailure|traceback \(most recent call last\)|uncaught exception|\[critical\]")
PROPERTIES = ("LoadState", "ActiveState", "SubState", "Result", "UnitFileState",
              "InvocationID", "ExecMainStatus", "NRestarts", "ConditionResult")
MESSAGES = {
    "auth-required": "Потрібна повторна авторизація; доступ до залежності заблоковано.",
    "uncaught-exception": "Програма повідомила про необроблений виняток.",
    "critical-log": "Власна програма записала критичну помилку в журнал.",
    "service-failed": "Власний сервіс завершився з помилкою.",
    "service-unavailable": "Увімкнений власний демон не працює.",
    "restart-loop": "Власний демон багаторазово перезапускається після збою.",
    "plugin-failed": "Власний GNOME-плагін не завантажився або має помилку.",
    "plugin-runtime-error": "GNOME зареєстрував помилку виконання власного плагіна.",
    "companion-failed": "Власний браузерний плагін не відповідає або має несумісний протокол.",
}
SEVERITIES = {"blocker", "critical", "error", "warning"}
CODE_SEVERITIES = {
    "auth-required": "blocker", "service-failed": "critical",
    "service-unavailable": "blocker", "restart-loop": "critical",
    "plugin-failed": "critical", "companion-failed": "error",
    "uncaught-exception": "error", "critical-log": "critical",
    "plugin-runtime-error": "error",
}

def severity_for(code: str) -> str:
    return CODE_SEVERITIES.get(code, "warning")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def boot_id() -> str:
    return Path("/proc/sys/kernel/random/boot_id").read_text().strip()


def root() -> Path:
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "workspace-state/alerts"


def snapshot_path() -> Path:
    return Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")) / "workspace-state/alerts.json"


def config_root() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "workspace-state/alerts.d"


def safe_text(value: Any, limit: int = 1500) -> str:
    text = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", str(value))[:limit]
    # Discard whole sensitive lines rather than guessing where a credential ends.
    return "\n".join(
        "[приховано: можливі секретні дані]" if re.search(
            r"(?i)token|password|passwd|secret|authorization|bearer|api[_-]?key|"
            r"private.key|credential|https?://[^\s/@]+:[^\s/@]+@|[?&](code|key)=", line
        ) else line for line in text.splitlines()
    )


def inventory() -> list[dict[str, Any]]:
    sources = []
    seen = set()
    for path in sorted(config_root().glob("*.toml")):
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise CommandError(f"unsafe alerts inventory permissions: {path.name}")
        if info.st_size > 128 * 1024:
            raise CommandError("alerts inventory is too large")
        payload = tomllib.loads(path.read_text())
        if payload.get("schema_version") != 1:
            raise CommandError("unsupported alerts inventory schema")
        if not isinstance(payload.get("sources"), list):
            raise CommandError("inventory sources must be an array")
        for raw in payload["sources"]:
            if not isinstance(raw, dict):
                raise CommandError("invalid inventory source")
            source = dict(raw)
            identifier = source.get("id", "")
            if not isinstance(identifier, str) or not IDENTIFIER.fullmatch(identifier) or identifier in seen:
                raise CommandError("invalid or duplicate owned source ID")
            if source.get("ownership") != "first-party" or not source.get("source_ref"):
                raise CommandError(f"{identifier}: explicit first-party ownership and source_ref required")
            kind = source.get("kind")
            if kind not in {"systemd", "gnome-extension", "browser-companion", "events"}:
                raise CommandError(f"{identifier}: unsupported read-only adapter")
            source.setdefault("host", "local")
            source.setdefault("scope", "user")
            source.setdefault("expected", "when-enabled")
            if not HOST.fullmatch(str(source["host"])) or source["scope"] not in {"user", "system"}:
                raise CommandError(f"{identifier}: invalid host/scope")
            if source["expected"] not in {"when-enabled", "running", "on-demand"}:
                raise CommandError(f"{identifier}: invalid expected lifecycle")
            if kind == "systemd" and not UNIT.fullmatch(str(source.get("unit", ""))):
                raise CommandError(f"{identifier}: exact service name required; no wildcards")
            if kind == "gnome-extension" and (
                source["host"] != "local" or not PLUGIN.fullmatch(str(source.get("uuid", "")))
            ):
                raise CommandError(f"{identifier}: invalid local plugin UUID")
            if kind == "browser-companion" and source["host"] != "local":
                raise CommandError(f"{identifier}: browser companion must be local")
            # Config is declarative: no arbitrary commands, URLs or repair actions.
            allowed = {"id", "label", "ownership", "source_ref", "host", "scope", "expected", "kind", "unit", "uuid"}
            if set(source) - allowed:
                raise CommandError(f"{identifier}: unsupported source options")
            source["label"] = safe_text(source.get("label", identifier), 120)
            source["source_ref"] = safe_text(source["source_ref"], 300)
            seen.add(identifier)
            sources.append(source)
    if not sources or len(sources) > MAX_SOURCES:
        raise CommandError("owned-system inventory must contain 1–64 explicit sources")
    return sources


@contextlib.contextmanager
def database():
    directory = root()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if directory.is_symlink() or directory.stat().st_uid != os.getuid():
        raise CommandError("unsafe alerts state directory")
    directory.chmod(0o700)
    fd = os.open(directory / "lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = directory / "incidents.sqlite3"
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        path.chmod(0o600)
        db = sqlite3.connect(path)
        db.row_factory = sqlite3.Row
        try:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS sources(id TEXT PRIMARY KEY, config TEXT NOT NULL,
                    health TEXT NOT NULL DEFAULT 'unknown', checked_at TEXT, coverage TEXT,
                    detail TEXT, since REAL);
                CREATE TABLE IF NOT EXISTS incidents(source TEXT, code TEXT, episode INTEGER,
                    active INTEGER, acknowledged INTEGER, first_seen TEXT, last_seen TEXT,
                    resolved_at TEXT, occurrences INTEGER, message TEXT, detail TEXT,
                    PRIMARY KEY(source, code));
                CREATE TABLE IF NOT EXISTS events(source TEXT, code TEXT, token TEXT, at REAL,
                    PRIMARY KEY(source, code, token));
                CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
            """)
            columns = {row["name"] for row in db.execute("PRAGMA table_info(incidents)")}
            if "severity" not in columns:
                db.execute("ALTER TABLE incidents ADD COLUMN severity TEXT NOT NULL DEFAULT 'warning'")
                for code, severity in CODE_SEVERITIES.items():
                    db.execute("UPDATE incidents SET severity=? WHERE code=?", (severity, code))
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()


def register(db, sources):
    for source in sources:
        db.execute("INSERT INTO sources(id,config) VALUES(?,?) ON CONFLICT(id) DO UPDATE SET config=excluded.config",
                   (source["id"], json.dumps(source)))


def record(db, source: str, code: str, message: str, detail: str = "", *, token: str = "", at: str = "", condition: bool = False, severity: str | None = None):
    if not IDENTIFIER.fullmatch(code):
        raise CommandError("invalid incident code")
    severity = severity or severity_for(code)
    if severity not in SEVERITIES:
        raise CommandError("invalid incident severity")
    if not db.execute("SELECT 1 FROM sources WHERE id=?", (source,)).fetchone():
        raise CommandError("source is not in the first-party inventory")
    old = db.execute("SELECT * FROM incidents WHERE source=? AND code=?", (source, code)).fetchone()
    ranks = {"warning": 0, "error": 1, "critical": 2, "blocker": 3}
    if old and old["active"] and ranks[old["severity"]] > ranks[severity]:
        severity = old["severity"]
    # A condition recurring after recovery is a new episode, even on the same
    # boot. Replayed journal events, however, must never revive a resolved issue.
    if condition:
        token += f":episode:{old['episode'] if old and old['active'] else old['episode'] + 1 if old else 1}"
    token = hashlib.sha256((token or uuid.uuid4().hex).encode()).hexdigest()
    inserted = db.execute("INSERT OR IGNORE INTO events VALUES(?,?,?,?)", (source, code, token, time.time())).rowcount
    if not inserted:
        if old and old["active"] and ranks[severity] > ranks[old["severity"]]:
            db.execute("UPDATE incidents SET severity=?,acknowledged=0 WHERE source=? AND code=?", (severity, source, code))
        if condition:
            db.execute("UPDATE incidents SET last_seen=? WHERE source=? AND code=? AND active=1", (now(), source, code))
        return
    if old is None and db.execute("SELECT count(*) FROM incidents WHERE source=?", (source,)).fetchone()[0] >= 32:
        raise CommandError("source incident-code limit reached")
    when = at or now()
    active = old and old["active"]
    db.execute("INSERT OR REPLACE INTO incidents(source,code,episode,active,acknowledged,first_seen,last_seen,resolved_at,occurrences,message,detail,severity) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (
        source, code, old["episode"] if active else (old["episode"] + 1 if old else 1),
        1, old["acknowledged"] if active else 0, old["first_seen"] if active else when,
        when, None, old["occurrences"] + 1 if active else 1,
        safe_text(message, 300), safe_text(detail), severity,
    ))


def resolve(db, source, code):
    db.execute("UPDATE incidents SET active=0,resolved_at=? WHERE source=? AND code=? AND active=1",
               (now(), source, code))


def publish(db, sources, *, scan_error: str | None = None, scanning: bool | None = None) -> dict[str, Any]:
    for key, value in (("scan_error", scan_error), ("scanning", scanning)):
        if value is not None:
            db.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (key, json.dumps(value)))
    meta = {row["key"]: json.loads(row["value"]) for row in db.execute("SELECT * FROM meta")}
    ids = {source["id"] for source in sources}
    systems = []
    for row in db.execute("SELECT * FROM sources ORDER BY id"):
        if row["id"] in ids:
            systems.append(json.loads(row["config"]) | {
                key: row[key] for key in ("health", "checked_at", "coverage", "detail")
            })
    incidents = [dict(row) for row in db.execute(
        "SELECT * FROM incidents WHERE active=1 OR acknowledged=0 ORDER BY active DESC,last_seen DESC"
    ) if row["source"] in ids]
    for system in systems:
        if any(row["active"] and row["source"] == system["id"] for row in incidents):
            system["health"] = "blocked"
    payload = {"schema_version": 1, "boot_id": boot_id(), "updated_at": now(),
               "scan_error": safe_text(meta.get("scan_error", "")), "scanning": meta.get("scanning", False),
               "last_scan_at": meta.get("last_scan_at"), "sources": systems,
               "last_scan_boot_id": meta.get("last_scan_boot_id"),
               "incidents": incidents[:200], "incident_count": len(incidents),
               "active_count": sum(bool(row["active"]) for row in incidents),
               "unread_count": sum(not row["acknowledged"] for row in incidents)}
    db.commit()  # A runtime snapshot must never precede durable incident storage.
    # The HUD transport is intentionally narrower than CLI diagnostics. This
    # also prevents an already-loaded older HUD from listing healthy services.
    important = [item for item in incidents if item["active"] and item["severity"] in {"blocker", "critical"}]
    visible_ids = {item["source"] for item in important}
    payload["important_count"] = len(important)
    atomic_json(snapshot_path(), payload | {
        "sources": [item for item in systems if item["id"] in visible_ids],
        "incidents": important[:200], "incident_count": len(important), "active_count": len(important),
        "unread_count": sum(not item["acknowledged"] for item in important),
        "verification_incomplete": any(item["health"] in {"unknown", "not-installed", "events-only"} or
            item["coverage"] in {"bounded-history", "plugin-errors-unavailable", "journal-unavailable", "scan-timeout"}
            for item in systems),
    })
    return payload


def command(source: dict[str, Any], args: list[str], *, timeout: float = 8):
    if source["host"] != "local":
        import shlex
        args = ["/usr/bin/ssh", "-oBatchMode=yes", "-oConnectTimeout=3", "-oConnectionAttempts=1",
                "-oStrictHostKeyChecking=yes", "-oServerAliveInterval=3", "-oServerAliveCountMax=1",
                source["host"], "env LC_ALL=C " + shlex.join(args)]
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                            env=os.environ | {"LC_ALL": "C"})
    if len(result.stdout) > MAX_OUTPUT:
        raise CommandError("read-only probe output exceeded its limit")
    return result


def classify(entry: dict[str, Any]) -> str | None:
    message = str(entry.get("MESSAGE", "")).lower()
    strong_auth = any(marker in message for marker in ("invalid_grant", "invalid token", "token has expired", "tokenexpired", "loginfailure"))
    error_context = str(entry.get("PRIORITY")) in {"0", "1", "2", "3"} or bool(re.search(r"\berror\b|exception|\b401\b|\b403\b", message))
    if strong_auth or error_context and any(marker in message for marker in ("authentication failed", "unauthorized")):
        return "auth-required"
    if str(entry.get("PRIORITY")) in {"0", "1", "2"} or "[critical]" in message:
        return "critical-log"
    # A traceback emitted by logging.exception is routine diagnostic output;
    # only an explicit uncaught-exception marker is actionable.
    if "uncaught exception" in message:
        return "uncaught-exception"
    return None


def _startup_outcome(source: dict, props: dict) -> str | None:
    """Read only an intentional aggregate outcome from this exact invocation."""
    if (source.get("host") != "local" or source.get("scope") != "user"
            or source.get("unit") != "wsctl-login-finalize.service"
            or props.get("Result") != "exit-code" or props.get("ExecMainStatus") != "1"):
        return None
    invocation = props.get("InvocationID", "")
    if not re.fullmatch(r"[0-9a-f]{32}", invocation):
        return None
    from . import operations
    from .login_status import status_path
    try:
        document = json.loads(status_path().read_text())
        receipt = status_path().parent / "finalizers" / f"{invocation}.json"
        context = operations.OperationContext.from_dict(json.loads(receipt.read_text()))
        outcome = json.loads(receipt.with_suffix(".outcome.json").read_text())
        finalizer = next((stage for stage in document.get("stages", [])
                          if isinstance(stage, dict) and stage.get("id") == "login-finalization"), {})
        if (context.boot_id != boot_id() or context.mode != "startup" or not context.matches(document)
                or document.get("operation_state") != "failed" or finalizer.get("state") != "failed"
                or outcome.get("operation_context") != context.to_dict()
                or outcome.get("kind") != "startup-incomplete"
                or not isinstance(outcome.get("message"), str)
                or finalizer.get("message") != outcome["message"]):
            return None
        return outcome["message"]
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def probe(source: dict[str, Any], since: float | None) -> dict[str, Any]:
    outcome = {"health": "unknown", "coverage": "unknown", "detail": "", "issues": [],
               "resolved": [], "since": since, "checked_at": now()}
    if source["kind"] == "events":
        return outcome | {"health": "events-only", "detail": "Очікує явних report/resolve від власної програми."}
    if source["kind"] == "browser-companion":
        from .browser import (_host_paths, _request_path, BrowserUnavailable,
                              BROWSER_PROTOCOL_VERSION, BROWSER_REQUIRED_CAPABILITIES)
        sockets = _host_paths()
        if not sockets:
            return outcome | {"health": "unknown", "coverage": "not-connected",
                              "detail": "Власний браузерний плагін не підключений; браузер не запускався перевіркою."}
        if len(sockets) > 8:
            return outcome | {"detail": "Кількість підключень перевищує безпечний ліміт перевірки."}
        failed = False
        for socket in sockets:
            try:
                info = _request_path(socket, "ping", timeout=1)
                failed |= (not isinstance(info, dict) or info.get("protocol_version") != BROWSER_PROTOCOL_VERSION or
                           not BROWSER_REQUIRED_CAPABILITIES.issubset(set(info.get("capabilities", []))))
            except BrowserUnavailable:
                failed = True
        outcome.update(health="blocked" if failed else "healthy", coverage="protocol-only",
                       detail=f"Власний native-messaging плагін: перевірено {len(sockets)} підключень лише командою ping.")
        if failed:
            outcome["issues"].append(("companion-failed", f"companion:{boot_id()}", "", outcome["detail"]))
        else:
            outcome["resolved"].append("companion-failed")
        return outcome
    if source["kind"] == "gnome-extension":
        result = command(source, ["/usr/bin/gnome-extensions", "info", source["uuid"]])
        if result.returncode:
            return outcome | {"detail": "GNOME-плагін або сеанс недоступний для перевірки."}
        state = next((line.split(":", 1)[1].strip() for line in result.stdout.splitlines() if line.strip().startswith("State:")), "UNKNOWN")
        outcome.update(health="healthy" if state == "ACTIVE" else "disabled" if state in {"INACTIVE", "INITIALIZED", "DISABLED"} else "blocked" if state in {"ERROR", "OUT OF DATE"} else "unknown", coverage="state-only", detail=f"GNOME extension state: {state}")
        if outcome["health"] == "blocked":
            outcome["issues"].append(("plugin-failed", f"plugin:{boot_id()}:{state}", "", f"Plugin: {source['uuid']}; state: {state}"))
        elif state == "ACTIVE":
            outcome["resolved"].append("plugin-failed")
        # This ledger is scoped to one explicitly owned UUID, never the general
        # Shell journal (which also contains third-party extension failures).
        errors = command(source, ["/usr/bin/gdbus", "call", "--session", "--dest", "org.gnome.Shell.Extensions",
            "--object-path", "/org/gnome/Shell/Extensions", "--method",
            "org.gnome.Shell.Extensions.GetExtensionErrors", source["uuid"]])
        if errors.returncode:
            return outcome | {"coverage": "plugin-errors-unavailable"}
        try:
            with VARIANT_LOCK:
                from gi.repository import GLib
                messages = GLib.Variant.parse(GLib.VariantType("(as)"), errors.stdout, None, None).unpack()[0]
        except ImportError:
            return outcome | {"coverage": "plugin-errors-unavailable"}
        except Exception as error:
            raise CommandError("invalid plugin error ledger") from error
        outcome["coverage"] = "state-and-plugin-errors"
        # The owned error ledger is diagnostic telemetry, not proof that the
        # plugin currently failed to load. Do not promote ledger entries.
        return outcome
    scope = ["--user"] if source["scope"] == "user" else []
    result = command(source, ["/usr/bin/systemctl", *scope, "show", source["unit"],
                              "--property=" + ",".join(PROPERTIES)])
    if result.returncode:
        if "LoadState=not-found" in result.stdout.splitlines():
            return outcome | {"health": "not-installed", "detail": "Зареєстрований власний сервіс не встановлений."}
        return outcome | {"detail": f"Стан сервісу недоступний (код {result.returncode}); проблеми не вважаються усуненими."}
    props = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    if props.get("LoadState") == "not-found":
        return outcome | {"health": "not-installed", "detail": "Зареєстрований власний сервіс не встановлений."}
    active, result_state = props.get("ActiveState", "unknown"), props.get("Result", "unknown")
    if props.get("LoadState") != "loaded" or active not in {"active", "inactive", "failed", "activating", "deactivating", "reloading"}:
        return outcome | {"detail": "Сервіс не надав повного перевіреного стану."}
    disabled = source["expected"] == "when-enabled" and props.get("UnitFileState") in {"disabled", "masked"} and active == "inactive"
    detail = "; ".join(f"{key}={value}" for key, value in props.items())
    outcome.update(health="disabled" if disabled else "healthy", detail=detail, coverage="state-only")
    if not disabled and (active in {"activating", "deactivating"} or active == "inactive" and source["expected"] != "on-demand"):
        outcome["health"] = "unknown"
    if active == "inactive" and source["expected"] == "on-demand" and not props.get("InvocationID"):
        outcome["health"] = "unknown"  # A default Result=success does not prove the job ran.
    state_token = ":".join(props.get(key, "") for key in ("InvocationID", "Result", "ActiveState", "ExecMainStatus"))
    if active == "failed" or (result_state not in {"success", "unknown", ""} and not disabled):
        aggregate = _startup_outcome(source, props)
        if aggregate:
            outcome["messages"] = {"service-failed": "Відновлення сеансу не завершено; підсумок помилок відновлення, а не окреме падіння сервісу."}
            detail += "; " + aggregate
            outcome["detail"] = detail
            # Updating an existing generic diagnosis keeps the same incident.
            state_token += ":startup-incomplete"
        outcome["issues"].append(("service-failed", state_token, "", detail))
    elif active == "active" or (source["expected"] == "on-demand" and result_state == "success" and props.get("InvocationID")):
        outcome["resolved"].extend(["service-failed", "service-unavailable", "restart-loop"])
    elif not disabled and active == "inactive" and props.get("ConditionResult") != "no" and (
        source["expected"] == "running" or
        source["expected"] == "when-enabled" and props.get("UnitFileState") in {"enabled", "enabled-runtime"}
    ):
        outcome["issues"].append(("service-unavailable", state_token, "", detail))
    if active == "activating" and int(props.get("NRestarts", "0") or 0) >= 5:
        outcome["issues"].append(("restart-loop", state_token, "", detail))
    # Read the interval since the last successful scan, across boots. No generic
    # host journal, third-party units, config files, or remote writes are used.
    before = time.time()
    start = max(0, since - 1) if since else before - 24 * 3600
    journal_args = ["/usr/bin/journalctl", *scope, "--unit=" + source["unit"],
                    "--since=@" + str(int(start)), "--no-pager", "--output=json",
                    "--lines=" + str(JOURNAL_LIMIT),
                    "--output-fields=MESSAGE,PRIORITY,__CURSOR,__REALTIME_TIMESTAMP,_SYSTEMD_INVOCATION_ID"]
    # Filter at the journal, BEFORE the limit. Routine daemon chatter must not
    # push yesterday's critical incident out of a small tail buffer.
    journals = [command(source, journal_args + ["--priority=0..2"]),
                command(source, journal_args + ["--case-sensitive=no", "--grep=" + LOG_PATTERN])]
    unavailable = any((journal.returncode != 0 and not (index == 1 and journal.returncode == 1 and not journal.stdout and not journal.stderr)) or
                      (source["scope"] == "system" and "not seeing messages" in journal.stderr.lower()) or
                      "permission" in journal.stderr.lower() for index, journal in enumerate(journals))
    if unavailable:
        outcome.update(coverage="journal-unavailable", detail=detail + "; журнал недоступний для цього користувача")
    else:
        lines = list(dict.fromkeys(line for journal in journals for line in journal.stdout.splitlines()))
        outcome["coverage"] = "bounded-history" if any(len(journal.stdout.splitlines()) >= JOURNAL_LIMIT for journal in journals) else "checked-interval"
        for line in lines:
            entry = json.loads(line)
            code = classify(entry)
            if not code:
                continue
            cursor = str(entry.get("__CURSOR") or hashlib.sha256(line.encode()).hexdigest())
            stamp = int(entry.get("__REALTIME_TIMESTAMP", 0) or 0) / 1_000_000
            at = datetime.fromtimestamp(stamp, timezone.utc).isoformat(timespec="seconds") if 0 < stamp <= before + 300 else now()
            # Never persist raw log messages, including OAuth responses or bot tokens.
            evidence = f"Unit: {source['unit']}; host: {source['host']}; event: {code}; time: {at}"
            outcome["issues"].append((code, cursor, at, evidence))
        outcome["since"] = before
    if outcome["issues"]:
        outcome["health"] = "blocked"
    elif outcome["coverage"] == "journal-unavailable":
        outcome["health"] = "unknown"
    return outcome


def safe_probe(source, since):
    try:
        return probe(source, since)
    except (OSError, ValueError, TypeError, CommandError, subprocess.TimeoutExpired):
        return {"health": "unknown", "coverage": "unknown", "detail": "Перевірка недоступна або перевищила час очікування; старі проблеми збережено.",
                "issues": [], "resolved": [], "since": since, "checked_at": now()}


def _probe_process(connection, source, since):
    # Each read-only probe and its subprocesses have one killable owner. No
    # incident database connection exists while these children are running.
    os.setsid()
    try:
        connection.send(safe_probe(source, since))
    finally:
        connection.close()


def _stop_probe(process):
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        if process.is_alive():
            process.kill()  # The child may not have reached setsid yet.
    process.join(timeout=0.25)
    if not process.is_alive():
        process.close()


def _bounded_probes(sources, previous):
    deadline = time.monotonic() + SCAN_BUDGET
    results = [{
        "health": "unknown", "coverage": "scan-timeout", "detail": "Загальний час перевірки вичерпано; попередні проблеми збережено.",
        "issues": [], "resolved": [], "since": previous.get(source["id"]), "checked_at": now(),
    } for source in sources]
    # Linux-only desktop tooling. Fork preserves the read-only probe functions
    # without an additional daemon; the parent remains the only store writer.
    context = multiprocessing.get_context("fork")
    pending = {}
    next_index = 0
    try:
        while next_index < len(sources) or pending:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            while next_index < len(sources) and len(pending) < 4:
                index = next_index
                next_index += 1
                reader, writer = context.Pipe(duplex=False)
                process = context.Process(target=_probe_process, args=(writer, sources[index], previous.get(sources[index]["id"])))
                try:
                    process.start()
                except BaseException:
                    reader.close()
                    writer.close()
                    raise
                writer.close()
                pending[reader] = (process, index)
            ready = wait_connections(list(pending), timeout=max(0, deadline - time.monotonic()))
            for reader in ready:
                process, index = pending.pop(reader)
                try:
                    results[index] = reader.recv()
                except (EOFError, OSError):
                    # A crashed child is unavailable, never a successful probe.
                    results[index]["coverage"] = "unknown"
                finally:
                    reader.close()
                    _stop_probe(process)
    finally:
        for reader, (process, _index) in pending.items():
            reader.close()
            _stop_probe(process)
    return results


def scan(*, lookback_hours: int = 0) -> dict[str, Any]:
    if not 0 <= lookback_hours <= 168:
        raise CommandError("lookback must be between 0 and 168 hours")
    try:
        sources = inventory()
    except (OSError, ValueError, TypeError, CommandError):
        with database() as db:
            previous = [json.loads(row[0]) for row in db.execute("SELECT config FROM sources")]
            publish(db, previous, scanning=False, scan_error="Реєстр власних систем недоступний або некоректний. Перевірку не виконано.")
        raise CommandError("invalid owned-system inventory; scan error published") from None
    with database():
        pass
    fd = os.open(root() / "scan.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise CommandError("an owned-system scan is already running") from None
        return _scan(sources, lookback_hours=lookback_hours)


def _scan(sources, *, lookback_hours=0):
    # Short transactions: network/desktop I/O never holds the incident store lock.
    with database() as db:
        register(db, sources)
        previous = {row["id"]: row["since"] for row in db.execute("SELECT id,since FROM sources")}
        if lookback_hours:
            previous = {source["id"]: time.time() - lookback_hours * 3600 for source in sources}
        publish(db, sources, scanning=True, scan_error="")
    results = _bounded_probes(sources, previous)
    with database() as db:
        for source, outcome in zip(sources, results):
            for code in outcome["resolved"]:
                resolve(db, source["id"], code)
            for code, token, at, detail in outcome["issues"]:
                record(db, source["id"], code, outcome.get("messages", {}).get(code, MESSAGES[code]), detail, token=token, at=at, condition=not at)
            db.execute("UPDATE sources SET health=?,checked_at=?,coverage=?,detail=?,since=? WHERE id=?", (
                outcome["health"], outcome["checked_at"], outcome["coverage"], safe_text(outcome["detail"]), outcome["since"], source["id"],
            ))
        # Active and unread incidents never expire. Keep deduplication longer
        # than the default journal lookback; no secrets or full logs are stored.
        cutoff = time.time() - 90 * 86400
        db.execute("DELETE FROM events WHERE at<?", (cutoff,))
        db.execute("INSERT OR REPLACE INTO meta VALUES('last_scan_at',?)", (json.dumps(now()),))
        db.execute("INSERT OR REPLACE INTO meta VALUES('last_scan_boot_id',?)", (json.dumps(boot_id()),))
        payload = publish(db, sources, scanning=False)
    return payload


def cli(args: argparse.Namespace) -> int:
    if args.alerts_command == "scan":
        payload = scan(lookback_hours=args.lookback_hours)
        print(json.dumps(payload, ensure_ascii=False) if args.json else
              f"Checked {len(payload['sources'])} owned systems; {payload['important_count']} active blocker/critical incident(s).")
        return 0
    sources = inventory()
    with database() as db:
        register(db, sources)
        if args.alerts_command == "sources":
            print(json.dumps(sources, ensure_ascii=False, indent=2))
            return 0
        if args.alerts_command in {"report", "resolve", "ack"}:
            if args.source not in {item["id"] for item in sources}:
                raise CommandError("refusing an unregistered/third-party source")
            if not IDENTIFIER.fullmatch(args.code):
                raise CommandError("invalid incident code")
            if args.alerts_command == "report":
                record(db, args.source, args.code, args.message, args.detail, token=args.event_id or "", severity=args.severity)
            elif args.alerts_command == "resolve":
                resolve(db, args.source, args.code)
            else:
                db.execute("UPDATE incidents SET acknowledged=1 WHERE source=? AND code=?", (args.source, args.code))
        payload = publish(db, sources)
        if args.alerts_command == "list":
            # The runtime UI is bounded; the CLI also exposes complete history.
            ids = {source["id"] for source in sources}
            where = "" if args.history else "WHERE active=1 OR acknowledged=0"
            payload["incidents"] = [dict(row) for row in db.execute(
                f"SELECT * FROM incidents {where} ORDER BY active DESC,last_seen DESC"
            ) if row["source"] in ids]
        print(json.dumps(payload, ensure_ascii=False, indent=2) if args.alerts_command == "list" else "OK")
    return 0


def add_parser(subparsers):
    group = subparsers.add_parser("alerts", help="critical incidents from explicitly owned systems only")
    commands = group.add_subparsers(dest="alerts_command", required=True)
    for name in ("scan", "list", "sources", "report", "resolve", "ack"):
        parser = commands.add_parser(name)
        parser.set_defaults(func=cli)
        if name == "scan":
            parser.add_argument("--json", action="store_true")
            parser.add_argument("--lookback-hours", type=int, default=0, help="recheck up to 168 hours of retained own-unit journals")
        if name == "list":
            parser.add_argument("--history", action="store_true", help="also include resolved and acknowledged incidents")
        if name in {"report", "resolve", "ack"}:
            parser.add_argument("source")
            parser.add_argument("code")
        if name == "report":
            parser.add_argument("message")
            parser.add_argument("--detail", default="")
            parser.add_argument("--event-id")
            parser.add_argument("--severity", choices=sorted(SEVERITIES), default="critical")
