"""A3 transport regression tests: loopback only, no model/job dispatch."""
from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import select
import socket
import subprocess
import sys
import threading
import time
import unittest

DIALER = Path(__file__).resolve().parents[1] / 'experiments/m4/a3/stdio_dialer.py'


@contextmanager
def peer(handler):
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    listener.listen(1)
    listener.settimeout(3)
    connections = []
    errors = []

    def serve():
        try:
            connection, _ = listener.accept()
            connections.append(connection)
            connection.settimeout(3)
            with connection:
                handler(connection)
        except OSError:
            pass
        except Exception as error:
            errors.append(error)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield listener.getsockname()[1]
    finally:
        listener.close()
        for connection in connections:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            connection.close()
        thread.join(4)
        if errors:
            raise errors[0]


def echo(connection):
    while data := connection.recv(65536):
        connection.sendall(data)


@contextmanager
def dialer(port):
    env = dict(os.environ)
    env.pop('M4_A3_LIVE', None)
    process = subprocess.Popen(
        [sys.executable, str(DIALER), '--port', str(port)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=env)
    try:
        yield process
    finally:
        if process.poll() is None:
            process.terminate()
        try:
            process.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate(timeout=3)
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                stream.close()


def read_bytes(process, length, timeout=2):
    result = bytearray()
    deadline = time.monotonic() + timeout
    while len(result) < length:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not select.select([process.stdout], [], [], remaining)[0]:
            raise AssertionError('response did not arrive while stdin remained open')
        data = os.read(process.stdout.fileno(), length - len(result))
        if not data:
            raise AssertionError('unexpected stdout EOF')
        result.extend(data)
    return bytes(result)


class TransportRecovery(unittest.TestCase):
    def test_live_requests_without_stdin_eof(self):
        with peer(echo) as port, dialer(port) as process:
            for request in (b'{"id":1,"method":"ping"}\n', b'{"id":2,"method":"ping"}\n'):
                process.stdin.write(request)
                process.stdin.flush()
                self.assertEqual(read_bytes(process, len(request)), request)
                self.assertIsNone(process.poll())

    def test_peer_eof_exits_while_stdin_is_open(self):
        # An endpoint restart must be visible immediately, not at the next request.
        with peer(lambda connection: None) as port, dialer(port) as process:
            self.assertNotEqual(process.wait(timeout=2), 0)
            self.assertEqual(process.stdout.read(), b'')

    def test_stdin_eof_drains_final_response(self):
        def handler(connection):
            data = bytearray()
            while chunk := connection.recv(65536):
                data.extend(chunk)
            connection.sendall(bytes(data) + b'FINAL\n')
        with peer(handler) as port, dialer(port) as process:
            stdout, stderr = process.communicate(b'hello\x00\xff\n', timeout=5)
            self.assertEqual(stdout, b'hello\x00\xff\nFINAL\n')
            self.assertEqual(process.returncode, 0, stderr)

    def test_large_binary_stream_is_byte_exact(self):
        payload = bytes(range(256)) * 4096  # 1 MiB; bounded buffers must progress.
        with peer(echo) as port, dialer(port) as process:
            stdout, stderr = process.communicate(payload, timeout=10)
            self.assertEqual(stdout, payload)
            self.assertEqual(process.returncode, 0, stderr)

    def test_fragment_does_not_wait_for_newline(self):
        with peer(echo) as port, dialer(port) as process:
            for chunk in (b'{"id":', b'3}', b'\n'):
                process.stdin.write(chunk)
                process.stdin.flush()
                self.assertEqual(read_bytes(process, len(chunk)), chunk)

    def test_peer_final_bytes_are_not_lost(self):
        def handler(connection):
            connection.sendall(b'last-frame\n')
        with peer(handler) as port, dialer(port) as process:
            self.assertEqual(read_bytes(process, 11), b'last-frame\n')
            self.assertNotEqual(process.wait(timeout=2), 0)

    def test_downstream_consumer_disconnect_is_bounded(self):
        def handler(connection):
            connection.sendall(b'response\n')
            time.sleep(0.2)
        with peer(handler) as port, dialer(port) as process:
            process.stdout.close()
            process.stdout = None
            self.assertNotEqual(process.wait(timeout=2), 0)


if __name__ == '__main__':
    unittest.main()
