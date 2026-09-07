import tempfile
import unittest
from pathlib import Path

import upstream_pool


NOW = 1_000_000.0


def snapshot() -> dict:
    nodes = {}
    for node_id, state in (
        ("standby", "trusted"),
        ("trusted", "trusted"),
        ("observation", "observation"),
        ("candidate", "candidate"),
    ):
        nodes[node_id] = {
            "config_text": f"client\nremote {node_id}.example 443\n",
            "config_hash": f"hash-{node_id}",
            "upstream_pool_state": state,
            "upstream_score": 90,
            "upstream_verified_at": NOW - 10,
            "upstream_expires_at": NOW + 1000,
            "hard_gate": "passed",
            "hard_gate_checked_at": NOW - 10,
            "hard_gate_expires_at": NOW + 10000,
            "actual_country": "JP",
            "actual_ip_type": "residential",
            "risk_free": True,
            "source_seen_at": NOW - 10,
        }
    return {
        "schema_version": 2,
        "sequence": 1,
        "generated_at": "2026-08-15T00:00:00+00:00",
        "source_instance": "tokyo",
        "nodes": nodes,
        "pools": {
            "trusted_ids": ["standby", "trusted"],
            "observation_ids": ["observation"],
            "candidate_ids": ["candidate"],
            "standby_ids": ["standby"],
        },
    }


class UpstreamPoolTests(unittest.TestCase):
    def test_order_is_standby_then_remaining_trusted_then_observation(self):
        self.assertEqual(
            ["standby", "trusted", "observation", "candidate"],
            upstream_pool.ordered_allowed_ids(snapshot()),
        )

    def test_candidate_is_connectable_after_upstream_hard_gate(self):
        value = snapshot()
        self.assertEqual({"candidate"}, upstream_pool.candidate_ids(value))
        selected = upstream_pool.select_connectable_nodes(value, {}, NOW)
        self.assertEqual(
            ["standby", "trusted", "observation", "candidate"],
            [item["id"] for item in selected],
        )

    def test_config_hash_change_invalidates_success(self):
        state = upstream_pool.record_probe_result(
            {}, "standby", "hash-standby", True, "", NOW, 1800
        )
        self.assertTrue(
            upstream_pool.probe_is_current(state, "standby", "hash-standby")
        )
        self.assertFalse(
            upstream_pool.probe_is_current(state, "standby", "new-hash")
        )

    def test_failure_cools_node_then_allows_it_again(self):
        state = upstream_pool.record_probe_result(
            {}, "standby", "hash-standby", False, "openvpn", NOW, 1800
        )
        during = upstream_pool.select_connectable_nodes(snapshot(), state, NOW + 60)
        after = upstream_pool.select_connectable_nodes(snapshot(), state, NOW + 1801)
        self.assertNotIn("standby", [item["id"] for item in during])
        self.assertEqual("standby", after[0]["id"])

    def test_snapshot_nodes_use_managed_config_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            config_dir = Path(directory) / "configs"
            nodes = upstream_pool.snapshot_nodes(snapshot(), config_dir)
            mapped = {item["id"]: item for item in nodes}
            self.assertEqual(config_dir / "standby.ovpn", Path(mapped["standby"]["config_file"]))
            self.assertEqual("trusted", mapped["standby"]["pool_state"])
            self.assertEqual("not_checked", mapped["standby"]["probe_status"])
            self.assertEqual("passed", mapped["standby"]["hard_gate"])
            self.assertEqual(4, len(nodes))

    def test_probe_queue_prefers_unmeasured_primary_then_candidate_and_caps_at_ten(self):
        nodes = upstream_pool.snapshot_nodes(snapshot(), Path("/tmp/configs"))
        state = {
            "trusted": {
                "config_hash": "hash-trusted",
                "last_probe_at": NOW - 10,
                "last_probe_success": True,
                "local_download_mbps": 100,
                "local_measured_at": NOW - 10,
            }
        }
        queue = upstream_pool.probe_queue_ids(nodes, limit=10, now=NOW, probe_state=state)
        self.assertEqual(["standby", "observation", "candidate"], queue)

    def test_rank_prefers_trusted_speed_and_allows_fast_candidate_challenge(self):
        nodes = upstream_pool.snapshot_nodes(snapshot(), Path("/tmp/configs"))
        for node in nodes:
            node["local_measured_at"] = NOW - 10
            node["last_probe_success"] = True
            node["local_download_mbps"] = {
                "standby": 100,
                "trusted": 80,
                "observation": 500,
                "candidate": 140,
            }[node["id"]]
        ordered = upstream_pool.rank_local_nodes(nodes, now=NOW)
        self.assertEqual(["candidate", "standby", "trusted", "observation"], [item["id"] for item in ordered])

    def test_probe_state_persists_speed_and_prunes_stale_nodes(self):
        state = upstream_pool.record_probe_result(
            {}, "candidate", "hash-candidate", True, "", NOW, 1800,
            local_download_mbps=123.4,
            local_ttfb_ms=42,
            local_probe_bytes=25_000_000,
            local_probe_path="isolated",
        )
        self.assertEqual(123.4, state["candidate"]["local_download_mbps"])
        self.assertEqual(42, state["candidate"]["local_ttfb_ms"])
        nodes = [{"id": "candidate", "config_hash": "hash-candidate"}]
        state["missing"] = {"config_hash": "hash-missing", "last_probe_at": NOW}
        state["old"] = {"config_hash": "hash-old", "last_probe_at": NOW - 9999}
        cleaned = upstream_pool.prune_probe_state(state, nodes, NOW, retention_seconds=3600)
        self.assertEqual({"candidate"}, set(cleaned))


if __name__ == "__main__":
    unittest.main()
