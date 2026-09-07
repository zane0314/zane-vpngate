import errno
import socket
import time
import unittest
from unittest import mock

import proxy_server
import vpngate_manager as manager


class ResidentialPolicyTests(unittest.TestCase):
    def setUp(self):
        self.cfg = {
            "routing_mode": "fixed_region",
            "force_country": "日本",
            "routing_ip_type": "residential",
        }

    def test_strict_filter_rejects_mobile_hosting_and_unknown(self):
        nodes = [
            {"id": "res", "country": "日本", "ip_type": "residential"},
            {"id": "mobile", "country": "日本", "ip_type": "mobile"},
            {"id": "hosting", "country": "日本", "ip_type": "hosting"},
            {"id": "unknown", "country": "日本", "ip_type": ""},
            {"id": "other-country", "country": "美国", "ip_type": "residential"},
        ]
        with mock.patch.object(manager, "STRICT_RESIDENTIAL_ONLY", True):
            self.assertEqual(
                [node["id"] for node in manager.apply_routing_filters(nodes, self.cfg)],
                ["res"],
            )
            self.assertEqual(
                [node["id"] for node in manager.apply_routing_filters(nodes, self.cfg, include_unknown_ip_type=True)],
                ["res", "unknown"],
            )

    def test_connection_validation_is_fail_closed_for_non_residential(self):
        with mock.patch.object(manager, "STRICT_RESIDENTIAL_ONLY", True):
            manager.validate_node_allowed_by_routing(
                {"id": "res", "country": "日本", "ip_type": "residential"},
                self.cfg,
            )
            for ip_type in ("mobile", "hosting", "proxy", ""):
                with self.subTest(ip_type=ip_type):
                    with self.assertRaises(RuntimeError):
                        manager.validate_node_allowed_by_routing(
                            {"id": ip_type or "unknown", "country": "日本", "ip_type": ip_type},
                            self.cfg,
                        )

    def test_standby_pool_counts_only_fresh_inactive_residential_nodes(self):
        now = time.time()
        nodes = [
            {"id": "ready", "country": "日本", "ip_type": "residential", "actual_country": "JP", "actual_ip_type": "residential", "risk_free": True, "pool_state": "observation", "probe_status": "available", "probed_at": now, "latency_ms": 20},
            {"id": "active", "country": "日本", "ip_type": "residential", "actual_country": "JP", "actual_ip_type": "residential", "risk_free": True, "pool_state": "trusted", "probe_status": "available", "probed_at": now, "active": True},
            {"id": "stale", "country": "日本", "ip_type": "residential", "actual_country": "JP", "actual_ip_type": "residential", "risk_free": True, "pool_state": "observation", "probe_status": "available", "probed_at": now - 9999},
            {"id": "mobile", "country": "日本", "ip_type": "mobile", "probe_status": "available", "probed_at": now},
            {"id": "failed", "country": "日本", "ip_type": "residential", "probe_status": "unavailable", "probed_at": now},
        ]
        with (
            mock.patch.object(manager, "STRICT_RESIDENTIAL_ONLY", True),
            mock.patch.object(manager, "STANDBY_MAX_AGE_SECONDS", 1800),
        ):
            self.assertEqual([node["id"] for node in manager.standby_nodes(nodes, self.cfg, now=now)], ["ready"])

    def test_no_eligible_standby_enters_fail_closed_state(self):
        cfg = dict(self.cfg, connection_enabled=True)
        with (
            mock.patch.object(manager, "load_ui_config", return_value=cfg),
            mock.patch.object(manager, "read_nodes", return_value=[]),
            mock.patch.object(manager, "enter_fail_closed") as fail_closed,
            mock.patch.object(manager.threading, "Thread"),
        ):
            manager.auto_switch_node()
        fail_closed.assert_called_once()

    def test_stable_pool_rotates_only_old_non_active_pool_nodes(self):
        now = time.time()
        nodes = [
            {"id": "active", "active": True, "pool_state": "trusted", "probed_at": now - 10 * 86400},
            {"id": "recent", "pool_state": "trusted", "probed_at": now - 3600},
            {"id": "old-observation", "pool_state": "observation", "probed_at": now - 5 * 86400},
            {"id": "old-trusted", "pool_state": "trusted", "probed_at": now - 6 * 86400},
            {"id": "candidate", "pool_state": "candidate", "probed_at": now - 9 * 86400},
        ]
        nodes[2]["ip_quality_score"] = 91
        nodes[3]["ip_quality_score"] = 96
        with mock.patch.object(manager, "STABLE_POOL_PROBE_DAILY_LIMIT", 2):
            self.assertEqual(
                ["old-trusted", "old-observation"],
                manager.stable_pool_probe_ids(nodes, now=now),
            )

    def test_switch_decision_preserves_quality_priority(self):
        current = {"ip_quality_score": 96, "download_mbps": 10}
        much_worse_but_fast = {"ip_quality_score": 89, "download_mbps": 1000}
        close_and_fast = {"ip_quality_score": 93, "download_mbps": 14}
        better_quality = {"ip_quality_score": 100, "download_mbps": 5}
        with (
            mock.patch.object(manager, "QUALITY_TIE_WINDOW", 5),
            mock.patch.object(manager, "SPEED_SWITCH_GAIN_PERCENT", 30),
        ):
            self.assertEqual("", manager.optimization_candidate_reason(current, much_worse_but_fast))
            self.assertEqual("quality_close_speed_gain", manager.optimization_candidate_reason(current, close_and_fast))
            self.assertEqual("", manager.optimization_candidate_reason(current, better_quality))

    def test_large_quality_gain_wins_without_speed_gain(self):
        with mock.patch.object(manager, "QUALITY_TIE_WINDOW", 5):
            self.assertEqual(
                "weighted_ip_quality",
                manager.optimization_candidate_reason(
                    {"ip_quality_score": 90, "download_mbps": 100},
                    {"ip_quality_score": 97, "download_mbps": 10},
                ),
            )


class ProxyFailClosedTests(unittest.TestCase):
    def test_missing_tun_device_never_falls_back_to_default_route(self):
        fake_socket = mock.Mock()
        fake_socket.setsockopt.side_effect = OSError(errno.ENODEV, "No such device")
        address = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("203.0.113.10", 443))
        with (
            mock.patch.object(proxy_server, "resolve_dns_over_tun0", return_value="203.0.113.10"),
            mock.patch.object(proxy_server.socket, "getaddrinfo", return_value=[address]),
            mock.patch.object(proxy_server.socket, "socket", return_value=fake_socket),
        ):
            with self.assertRaisesRegex(OSError, "ERR_ROUTE_DEV_NOT_FOUND"):
                proxy_server.create_connection(("example.com", 443))
        fake_socket.connect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
