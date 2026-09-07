"""Task 2: proxy_server 按连接动态绑定活动槽设备。"""

import os
import socket
import tempfile
import unittest
from unittest import mock

import proxy_server
import slot_state


def _build_dns_response(query: bytes) -> bytes:
    tx_id = query[:2]
    header = tx_id + b"\x81\x80" + b"\x00\x01" + b"\x00\x01" + b"\x00\x00\x00\x00"
    question = query[12:]
    answer = (
        b"\xc0\x0c"  # 指向问题段的名称指针
        + b"\x00\x01\x00\x01"  # A / IN
        + b"\x00\x00\x00\x3c"  # TTL
        + b"\x00\x04"  # rdlength
        + bytes([203, 0, 113, 10])
    )
    return header + question + answer


def _make_fake_udp_socket():
    fake = mock.Mock()
    sent = {}

    def sendto(packet, addr):
        sent["packet"] = packet
        return len(packet)

    def recvfrom(size):
        return _build_dns_response(sent["packet"]), ("8.8.8.8", 53)

    fake.sendto.side_effect = sendto
    fake.recvfrom.side_effect = recvfrom
    return fake


class ResolveActiveDeviceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data_dir = self.tmp.name

    def test_pointer_to_slot_a_returns_tun0(self):
        slot_state.write_active_slot(self.data_dir, "A", "node-1")
        with mock.patch.object(proxy_server.os.path, "isdir", return_value=True):
            self.assertEqual("tun0", proxy_server.resolve_active_device(self.data_dir))

    def test_pointer_to_slot_b_returns_tun1(self):
        slot_state.write_active_slot(self.data_dir, "B", "node-2")
        with mock.patch.object(proxy_server.os.path, "isdir", return_value=True):
            self.assertEqual("tun1", proxy_server.resolve_active_device(self.data_dir))

    def test_no_pointer_raises_3004(self):
        with self.assertRaisesRegex(OSError, "3004"):
            proxy_server.resolve_active_device(self.data_dir)

    def test_cleared_pointer_raises_3004(self):
        slot_state.write_active_slot(self.data_dir, "A", "node-1")
        slot_state.clear_active_slot(self.data_dir)
        with self.assertRaisesRegex(OSError, "3004"):
            proxy_server.resolve_active_device(self.data_dir)

    def test_corrupt_pointer_raises_3004(self):
        with open(os.path.join(self.data_dir, "active_slot.json"), "w") as fh:
            fh.write("{not json")
        with self.assertRaisesRegex(OSError, "3004"):
            proxy_server.resolve_active_device(self.data_dir)

    def test_missing_device_raises_3004(self):
        slot_state.write_active_slot(self.data_dir, "A", "node-1")
        with mock.patch.object(proxy_server.os.path, "isdir", return_value=False):
            with self.assertRaisesRegex(OSError, "3004"):
                proxy_server.resolve_active_device(self.data_dir)


class CreateConnectionFailClosedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data_dir = self.tmp.name

    def test_no_pointer_never_touches_socket(self):
        with (
            mock.patch.object(proxy_server, "PROXY_DATA_DIR", self.data_dir),
            mock.patch.object(proxy_server.socket, "socket") as socket_ctor,
        ):
            with self.assertRaisesRegex(OSError, "ERR_ROUTE_DEV_NOT_FOUND"):
                proxy_server.create_connection(("example.com", 443))
        socket_ctor.assert_not_called()

    def test_missing_device_never_touches_socket(self):
        slot_state.write_active_slot(self.data_dir, "B", "node-2")
        with (
            mock.patch.object(proxy_server, "PROXY_DATA_DIR", self.data_dir),
            mock.patch.object(proxy_server.os.path, "isdir", return_value=False),
            mock.patch.object(proxy_server.socket, "socket") as socket_ctor,
        ):
            with self.assertRaisesRegex(OSError, "ERR_ROUTE_DEV_NOT_FOUND"):
                proxy_server.create_connection(("example.com", 443))
        socket_ctor.assert_not_called()

    def test_dns_resolution_also_fail_closed_without_pointer(self):
        with (
            mock.patch.object(proxy_server, "PROXY_DATA_DIR", self.data_dir),
            mock.patch.object(proxy_server.socket, "socket") as socket_ctor,
        ):
            with self.assertRaisesRegex(OSError, "3004"):
                proxy_server.resolve_dns_over_tun("example.com", qtype="A")
        socket_ctor.assert_not_called()


class CreateConnectionDeviceBindingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data_dir = self.tmp.name

    def test_binds_pointer_device_tun1(self):
        slot_state.write_active_slot(self.data_dir, "B", "node-2")
        fake_tcp = mock.Mock()
        address = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("203.0.113.10", 443))
        with (
            mock.patch.object(proxy_server, "PROXY_DATA_DIR", self.data_dir),
            mock.patch.object(proxy_server.os.path, "isdir", return_value=True),
            mock.patch.object(proxy_server.socket, "getaddrinfo", return_value=[address]),
            mock.patch.object(proxy_server.socket, "socket", return_value=fake_tcp),
        ):
            conn = proxy_server.create_connection(("203.0.113.10", 443))
        self.assertIs(conn, fake_tcp)
        fake_tcp.setsockopt.assert_any_call(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, b"tun1")
        fake_tcp.connect.assert_called_once_with(("203.0.113.10", 443))

    def test_dns_and_tcp_sockets_use_same_device(self):
        slot_state.write_active_slot(self.data_dir, "B", "node-2")
        fake_udp = _make_fake_udp_socket()
        fake_tcp = mock.Mock()
        sockets = []

        def socket_factory(*args, **kwargs):
            if args[1] == socket.SOCK_DGRAM:
                sockets.append(fake_udp)
                return fake_udp
            sockets.append(fake_tcp)
            return fake_tcp

        address = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("203.0.113.10", 443))
        with (
            mock.patch.object(proxy_server, "PROXY_DATA_DIR", self.data_dir),
            mock.patch.object(proxy_server.os.path, "isdir", return_value=True),
            mock.patch.object(proxy_server.socket, "getaddrinfo", return_value=[address]),
            mock.patch.object(proxy_server.socket, "socket", side_effect=socket_factory),
        ):
            proxy_server.create_connection(("example.com", 443))

        fake_udp.setsockopt.assert_any_call(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, b"tun1")
        fake_tcp.setsockopt.assert_any_call(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, b"tun1")
        for sock in sockets:
            for call in sock.setsockopt.call_args_list:
                if call.args[:2] == (socket.SOL_SOCKET, socket.SO_BINDTODEVICE):
                    self.assertEqual(b"tun1", call.args[2])

    def test_explicit_device_bypasses_pointer(self):
        # 无指针时显式 device 仍可工作（供探针/测试绕过）
        fake_tcp = mock.Mock()
        address = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("203.0.113.10", 443))
        with (
            mock.patch.object(proxy_server, "PROXY_DATA_DIR", self.data_dir),
            mock.patch.object(proxy_server.socket, "getaddrinfo", return_value=[address]),
            mock.patch.object(proxy_server.socket, "socket", return_value=fake_tcp),
        ):
            conn = proxy_server.create_connection(("203.0.113.10", 443), device="tun0")
        self.assertIs(conn, fake_tcp)
        fake_tcp.setsockopt.assert_any_call(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, b"tun0")

    def test_pointer_reread_on_each_connection(self):
        slot_state.write_active_slot(self.data_dir, "A", "node-1")
        fake_tcp = mock.Mock()
        address = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("203.0.113.10", 443))
        with (
            mock.patch.object(proxy_server, "PROXY_DATA_DIR", self.data_dir),
            mock.patch.object(proxy_server.os.path, "isdir", return_value=True),
            mock.patch.object(proxy_server.socket, "getaddrinfo", return_value=[address]),
            mock.patch.object(proxy_server.socket, "socket", return_value=fake_tcp),
        ):
            proxy_server.create_connection(("203.0.113.10", 443))
            slot_state.write_active_slot(self.data_dir, "B", "node-2")
            proxy_server.create_connection(("203.0.113.10", 443))
        bind_calls = [
            c for c in fake_tcp.setsockopt.call_args_list
            if c.args[:2] == (socket.SOL_SOCKET, socket.SO_BINDTODEVICE)
        ]
        self.assertEqual([b"tun0", b"tun1"], [c.args[2] for c in bind_calls])


class CompatWrapperTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data_dir = self.tmp.name

    def test_resolve_dns_over_tun0_still_callable(self):
        slot_state.write_active_slot(self.data_dir, "A", "node-1")
        fake_udp = _make_fake_udp_socket()
        with (
            mock.patch.object(proxy_server, "PROXY_DATA_DIR", self.data_dir),
            mock.patch.object(proxy_server.os.path, "isdir", return_value=True),
            mock.patch.object(proxy_server.socket, "socket", return_value=fake_udp),
        ):
            result = proxy_server.resolve_dns_over_tun0("example.com")
        self.assertEqual("203.0.113.10", result)
        fake_udp.setsockopt.assert_any_call(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, b"tun0")

    def test_dns_query_over_tun0_still_callable_with_device(self):
        fake_udp = _make_fake_udp_socket()
        with mock.patch.object(proxy_server.socket, "socket", return_value=fake_udp):
            result = proxy_server.dns_query_over_tun0("example.com", 1, "8.8.8.8", 5, device="tun1")
        self.assertEqual("203.0.113.10", result)
        fake_udp.setsockopt.assert_any_call(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, b"tun1")

    def test_resolve_dns_over_tun_with_qtype(self):
        fake_udp = _make_fake_udp_socket()
        with mock.patch.object(proxy_server.socket, "socket", return_value=fake_udp):
            result = proxy_server.resolve_dns_over_tun("example.com", qtype="A", device="tun1")
        self.assertEqual("203.0.113.10", result)

    def test_resolve_dns_over_tun_passthrough_literal_ip(self):
        with mock.patch.object(proxy_server.socket, "socket") as socket_ctor:
            self.assertEqual("203.0.113.10", proxy_server.resolve_dns_over_tun("203.0.113.10", device="tun1"))
        socket_ctor.assert_not_called()


if __name__ == "__main__":
    unittest.main()
