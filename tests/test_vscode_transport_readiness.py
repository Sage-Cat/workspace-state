from __future__ import annotations

import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from workspace_state import vscode
from workspace_state.util import CommandError


class VscodeTransportReadinessTests(unittest.TestCase):
    def test_busy_authenticated_socket_is_retryable_readiness(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "vscode"
            root.mkdir(mode=0o700)
            endpoint = root / ("a" * 32 + ".sock")
            errors = []
            with socket.socket(socket.AF_UNIX) as server:
                server.bind(str(endpoint))
                endpoint.chmod(0o600)
                server.listen(1)
                server.settimeout(2)

                def busy_host():
                    try:
                        with server.accept()[0] as connection:
                            connection.settimeout(2)
                            # Accept the request but stay busy until the bounded
                            # client closes, as an activating extension host can.
                            while connection.recv(8192):
                                pass
                    except Exception as error:
                        errors.append(error)

                worker = threading.Thread(target=busy_host)
                worker.start()
                try:
                    with patch.object(vscode, "runtime_root", return_value=root):
                        with self.assertRaises(vscode.CompanionNotReady) as raised:
                            vscode._request(endpoint, "state", timeout=.03)
                    self.assertIsInstance(raised.exception.__cause__, TimeoutError)
                finally:
                    worker.join(3)
                self.assertFalse(worker.is_alive())
                self.assertEqual(errors, [])

    def test_discovery_retries_busy_endpoint_without_treating_it_as_absent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            endpoint = root / ("b" * 32 + ".sock")
            endpoint.touch()
            state = {"instance": endpoint.stem, "pid": 123, "version": 1}
            with patch.object(vscode, "runtime_root", return_value=root), patch.object(
                vscode, "_request_once", side_effect=[TimeoutError(), state],
            ) as request:
                with self.assertRaises(vscode.CompanionNotReady):
                    vscode._states(time.monotonic() + 10)
                self.assertEqual(vscode._states(time.monotonic() + 10), [{**state, "endpoint": endpoint}])
            self.assertEqual(request.call_count, 2)

    def test_transport_classification_does_not_retry_security_or_protocol_errors(self):
        for error in (CommandError("Unsafe VS Code companion endpoint permissions"),
                      CommandError("VS Code companion identity changed")):
            with self.subTest(error=error), patch.object(vscode, "_request_once", side_effect=error):
                with self.assertRaises(CommandError) as raised:
                    vscode._request(Path("/unused.sock"), "state")
                self.assertIs(raised.exception, error)
                self.assertNotIsInstance(raised.exception, vscode.CompanionNotReady)


if __name__ == "__main__":
    unittest.main()
