import inspect
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

_DATA_TEMP = tempfile.TemporaryDirectory()
os.environ.setdefault("VPNGATE_DATA_DIR", _DATA_TEMP.name)

import node_pool
import vpngate_manager as manager


def write_snapshot(path: Path) -> None:
    text = "client\nremote one.example 443\n"
    value = {
        "schema_version": 2,
        "sequence": 4,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
        "source_instance": "tokyo",
        "nodes": {
            "one": {
                "config_text": text,
                "config_hash": node_pool.config_hash(text),
                "upstream_pool_state": "trusted",
                "upstream_score": 90,
                "upstream_verified_at": time.time(),
                "upstream_expires_at": 0,
                "hard_gate": "passed",
                "hard_gate_checked_at": time.time(),
                "hard_gate_expires_at": time.time() + 7200,
                "actual_country": "JP",
                "actual_ip_type": "residential",
                "risk_free": True,
                "source_seen_at": time.time(),
            }
        },
        "pools": {
            "trusted_ids": ["one"],
            "observation_ids": [],
            "candidate_ids": [],
            "standby_ids": ["one"],
        },
    }
    path.write_text(json.dumps(value), encoding="utf-8")


class UpstreamManagerModeTests(unittest.TestCase):
    def setUp(self):
        self.old_mode = manager.POOL_SOURCE_MODE
        manager.POOL_SOURCE_MODE = "upstream-consumer"
        manager.ensure_dirs()

    def tearDown(self):
        manager.POOL_SOURCE_MODE = self.old_mode

    def test_refresh_uses_snapshot_without_local_reconciliation(self):
        write_snapshot(manager.UPSTREAM_SNAPSHOT_FILE)
        with mock.patch.object(manager.node_pool, "reconcile_pools") as reconcile:
            pools = manager.refresh_pool_state()
        reconcile.assert_not_called()
        self.assertEqual(["one"], pools["trusted"])
        self.assertEqual("one", manager.read_nodes()[0]["id"])

    def test_hard_gated_candidate_can_be_connected(self):
        manager.validate_node_allowed_by_routing(
            {
                "id": "candidate",
                "upstream_pool_state": "candidate",
                "ip_type": "residential",
                "hard_gate": "passed",
            },
            {"routing_mode": "auto", "routing_ip_type": "all"},
        )

    def test_lightweight_probe_never_calls_reputation_classification(self):
        source = inspect.getsource(manager.lightweight_probe_node_by_id)
        self.assertNotIn("score_ip_reputation", source)
        self.assertNotIn("enrich_ip_info", source)
        self.assertIn("probe_tunnel_egress", source)

    def test_lightweight_result_rejects_default_egress_fallback(self):
        self.assertFalse(manager.lightweight_probe_ok({"ok": True, "exit_ip": "1.1.1.1"}, "1.1.1.1"))
        self.assertTrue(manager.lightweight_probe_ok({"ok": True, "exit_ip": "2.2.2.2"}, "1.1.1.1"))

    def test_collector_has_dedicated_consumer_branch_before_local_maintenance(self):
        source = inspect.getsource(manager.collector_loop)
        self.assertLess(source.index("if is_upstream_consumer()"), source.index("maintain_valid_nodes"))
        self.assertIn("continue", source[source.index("if is_upstream_consumer()"):source.index("maintain_valid_nodes")])


if __name__ == "__main__":
    unittest.main()
