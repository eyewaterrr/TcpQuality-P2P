"""Offline regression tests: no Internet services are contacted."""
import importlib.util
import json
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

APP = Path(__file__).resolve().parents[1] / 'p2p.py'
spec = importlib.util.spec_from_file_location('p2p', APP)
p2p = importlib.util.module_from_spec(spec)
spec.loader.exec_module(p2p)


class TCPServer:
    def __init__(self):
        self.sock = socket.socket()
        self.sock.bind(('127.0.0.1', 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        self.stop = False
        self.thread = threading.Thread(target=self.run, daemon=True)

    def run(self):
        self.sock.settimeout(.1)
        while not self.stop:
            try:
                client, _ = self.sock.accept()
                client.close()
            except socket.timeout:
                pass
            except OSError:
                break

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, exc_type, value, traceback):
        self.stop = True
        self.sock.close()
        self.thread.join(timeout=2)


class OfflineTests(unittest.TestCase):
    def test_tcp_open_and_refused(self):
        with TCPServer() as server:
            status, rtt, _ = p2p.connect_probe(socket.AF_INET, '127.0.0.1', server.port, .5)
            self.assertEqual(status, 'ok')
            self.assertGreaterEqual(rtt, 0)
            port = server.port
        status, _, _ = p2p.connect_probe(socket.AF_INET, '127.0.0.1', port, .5)
        self.assertEqual(status, 'refused')

    def test_statistic_excludes_refused(self):
        stats = p2p.statistics_for([
            {'status': 'ok', 'rtt_ms': 10}, {'status': 'refused', 'rtt_ms': 1},
            {'status': 'ok', 'rtt_ms': 20}, {'status': 'timeout', 'rtt_ms': None}])
        self.assertEqual(stats['received'], 2)
        self.assertEqual(stats['refused'], 1)
        self.assertEqual(stats['response_missing_pct'], 50)
        self.assertEqual(stats['jitter_ms'], 10)
        self.assertAlmostEqual(stats['p95_ms'], 19.5)

    def test_cli_json_csv(self):
        with TCPServer() as server, tempfile.TemporaryDirectory() as tmp:
            jp = Path(tmp) / 'report.json'
            cp = Path(tmp) / 'samples.csv'
            process = subprocess.run(
                [sys.executable, str(APP), '127.0.0.1', '--port', str(server.port),
                 '-c', '3', '--interval', '0', '--mode', 'connect', '--quick',
                 '--json', str(jp), '--csv', str(cp)],
                capture_output=True, text=True, timeout=15)
            self.assertEqual(process.returncode, 0, process.stderr + process.stdout)
            data = json.loads(jp.read_text(encoding='utf8'))
            self.assertEqual(data['stats']['received'], 3)
            self.assertEqual(data['probe_mode'], 'connect')
            self.assertIn('rtt_ms', cp.read_text(encoding='utf-8-sig'))

    def test_cli_fails_for_closed_port(self):
        with TCPServer() as server:
            port = server.port
        process = subprocess.run([sys.executable, str(APP), '127.0.0.1', '--port', str(port),
                                  '-c', '2', '--interval', '0', '--mode', 'connect', '--quick'],
                                 capture_output=True, text=True, timeout=15)
        self.assertEqual(process.returncode, 1)
        self.assertIn('端口拒绝 2', process.stdout)

    def test_syn_matching_uses_outgoing_source_port(self):
        output = ('SENT (0.0100s) TCP 127.0.0.1:43321 > 127.0.0.1:5201 S\n'
                  'RCVD (0.0120s) TCP 127.0.0.1:5201 > 127.0.0.1:43321 SA\n'
                  'Raw packets sent: 1 (40B) | Rcvd: 1 (46B)')
        completed = subprocess.CompletedProcess(args=[], returncode=0, stdout=output, stderr='')
        with patch.object(p2p.subprocess, 'run', return_value=completed):
            status, rtt, _ = p2p.syn_probe('127.0.0.1', 5201, 1)
        self.assertEqual(status, 'ok')
        self.assertAlmostEqual(rtt, 2)

    def test_default_modes(self):
        basic = p2p.arguments(['127.0.0.1'])
        self.assertTrue(basic.route)
        self.assertFalse(basic.speed)
        quick = p2p.arguments(['127.0.0.1', '--quick'])
        self.assertFalse(quick.route)
        self.assertFalse(quick.speed)
        full = p2p.arguments(['127.0.0.1', '--full'])
        self.assertTrue(full.route)
        self.assertTrue(full.speed)
        with self.assertRaises(SystemExit):
            p2p.arguments(['127.0.0.1', '--quick', '--full'])

    def test_reject_invalid_argument(self):
        with self.assertRaises(SystemExit):
            p2p.arguments(['127.0.0.1', '--port', '65536'])
        with self.assertRaises(SystemExit):
            p2p.arguments(['127.0.0.1', '--max-rate', 'oops'])


if __name__ == '__main__':
    unittest.main()