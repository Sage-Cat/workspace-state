"""Synthetic processes for run_vm_scale.py; never authenticates to services."""
from __future__ import annotations

import ctypes
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import signal
import socket
import sys
import time
import uuid


def guard() -> None:
    if socket.gethostname() != 'wsctl-validation' or not (Path.home() / '.local/state/wsctl-scale/consent.json').is_file():
        raise SystemExit('Fixture requires the explicitly configured disposable validation guest')


def conversation() -> None:
    identity = str(uuid.UUID(sys.argv[-1]))
    ctypes.CDLL(None).prctl(15, b'codex', 0, 0, 0)
    root = Path.home() / '.codex/sessions' / datetime.now(timezone.utc).strftime('%Y/%m/%d')
    root.mkdir(parents=True, exist_ok=True)
    rollout = root / f'rollout-synthetic-{identity}.jsonl'
    stream = rollout.open('a+', buffering=1)
    if not rollout.stat().st_size:
        stream.write(json.dumps({'type': 'session_meta', 'payload': {'id': identity,
                     'session_id': identity, 'timestamp': datetime.now(timezone.utc).isoformat(),
                     'cwd': os.getcwd(), 'synthetic': True}}) + '\n')
    print(f'SYNTHETIC conversation {identity}: local process/UUID readiness only', flush=True)
    print('› Fixture ready; no authentication or model request', flush=True)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    while True:
        time.sleep(10)
        stream.flush()


def window() -> None:
    import gi
    gi.require_version('Gtk', '3.0')
    from gi.repository import Gio, GLib, Gtk
    alias, title = sys.argv[-2:]
    GLib.set_prgname(alias)
    app = Gtk.Application(application_id='org.sagecat.Scale.' + alias.replace('-', '_'),
                          flags=Gio.ApplicationFlags.NON_UNIQUE)
    def activate(application):
        win = Gtk.ApplicationWindow(application=application, title=title)
        win.set_wmclass(alias, alias)
        win.set_default_size(580, 360)
        win.add(Gtk.Label(label=title + '\nSynthetic local window: no real service/account'))
        win.show_all()
    app.connect('activate', activate)
    raise SystemExit(app.run([sys.argv[0]]))


if __name__ == '__main__':
    guard()
    if sys.argv[1] == 'conversation':
        conversation()
    else:
        window()
