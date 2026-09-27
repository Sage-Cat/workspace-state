#!/usr/bin/env python3
"""Capture real CLI output with an isolated headless browser; no live UI changes."""
import html
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]


def run(args, **kwargs):
    return subprocess.check_output(args, text=True, timeout=15, **kwargs).strip()


def render(title, subtitle, content, destination):
    chrome = shutil.which('google-chrome') or shutil.which('chromium')
    if not chrome:
        raise SystemExit('Install Chrome or Chromium to render the documentation capture.')
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='documentation-browser-') as temporary:
        root = Path(temporary)
        page = root / 'capture.html'
        height = max(440, 220 + 24 * len(content.splitlines()))
        page.write_text('<!doctype html><meta charset="utf-8"><style>'
            'body{margin:0;background:#0c1420;color:#e2eaf2;font-family:monospace;padding:40px}'
            'h1{font:600 30px sans-serif;color:#69e0cf;margin:0 0 12px}'
            'p{font:16px sans-serif;color:#a8b6ca;margin-bottom:28px}'
            'pre{font:17px/24px monospace;white-space:pre-wrap;overflow-wrap:anywhere;'
            'padding:24px;background:#132234;border:1px solid #294157;border-radius:12px}'
            '</style><h1>' + html.escape(title) + '</h1><p>' + html.escape(subtitle)
            + '</p><pre>' + html.escape(content) + '</pre>')
        subprocess.run([chrome, '--headless', '--disable-gpu', '--disable-background-networking',
            '--no-first-run', '--no-default-browser-check', '--hide-scrollbars',
            '--user-data-dir=' + str(root / 'profile'), '--screenshot=' + str(destination),
            '--window-size=1200,' + str(height), '--timeout=10000', page.as_uri()],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
    print(destination)


if __name__ == '__main__':
    with tempfile.TemporaryDirectory(prefix='alerts-documentation-') as temporary:
        root = Path(temporary)
        env = dict(os.environ)
        for key, folder in [('XDG_CONFIG_HOME', 'config'), ('XDG_STATE_HOME', 'state'), ('XDG_RUNTIME_DIR', 'runtime')]:
            path = root / folder
            path.mkdir(mode=0o700)
            env[key] = str(path)
        inventory = root / 'config/workspace-state/alerts.d'
        inventory.mkdir(parents=True)
        (inventory / 'example.toml').write_text('schema_version = 1\n[[sources]]\nid = "example-app"\n'
            'label = "Example application"\nkind = "events"\nownership = "first-party"\n'
            'source_ref = "https://example.org/project"\n')
        (inventory / 'example.toml').chmod(0o600)
        cli = [str(ROOT / 'bin/wsctl'), 'alerts']
        run(cli + ['report', 'example-app', 'connection-lost', 'Reconnect the example service', '--severity', 'critical'], env=env)
        active = json.loads(run(cli + ['list'], env=env))['incidents'][0]
        run(cli + ['ack', 'example-app', 'connection-lost'], env=env)
        acknowledged = json.loads(run(cli + ['list'], env=env))['incidents'][0]
        run(cli + ['resolve', 'example-app', 'connection-lost'], env=env)
        resolved = json.loads(run(cli + ['list', '--history'], env=env))['incidents'][0]
        def fields(row):
            return json.dumps({k: row[k] for k in ('source', 'code', 'severity', 'active', 'acknowledged')}, indent=2)
        content = '$ wsctl alerts report example-app connection-lost ' + chr(92) + '\n    "Reconnect the example service" --severity critical\nOK\n\n'
        content += '$ wsctl alerts list  # selected incident fields\n' + fields(active)
        content += '\n\n$ wsctl alerts ack example-app connection-lost\n'
        content += 'active = ' + str(acknowledged['active']) + '  acknowledged = ' + str(acknowledged['acknowledged'])
        content += '\n\n$ wsctl alerts resolve example-app connection-lost\n'
        content += 'active = ' + str(resolved['active']) + '  acknowledged = ' + str(resolved['acknowledged'])
        render('workspace-state · report, acknowledge, resolve',
            'Real CLI output, selected fields · synthetic application · temporary XDG state',
            content, ROOT / 'docs/screenshots/incident-lifecycle.png')
