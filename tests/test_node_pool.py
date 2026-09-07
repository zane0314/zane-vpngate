import tempfile
import unittest
from pathlib import Path

import node_pool


NOW = 1000.0


def eligible(node_id: str, score: int = 100, successes: int = 1, composite: int = 80):
    return {
        "id": node_id,
        "probe_status": "available",
        "actual_country": "JP",
        "actual_ip_type": "residential",
        "risk_free": True,
        "ip_quality_score": score,
        "success_count": successes,
        "consecutive_successes": successes,
        "failure_count": 0,
        "composite_score": composite,
        "hard_gate": "passed",
        "hard_gate_checked_at": NOW - 10,
        "hard_gate_expires_at": NOW + 7200,
    }


class NodePoolTests(unittest.TestCase):
    def test_pool_limits_are_hard(self):
        nodes = [eligible(f"node-{index}") for index in range(30)]
        pools = node_pool.reconcile_pools(nodes, now=NOW)
        self.assertEqual(10, len(pools["trusted"]))
        self.assertEqual(10, len(pools["observation"]))
        self.assertEqual(10, sum(node["pool_state"] == "candidate" for node in nodes))

    def test_one_success_is_enough_for_trusted_when_ip_gate_passes(self):
        nodes = [eligible(f"trusted-{index}") for index in range(15)]
        pools = node_pool.reconcile_pools(nodes, now=NOW)
        self.assertEqual(10, len(pools["trusted"]))
        self.assertEqual(5, len(pools["observation"]))

    def test_lower_ip_quality_falls_to_observation_after_one_success(self):
        nodes = [eligible(f"obs-{index}", score=80) for index in range(15)]
        pools = node_pool.reconcile_pools(nodes, now=NOW)
        self.assertEqual([], pools["trusted"])
        self.assertEqual(10, len(pools["observation"]))

    def test_non_residential_never_enters_active_pools(self):
        node = eligible("hosting")
        node["actual_ip_type"] = "hosting"
        pools = node_pool.reconcile_pools([node], now=NOW)
        self.assertEqual([], pools["trusted"])
        self.assertEqual([], pools["observation"])

    def test_hard_failure_is_removed_immediately(self):
        node = eligible("failed")
        node["pool_state"] = "trusted"
        node["probe_status"] = "unavailable"
        node["hard_gate"] = "failed"
        pools = node_pool.reconcile_pools([node], now=NOW)
        self.assertEqual([], pools["trusted"])
        self.assertEqual("cooldown", node["pool_state"])

    def test_managed_config_prune_keeps_unrelated_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "keep.ovpn").write_text("keep", encoding="utf-8")
            (root / "drop.ovpn").write_text("drop", encoding="utf-8")
            (root / "manual.ovpn").write_text("manual", encoding="utf-8")
            removed = node_pool.prune_managed_configs(
                root, managed_ids={"keep", "drop"}, retained_ids={"keep"}
            )
            self.assertEqual([root / "drop.ovpn"], removed)
            self.assertTrue((root / "keep.ovpn").exists())
            self.assertTrue((root / "manual.ovpn").exists())

    def test_legacy_vpngate_config_prune_is_bounded_and_pattern_safe(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "JP_1.2.3.4_443_tcp.ovpn").write_text("keep", encoding="utf-8")
            (root / "JP_5.6.7.8_443_tcp.ovpn").write_text("drop", encoding="utf-8")
            (root / "manual.ovpn").write_text("manual", encoding="utf-8")
            managed = node_pool.managed_config_ids(root)
            removed = node_pool.prune_managed_configs(
                root,
                managed_ids=managed,
                retained_ids={"JP_1.2.3.4_443_tcp"},
            )
            self.assertEqual([root / "JP_5.6.7.8_443_tcp.ovpn"], removed)
            self.assertTrue((root / "JP_1.2.3.4_443_tcp.ovpn").exists())
            self.assertTrue((root / "manual.ovpn").exists())

    def test_auto_favorite_honors_manual_opt_out(self):
        node = eligible("clean")
        node["pool_state"] = "trusted"
        self.assertEqual(["clean"], node_pool.auto_favorite_ids([node], set()))
        self.assertEqual([], node_pool.auto_favorite_ids([node], {"clean"}))

    def test_rank_is_ip_quality_first_then_speed(self):
        clean = eligible("clean", score=96)
        clean.update(download_mbps=5, stability_score=10, network_score=10)
        fast = eligible("fast", score=90)
        fast.update(download_mbps=500, stability_score=100, network_score=100)
        self.assertEqual("clean", sorted([fast, clean], key=node_pool.rank_key)[0]["id"])

    def test_config_persistence_is_hash_deduplicated(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "node.ovpn"
            node = {"config_file": str(path), "config_text": "client\nremote 1.2.3.4 443\n"}
            self.assertTrue(node_pool.persist_config(node))
            first_mtime = path.stat().st_mtime_ns
            self.assertFalse(node_pool.persist_config(node))
            self.assertEqual(first_mtime, path.stat().st_mtime_ns)


if __name__ == "__main__":
    unittest.main()
