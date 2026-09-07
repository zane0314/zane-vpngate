"""Task 4 失败测试：health_probes 模块（VPNGate 槽探针 + 纯函数故障矩阵）。"""

import os
import subprocess
import tempfile
import unittest
from unittest import mock

import health_probes


class ClassifyFailureMatrixTests(unittest.TestCase):
    MATRIX = [
        # (vpngate, vless, trojan, attribution, action, count_node_failure, pause_performance_switch)
        (True, False, True, "vless-only", "hold", False, False),
        (True, True, False, "trojan-only", "hold", False, False),
        (True, False, False, "frontend-stack", "hold", False, True),
        (False, True, True, "vpngate-exit", "failover", True, False),
        (False, False, False, "host-stack", "pause_rotation", False, True),
        (False, False, True, "vpngate-exit+vless", "failover", True, False),
        (False, True, False, "vpngate-exit+trojan", "failover", True, False),
    ]

    def test_matrix_rows(self):
        for vpngate, vless, trojan, attribution, action, count, pause_perf in self.MATRIX:
            with self.subTest(vpngate=vpngate, vless=vless, trojan=trojan):
                result = health_probes.classify_failure(vpngate, vless, trojan)
                self.assertEqual(result["attribution"], attribution)
                self.assertEqual(result["action"], action)
                self.assertIs(result["count_node_failure"], count)
                self.assertIs(result["pause_performance_switch"], pause_perf)
                self.assertIn("alert", result)
                self.assertIsNotNone(result["alert"])

    def test_healthy_row(self):
        result = health_probes.classify_failure(True, True, True)
        self.assertEqual(result, {
            "attribution": "healthy",
            "action": "hold",
            "count_node_failure": False,
            "pause_performance_switch": False,
            "alert": None,
        })

    def test_pure_function_no_side_effects(self):
        before = dict(health_probes.__dict__)
        health_probes.classify_failure(False, False, False)
        self.assertEqual(dict(health_probes.__dict__), before)


class ProbeVpngateSlotTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.data_dir = os.path.join(self._tmp.name, "data")
        self.net_root = os.path.join(self._tmp.name, "sys-class-net")
        os.makedirs(self.data_dir)
        os.makedirs(self.net_root)

    def _make_device(self, device="tun0"):
        os.makedirs(os.path.join(self.net_root, device))

    def _egress_ok(self, device):
        return {"ok": True, "exit_ip": "203.0.113.9"}

    def _probe(self, **kwargs):
        kwargs.setdefault("process_alive", lambda slot, data_dir: True)
        kwargs.setdefault("egress_probe", self._egress_ok)
        kwargs.setdefault("net_root", self.net_root)
        kwargs.setdefault("baseline_ip", "198.51.100.1")
        return health_probes.probe_vpngate_slot("A", self.data_dir, **kwargs)

    def test_happy_path(self):
        self._make_device()
        result = self._probe()
        self.assertTrue(result["ok"])
        self.assertIsInstance(result["checked_at"], float)
        self.assertIsInstance(result["detail"], str)
        self.assertEqual(set(result), {"ok", "detail", "checked_at"})

    def test_invalid_slot_raises(self):
        with self.assertRaises(ValueError):
            health_probes.probe_vpngate_slot("C", self.data_dir)

    def test_process_dead(self):
        self._make_device()
        egress = mock.Mock(return_value={"ok": True, "exit_ip": "203.0.113.9"})
        result = self._probe(process_alive=lambda slot, data_dir: False,
                             egress_probe=egress)
        self.assertFalse(result["ok"])
        self.assertIn("process", result["detail"])
        egress.assert_not_called()

    def test_device_missing(self):
        egress = mock.Mock(return_value={"ok": True, "exit_ip": "203.0.113.9"})
        result = self._probe(egress_probe=egress)
        self.assertFalse(result["ok"])
        self.assertIn("device", result["detail"])
        egress.assert_not_called()

    def test_exit_ip_equals_baseline(self):
        self._make_device()
        result = self._probe(egress_probe=lambda device: {"ok": True, "exit_ip": "198.51.100.1"})
        self.assertFalse(result["ok"])
        self.assertIn("baseline", result["detail"])

    def test_baseline_unavailable_fails_closed(self):
        self._make_device()
        result = self._probe(baseline_ip=None)
        self.assertFalse(result["ok"])
        self.assertIn("baseline", result["detail"])

    def test_egress_probe_failure(self):
        self._make_device()
        result = self._probe(egress_probe=lambda device: {"ok": False, "error": "no route"})
        self.assertFalse(result["ok"])
        self.assertIn("no route", result["detail"])

    def test_egress_probe_bound_to_slot_device(self):
        self._make_device("tun2")
        egress = mock.Mock(return_value={"ok": True, "exit_ip": "203.0.113.9"})
        result = health_probes.probe_vpngate_slot(
            "B", self.data_dir,
            process_alive=lambda slot, data_dir: True,
            egress_probe=egress,
            net_root=self.net_root,
            baseline_ip="198.51.100.1",
        )
        self.assertTrue(result["ok"])
        egress.assert_called_once_with("tun2")

    def test_default_process_check_without_pidfile(self):
        self._make_device()
        result = health_probes.probe_vpngate_slot(
            "A", self.data_dir,
            egress_probe=self._egress_ok,
            net_root=self.net_root,
            baseline_ip="198.51.100.1",
        )
        self.assertFalse(result["ok"])
        self.assertIn("process", result["detail"])

    def test_default_process_check_with_live_pidfile(self):
        self._make_device()
        with open(os.path.join(self.data_dir, "openvpn_slot_A.pid"), "w") as fh:
            fh.write(str(os.getpid()))
        alive = health_probes.default_slot_process_alive("A", self.data_dir)
        self.assertTrue(alive)

    def test_default_process_check_with_dead_pidfile(self):
        with open(os.path.join(self.data_dir, "openvpn_slot_A.pid"), "w") as fh:
            fh.write("not-a-pid")
        self.assertFalse(health_probes.default_slot_process_alive("A", self.data_dir))


class ProbeFrontendTests(unittest.TestCase):
    def test_not_configured_none(self):
        result = health_probes.probe_frontend("vless", None)
        self.assertTrue(result["ok"])
        self.assertEqual(result["detail"], "not-configured")
        self.assertIsInstance(result["checked_at"], float)

    def test_not_configured_missing_keys(self):
        for cfg in ({}, {"host": "example.com"}, {"port": 443}):
            with self.subTest(cfg=cfg):
                result = health_probes.probe_frontend("trojan", cfg)
                self.assertTrue(result["ok"])
                self.assertEqual(result["detail"], "not-configured")

    def test_configured_tcp_success(self):
        fake_socket = mock.Mock()
        with mock.patch.object(health_probes.socket, "socket", return_value=fake_socket) as socket_cls:
            result = health_probes.probe_frontend("vless", {"host": "127.0.0.1", "port": 8443})
        self.assertTrue(result["ok"])
        fake_socket.connect.assert_called_once_with(("127.0.0.1", 8443))
        fake_socket.close.assert_called_once()

    def test_configured_tcp_failure(self):
        fake_socket = mock.Mock()
        fake_socket.connect.side_effect = OSError("refused")
        with mock.patch.object(health_probes.socket, "socket", return_value=fake_socket):
            result = health_probes.probe_frontend("vless", {"host": "127.0.0.1", "port": 8443})
        self.assertFalse(result["ok"])
        self.assertIn("refused", result["detail"])
        fake_socket.close.assert_called_once()


class ProbeFrontendProxyTests(unittest.TestCase):
    CFG = {
        "probe_proxy": "socks5h://127.0.0.1:18081",
        "probe_url": "http://127.0.0.1:18978/",
    }

    def _completed(self, returncode=0, stdout="ok"):
        return subprocess.CompletedProcess(args=["curl"], returncode=returncode,
                                           stdout=stdout, stderr="")

    def test_proxy_curl_success_with_expect(self):
        with mock.patch.object(health_probes.subprocess, "run",
                               return_value=self._completed()) as run:
            result = health_probes.probe_frontend("vless", dict(self.CFG))
        self.assertTrue(result["ok"])
        self.assertIn("proxy-ok", result["detail"])
        cmd = run.call_args[0][0]
        self.assertEqual(cmd[:2], ["curl", "-s"])
        self.assertEqual(cmd[cmd.index("--max-time") + 1], "8")
        self.assertEqual(cmd[cmd.index("-x") + 1], "socks5h://127.0.0.1:18081")
        self.assertEqual(cmd[-1], "http://127.0.0.1:18978/")

    def test_proxy_custom_expect_and_timeout(self):
        cfg = dict(self.CFG, expect="pong", timeout=3)
        with mock.patch.object(health_probes.subprocess, "run",
                               return_value=self._completed(stdout="pong pong")) as run:
            result = health_probes.probe_frontend("trojan", cfg)
        self.assertTrue(result["ok"])
        cmd = run.call_args[0][0]
        self.assertEqual(cmd[cmd.index("--max-time") + 1], "3")

    def test_proxy_curl_nonzero_exit(self):
        with mock.patch.object(health_probes.subprocess, "run",
                               return_value=self._completed(returncode=7, stdout="")):
            result = health_probes.probe_frontend("vless", dict(self.CFG))
        self.assertFalse(result["ok"])
        self.assertIn("7", result["detail"])

    def test_proxy_curl_timeout(self):
        with mock.patch.object(health_probes.subprocess, "run",
                               side_effect=subprocess.TimeoutExpired(cmd="curl", timeout=10)):
            result = health_probes.probe_frontend("vless", dict(self.CFG))
        self.assertFalse(result["ok"])
        self.assertIn("timeout", result["detail"])

    def test_proxy_response_missing_expect(self):
        with mock.patch.object(health_probes.subprocess, "run",
                               return_value=self._completed(stdout="nope")):
            result = health_probes.probe_frontend("vless", dict(self.CFG))
        self.assertFalse(result["ok"])
        self.assertIn("expect", result["detail"])

    def test_no_probe_proxy_falls_back_to_tcp(self):
        fake_socket = mock.Mock()
        with mock.patch.object(health_probes.subprocess, "run") as run, \
                mock.patch.object(health_probes.socket, "socket", return_value=fake_socket):
            result = health_probes.probe_frontend("vless", {"host": "127.0.0.1", "port": 8443})
        self.assertTrue(result["ok"])
        run.assert_not_called()
        fake_socket.connect.assert_called_once_with(("127.0.0.1", 8443))

    def test_none_cfg_not_configured(self):
        with mock.patch.object(health_probes.subprocess, "run") as run:
            result = health_probes.probe_frontend("vless", None)
        self.assertTrue(result["ok"])
        self.assertEqual(result["detail"], "not-configured")
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
