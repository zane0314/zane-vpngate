"""Task 6 失败测试：双槽状态暴露（state.json 新键 / check_proxy_health 活动槽设备 / env 默认值）。"""

import json
import os
import tempfile
import time
import unittest
from unittest import mock

_DATA_TEMP = tempfile.TemporaryDirectory()
os.environ.setdefault("VPNGATE_DATA_DIR", _DATA_TEMP.name)

import slot_state
import vpngate_manager as manager


class DualSlotStatusTests(unittest.TestCase):
    def setUp(self):
        manager.ensure_dirs()
        self._runtime_backup = {
            slot: dict(state) for slot, state in manager.SLOT_RUNTIME.items()
        }
        for slot in manager.SLOT_RUNTIME:
            manager.SLOT_RUNTIME[slot]["process"] = None
            manager.SLOT_RUNTIME[slot]["node_id"] = None
            manager.SLOT_RUNTIME[slot]["draining_since"] = None
        for name in ("active_slot.json", "slot_states.json", "state.json"):
            try:
                os.unlink(manager.DATA_DIR / name)
            except OSError:
                pass

    def tearDown(self):
        for slot, state in self._runtime_backup.items():
            manager.SLOT_RUNTIME[slot].update(state)
        manager._sync_legacy_active_state()
        for name in ("active_slot.json", "slot_states.json", "state.json"):
            try:
                os.unlink(manager.DATA_DIR / name)
            except OSError:
                pass

    def test_get_state_exposes_dual_slot_keys_with_defaults(self):
        state = manager.get_state()
        self.assertIsNone(state["active_slot"])
        self.assertIsNone(state["draining_slot"])
        self.assertEqual(0, state["switch_cooldown_until"])
        self.assertFalse(state["rotation_paused"])
        self.assertIsNone(state["last_switch_at"])
        self.assertIsNone(state["last_failover_reason"])
        slots = state["slots"]
        self.assertEqual({"A", "B"}, set(slots))
        for slot_name, device in (("A", "tun0"), ("B", "tun2")):
            info = slots[slot_name]
            for key in ("node_id", "device", "exit_ip", "verified_at", "health"):
                self.assertIn(key, info, f"slots.{slot_name} 缺少 {key}")
            self.assertEqual(device, info["device"])
            self.assertIsNone(info["node_id"])
            self.assertIsNone(info["exit_ip"])
        # 槽位视图必须可 JSON 序列化（不得混入 process 等运行时对象）
        json.dumps(state)

    def test_get_state_reflects_slot_info_after_connect(self):
        now = time.time()
        slot_state.write_slot_states(
            str(manager.DATA_DIR),
            {
                "A": {
                    "node_id": "jp_node_1",
                    "device": "tun0",
                    "exit_ip": "203.0.113.10",
                    "asn": "AS0000",
                    "verified_at": now,
                    "health": "ok",
                }
            },
        )
        slot_state.write_active_slot(str(manager.DATA_DIR), "A", "jp_node_1")
        manager.SLOT_RUNTIME["A"]["node_id"] = "jp_node_1"
        manager.SLOT_RUNTIME["B"]["draining_since"] = now

        state = manager.get_state()
        self.assertEqual("A", state["active_slot"])
        self.assertEqual("B", state["draining_slot"])
        slot_a = state["slots"]["A"]
        self.assertEqual("jp_node_1", slot_a["node_id"])
        self.assertEqual("tun0", slot_a["device"])
        self.assertEqual("203.0.113.10", slot_a["exit_ip"])
        self.assertEqual(now, slot_a["verified_at"])
        self.assertEqual("ok", slot_a["health"])

    def test_check_proxy_health_without_active_slot_fails_without_socket(self):
        with mock.patch.object(manager.socket, "socket") as socket_mock:
            result = manager.check_proxy_health()
        socket_mock.assert_not_called()
        self.assertFalse(result["ok"])
        self.assertIn("无活动槽", result["error"])

    def test_dual_slot_env_defaults(self):
        self.assertEqual(60, manager.DRAIN_SECONDS)
        self.assertEqual(600, manager.SWITCH_COOLDOWN_SECONDS)
        self.assertEqual(21600, manager.NODE_FAILURE_COOLDOWN_SECONDS)
        self.assertEqual(2, manager.VPNGATE_PROBE_FAIL_THRESHOLD)

    def test_upstream_env_profile_contains_dual_slot_tunables(self):
        profile = (manager.ROOT_DIR / "config" / "aimilivpn-upstream.env").read_text(
            encoding="utf-8"
        )
        for line in (
            "DRAIN_SECONDS=60",
            "SWITCH_COOLDOWN_SECONDS=600",
            "NODE_FAILURE_COOLDOWN_SECONDS=21600",
            "VPNGATE_PROBE_FAIL_THRESHOLD=2",
        ):
            self.assertIn(line, profile)


if __name__ == "__main__":
    unittest.main()
