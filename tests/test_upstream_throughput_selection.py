import inspect
import unittest
from unittest import mock

import upstream_pool
import vpngate_manager as manager


NOW = 1_000_000.0


def snapshot() -> dict:
    nodes = {}
    for node_id, state in (
        ("trusted-a", "trusted"),
        ("trusted-b", "trusted"),
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
            "hard_gate_expires_at": NOW + 1000,
            "actual_country": "JP",
            "actual_ip_type": "residential",
            "risk_free": True,
            "source_seen_at": NOW - 10,
        }
    return {
        "schema_version": 2,
        "sequence": 1,
        "generated_at": "1970-01-12T13:46:40+00:00",
        "source_instance": "tokyo",
        "nodes": nodes,
        "pools": {
            "trusted_ids": ["trusted-a", "trusted-b"],
            "observation_ids": ["observation"],
            "candidate_ids": ["candidate"],
            "standby_ids": ["trusted-a"],
        },
    }


class UpstreamThroughputSelectionTests(unittest.TestCase):
    def test_consumer_probe_is_bounded_to_one_25mb_sample(self):
        self.assertEqual(25_000_000, manager.UPSTREAM_LOCAL_PROBE_BYTES)
        self.assertEqual(1, manager.UPSTREAM_LOCAL_PROBE_SAMPLES)
        source = inspect.getsource(manager.lightweight_probe_node_by_id)
        self.assertNotIn("score_ip_reputation", source)
        self.assertNotIn("enrich_ip_info", source)

    def test_standby_nodes_rank_fresh_local_speed_with_observation_fallback(self):
        with mock.patch.object(manager, "is_upstream_consumer", return_value=True), \
             mock.patch.object(manager, "load_upstream_snapshot", return_value=snapshot()), \
             mock.patch.object(manager.upstream_pool, "load_probe_state", return_value={
                 "trusted-a": {"config_hash": "hash-trusted-a", "last_probe_at": NOW - 10, "last_probe_success": True, "local_download_mbps": 100},
                 "trusted-b": {"config_hash": "hash-trusted-b", "last_probe_at": NOW - 10, "last_probe_success": True, "local_download_mbps": 80},
                 "observation": {"config_hash": "hash-observation", "last_probe_at": NOW - 10, "last_probe_success": True, "local_download_mbps": 500},
                 "candidate": {"config_hash": "hash-candidate", "last_probe_at": NOW - 10, "last_probe_success": True, "local_download_mbps": 110},
             }), \
             mock.patch.object(manager, "read_nodes", return_value=[
                 {"id": "trusted-a", "active": False},
                 {"id": "trusted-b", "active": False},
                 {"id": "observation", "active": False},
                 {"id": "candidate", "active": False},
             ]):
            result = manager.standby_nodes(
                [
                    {"id": "trusted-a", "active": False},
                    {"id": "trusted-b", "active": False},
                    {"id": "observation", "active": False},
                    {"id": "candidate", "active": False},
                ],
                {"routing_mode": "auto", "routing_ip_type": "all"},
                now=NOW,
            )
        self.assertEqual(
            ["trusted-a", "trusted-b", "observation", "candidate"],
            [item["id"] for item in result],
        )

    def test_refresh_uses_local_rank_for_consumer_standby_state(self):
        probe_state = {
            "trusted-a": {"config_hash": "hash-trusted-a", "last_probe_at": NOW - 10, "last_probe_success": True, "local_download_mbps": 100, "local_measured_at": NOW - 10},
            "trusted-b": {"config_hash": "hash-trusted-b", "last_probe_at": NOW - 10, "last_probe_success": True, "local_download_mbps": 80, "local_measured_at": NOW - 10},
            "observation": {"config_hash": "hash-observation", "last_probe_at": NOW - 10, "last_probe_success": True, "local_download_mbps": 500, "local_measured_at": NOW - 10},
            "candidate": {"config_hash": "hash-candidate", "last_probe_at": NOW - 10, "last_probe_success": True, "local_download_mbps": 110, "local_measured_at": NOW - 10},
        }
        with mock.patch.object(manager, "load_upstream_snapshot", return_value=snapshot()), \
             mock.patch.object(manager.upstream_pool, "load_probe_state", return_value=probe_state), \
             mock.patch.object(manager, "read_nodes", return_value=[]), \
             mock.patch.object(manager.time, "time", return_value=NOW), \
             mock.patch.object(manager.node_pool, "persist_config"):
            result = manager.refresh_upstream_pool_state(persist=False)
        self.assertEqual(
            ["trusted-a", "trusted-b", "observation", "candidate"],
            result["standby"],
        )

    def test_probe_queue_never_exceeds_ten(self):
        nodes = []
        for index in range(12):
            nodes.append(
                {
                    "id": f"node-{index}",
                    "pool_state": "candidate",
                    "hard_gate": "passed",
                    "hard_gate_expires_at": NOW + 1000,
                    "actual_country": "JP",
                    "actual_ip_type": "residential",
                    "risk_free": True,
                    "config_hash": f"hash-{index}",
                }
            )
        self.assertEqual(10, len(upstream_pool.probe_queue_ids(nodes, limit=10, now=NOW)))


if __name__ == "__main__":
    unittest.main()
