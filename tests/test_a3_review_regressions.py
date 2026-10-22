"""Independent-review findings, reproduced without provider or user data."""
import importlib.util
import os
from pathlib import Path
import socket
import struct
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from test_a3_transport_recovery import peer, dialer
from test_a3_tunnel_guard import guard

class ReviewedTransport(unittest.TestCase):
    def test_rst_backpressure_drains_received_output_before_failure(self):
        payload=bytes(range(256))*720  # 184,320; larger than stdout pipe capacity.
        sent=threading.Event()
        def handler(connection):
            connection.sendall(payload)
            # Allow the relay to receive into its buffer before generating RST.
            time.sleep(.3)
            connection.setsockopt(socket.SOL_SOCKET,socket.SO_LINGER,struct.pack('ii',1,0))
            sent.set()
        with peer(handler) as port, dialer(port) as process:
            self.assertTrue(sent.wait(3))
            time.sleep(.3)  # Leave downstream blocked while RST is processed.
            self.assertIsNone(process.poll(), 'must drain buffered bytes, not exit immediately')
            output,_=process.communicate(timeout=5)
            self.assertEqual(output,payload)
            self.assertNotEqual(process.returncode,0)

class ReviewedLogSafety(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.path=self.root/'log'
    def test_existing_world_readable_log_is_tightened(self):
        self.path.write_bytes(b'existing\n');self.path.chmod(0o644)
        fd=guard.private_append(self.path)
        try:os.write(fd,b'new\n')
        finally:os.close(fd)
        self.assertEqual(self.path.stat().st_mode & 0o777,0o600)
        self.assertEqual(self.path.read_bytes(),b'existing\nnew\n')
    def test_symlink_log_is_rejected_without_touching_target(self):
        target=self.root/'target';target.write_bytes(b'keep');target.chmod(0o644)
        self.path.symlink_to(target)
        with self.assertRaises(OSError):guard.private_append(self.path)
        self.assertEqual(target.read_bytes(),b'keep')
        self.assertEqual(target.stat().st_mode & 0o777,0o644)
    def test_hard_link_log_is_rejected(self):
        target=self.root/'target';target.write_bytes(b'keep');os.link(target,self.path)
        with self.assertRaises(guard.Refused):guard.private_append(self.path)
    def test_fifo_is_rejected_without_waiting_for_reader(self):
        os.mkfifo(self.path)
        with self.assertRaises((OSError,guard.Refused)):guard.private_append(self.path)
    def test_different_owner_is_rejected(self):
        self.path.write_bytes(b'keep')
        with patch.object(guard.os,'getuid',return_value=os.getuid()+1):
            with self.assertRaises(guard.Refused):guard.private_append(self.path)

if __name__=='__main__':unittest.main()
