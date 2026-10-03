#!/usr/bin/python3
"""Credential-free, loopback-only delayed pages in the disposable KVM guest."""
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
import subprocess
import time

ROOT = Path.home() / '.local/state/wsctl-scale'


def delay_seconds():
    value = json.loads((ROOT / 'http-delay.json').read_text())['seconds']
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not 0 <= value <= 15:
        raise ValueError('Fixture delay must be between 0 and 15 seconds')
    return value


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/health':
            body = b'wsctl-scale-pages\n'
        elif self.path.startswith('/scale/'):
            started = time.monotonic()
            delay = delay_seconds()
            time.sleep(delay)
            body = ('<!doctype html><title>Synthetic scale page</title><p>' +
                    escape(self.path) + '</p>').encode()
            # Service journal proves actual delayed requests, without private URLs.
            print(json.dumps({'event': 'synthetic-page', 'delay': delay,
                              'elapsed': time.monotonic() - started}), flush=True)
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, *_args):
        pass


def main():
    if socket.gethostname() != 'wsctl-validation' or subprocess.check_output(
            ['systemd-detect-virt'], text=True).strip() != 'kvm':
        raise SystemExit('Refusing outside wsctl-validation KVM guest')
    delay_seconds()
    ThreadingHTTPServer(('127.0.0.1', 18765), Handler).serve_forever()


if __name__ == '__main__':
    main()
