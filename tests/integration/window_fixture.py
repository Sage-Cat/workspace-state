"""Application fixture; run only inside run_headless.py's private compositor."""
import os
from pathlib import Path
import sys

import gi
gi.require_version('Gtk', '3.0')
from gi.repository import Gio, Gtk

if os.environ.get('WAYLAND_DISPLAY') != 'wsctl-integration' or not str(Path(os.environ['XDG_RUNTIME_DIR'])).startswith('/tmp/wsctl-headless-'):
    raise SystemExit('refusing to launch the fixture outside the isolated desktop')

application = Gtk.Application(application_id='org.sagecat.WorkspaceIntegration', flags=Gio.ApplicationFlags.NON_UNIQUE)


def activate(app):
    window = Gtk.ApplicationWindow(application=app, title='Workspace integration fixture')
    window.set_default_size(400, 300)
    window.add(Gtk.Label(label='Private headless workspace integration fixture'))
    window.show_all()


application.connect('activate', activate)
raise SystemExit(application.run(sys.argv))
