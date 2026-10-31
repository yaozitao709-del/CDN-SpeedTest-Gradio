import json
import socket
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from launcher import LaunchLock, find_instance, choose_port, launch


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_instance_is_reused_only_when_identity_matches(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps({'app': 'cdn-speedtest-gradio', 'instance': 'test'}).encode())

            def log_message(self, *_):
                pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            url = f'http://127.0.0.1:{server.server_port}'
            file = self.root / 'instance.json'
            file.write_text(json.dumps({'url': url, 'instance': 'test'}))
            self.assertEqual(find_instance(file), url)
            file.write_text(json.dumps({'url': url, 'instance': 'wrong'}))
            self.assertIsNone(find_instance(file))
        finally:
            server.shutdown()
            server.server_close()

    def test_instance_file_cannot_redirect_to_remote_site(self):
        file = self.root / 'instance.json'
        for url in ['https://example.com', 'http://localhost.evil.test:7861', 'file:///etc/passwd']:
            file.write_text(json.dumps({'url': url, 'instance': 'test'}))
            self.assertIsNone(find_instance(file))

    def test_port_chooser_skips_an_occupied_port(self):
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            sock.listen()
            port = sock.getsockname()[1]
            self.assertNotEqual(choose_port(port, port + 5), port)

    def test_launch_lock_prevents_duplicate_bootstrap(self):
        lock = self.root / 'launch.lock'
        with LaunchLock(lock):
            with self.assertRaises(BlockingIOError):
                with LaunchLock(lock):
                    self.fail('second startup must not obtain the lock')
        with LaunchLock(lock):
            pass

    def test_instance_write_failure_reaps_spawned_server(self):
        data = self.root / 'data'
        data.mkdir()
        process = Mock(pid=55555)
        process.poll.return_value = None
        with patch('launcher.ROOT', self.root), patch('launcher.DATA', data), \
                patch('launcher.INSTANCE', data / 'instance.json'), \
                patch('launcher.find_instance', return_value=None), \
                patch('launcher.ensure_runtime', return_value=Path('/fake/python')), \
                patch('launcher.choose_port', return_value=7890), \
                patch('launcher.health', return_value=True), \
                patch('launcher.HTTP.open', side_effect=OSError('no HTTP in unit test')), \
                patch('launcher.subprocess.Popen', return_value=process), \
                patch('pathlib.Path.write_text', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                launch(False)
        process.terminate.assert_called_once()
        process.wait.assert_called()


if __name__ == '__main__':
    unittest.main()
