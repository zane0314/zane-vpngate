"""standby 槽隧道自动维护测试（云途 Task 9 Step 1 实测缺口）。

缺口：部署后 standby 槽（tun1）永不建隧道——schedule_standby_replenish 在
consumer 模式首行 return，且即使非 consumer 也只补节点池不 connect 槽位；
connect_node 默认 slot="A"，仅 failover/boot 恢复才碰 B 槽。

修复：maintain_standby_slot() 在 ①活动指针有效且 active 槽健康、
②另一槽空闲（无 process、非 draining）、③无 is_connecting 并发冲突 时，
复用 standby_nodes 的池约束选 ≠ active 节点的候选，connect_node(slot=空闲槽)
建热备（connect_node 内部 verify_slot_egress 校验出口 ≠ 直连基线且 ≠ 另一槽），
_adopt_slot_after_connect 保证不抢占健康 active 的活动指针。
"""

import os
import tempfile
import time
import unittest
from unittest import mock

_DATA_TEMP = tempfile.TemporaryDirectory()
os.environ.setdefault("VPNGATE_DATA_DIR", _DATA_TEMP.name)

import slot_state
import vpngate_manager as manager


class FakeProcess:
    def __init__(self):
        self.returncode = None
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = 0

    def wait(self, timeout=None):
        return self.returncode if self.returncode is not None else 0

    def kill(self):
        self.killed = True
        self.returncode = -9


def ok_probe(slot="A"):
    return {"ok": True, "detail": f"ok: slot {slot}", "checked_at": time.time()}


class StandbySlotMaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.old_mode = manager.POOL_SOURCE_MODE
        manager.POOL_SOURCE_MODE = "upstream-consumer"
        manager.ensure_dirs()
        self._runtime_backup = {
            slot: dict(state) for slot, state in manager.SLOT_RUNTIME.items()
        }
        for slot in manager.SLOT_RUNTIME:
            manager.SLOT_RUNTIME[slot]["process"] = None
            manager.SLOT_RUNTIME[slot]["node_id"] = None
            manager.SLOT_RUNTIME[slot]["draining_since"] = None
        self._failures_backup = dict(manager.standby_slot_failures)
        self._paused_backup = dict(manager.standby_slot_paused_until)
        for slot in manager.standby_slot_failures:
            manager.standby_slot_failures[slot] = 0
            manager.standby_slot_paused_until[slot] = 0.0
        self._old_is_connecting = manager.is_connecting
        manager.is_connecting = False
        for name in (
            "active_slot.json",
            "slot_states.json",
            "state.json",
            "nodes.json",
            "local-probe-state.json",
        ):
            try:
                os.unlink(manager.DATA_DIR / name)
            except OSError:
                pass

    def tearDown(self):
        for slot, state in self._runtime_backup.items():
            manager.SLOT_RUNTIME[slot].update(state)
        manager.standby_slot_failures.update(self._failures_backup)
        manager.standby_slot_paused_until.update(self._paused_backup)
        manager.is_connecting = self._old_is_connecting
        manager.POOL_SOURCE_MODE = self.old_mode
        manager._sync_legacy_active_state()

    def _activate_a(self):
        """A 槽活跃且健康：占用运行时、slot_states health=ok、写活动指针。"""
        manager.SLOT_RUNTIME["A"]["process"] = FakeProcess()
        manager.SLOT_RUNTIME["A"]["node_id"] = "node-a"
        slot_state.write_slot_states(str(manager.DATA_DIR), {
            "A": {"node_id": "node-a", "device": "tun0", "exit_ip": "198.51.100.1",
                  "asn": "", "verified_at": time.time(), "health": "ok"},
        })
        slot_state.write_active_slot(str(manager.DATA_DIR), "A", "node-a")
        manager._sync_legacy_active_state()

    # ---- 用例 1：active 健康 + B 空闲 → 调 connect_node(slot="B")，候选排除 active 节点 ----

    def test_healthy_active_and_idle_b_connects_standby(self):
        self._activate_a()

        def fake_connect(node_id, manual=False, slot="A"):
            manager.SLOT_RUNTIME[slot]["process"] = FakeProcess()
            manager.SLOT_RUNTIME[slot]["node_id"] = node_id
            manager.SLOT_RUNTIME[slot]["draining_since"] = None
            return f"Connected {node_id}"

        with (
            mock.patch.object(
                manager, "standby_nodes",
                return_value=[{"id": "node-a"}, {"id": "node-b"}],
            ),
            mock.patch.object(manager, "connect_node", side_effect=fake_connect) as connect,
        ):
            result = manager.maintain_standby_slot()

        self.assertTrue(result)
        connect.assert_called_once_with("node-b", slot="B")

    # ---- 用例 2：B 正在 draining → 不建 ----

    def test_draining_slot_is_not_reused(self):
        self._activate_a()
        manager.SLOT_RUNTIME["B"]["draining_since"] = time.time()

        with (
            mock.patch.object(manager, "standby_nodes", return_value=[{"id": "node-b"}]),
            mock.patch.object(manager, "connect_node") as connect,
        ):
            result = manager.maintain_standby_slot()

        self.assertFalse(result)
        connect.assert_not_called()

    # ---- 用例 3：B 已有隧道 → 不重复建 ----

    def test_occupied_standby_slot_is_not_reconnected(self):
        self._activate_a()
        manager.SLOT_RUNTIME["B"]["process"] = FakeProcess()
        manager.SLOT_RUNTIME["B"]["node_id"] = "node-b"

        with (
            mock.patch.object(manager, "standby_nodes", return_value=[{"id": "node-c"}]),
            mock.patch.object(manager, "connect_node") as connect,
        ):
            result = manager.maintain_standby_slot()

        self.assertFalse(result)
        connect.assert_not_called()

    # ---- 用例 4：无合格候选 → 不建且不报错 ----

    def test_no_eligible_candidate_connects_nothing(self):
        self._activate_a()

        with (
            mock.patch.object(manager, "standby_nodes", return_value=[{"id": "node-a"}]),
            mock.patch.object(manager, "connect_node") as connect,
        ):
            result = manager.maintain_standby_slot()

        self.assertFalse(result)
        connect.assert_not_called()

    # ---- 用例 5：standby 建连成功后指针仍指 A（不抢占）----

    def test_standby_connect_does_not_steal_active_pointer(self):
        self._activate_a()

        def fake_connect(node_id, manual=False, slot="A"):
            # 模拟 connect_node 成功路径的真实收尾：写 slot_states 后走 _adopt_slot_after_connect
            manager.SLOT_RUNTIME[slot]["process"] = FakeProcess()
            manager.SLOT_RUNTIME[slot]["node_id"] = node_id
            manager.SLOT_RUNTIME[slot]["draining_since"] = None
            states = slot_state.read_slot_states(str(manager.DATA_DIR))
            states[slot] = {
                "node_id": node_id, "device": "tun1", "exit_ip": "198.51.100.2",
                "asn": "", "verified_at": time.time(), "health": "ok",
            }
            slot_state.write_slot_states(str(manager.DATA_DIR), states)
            manager._adopt_slot_after_connect(slot, node_id)
            return f"Connected {node_id}"

        with (
            mock.patch.object(manager, "standby_nodes", return_value=[{"id": "node-b"}]),
            mock.patch.object(manager, "connect_node", side_effect=fake_connect),
            mock.patch.object(
                manager.health_probes, "probe_vpngate_slot", return_value=ok_probe("A")
            ),
            mock.patch.object(manager, "direct_public_ip", return_value="203.0.113.1"),
        ):
            result = manager.maintain_standby_slot()

        self.assertTrue(result)
        pointer = slot_state.read_active_slot(str(manager.DATA_DIR))
        self.assertEqual("A", pointer["slot"], "standby 建连不得抢占健康 active 的活动指针")
        self.assertEqual("node-a", pointer["node_id"])

    # ---- 用例 6：consumer 模式下补备触发可用（补槽不被 consumer 早退吞掉）----

    def test_consumer_mode_replenish_triggers_slot_maintenance(self):
        with mock.patch.object(manager, "maintain_standby_slot") as maintain:
            manager.schedule_standby_replenish()
        maintain.assert_called_once()

    # ---- 并发与节制：is_connecting 冲突不建；同槽连续失败 3 次暂停 10 分钟 ----

    def test_is_connecting_conflict_skips(self):
        self._activate_a()
        manager.is_connecting = True

        with (
            mock.patch.object(manager, "standby_nodes", return_value=[{"id": "node-b"}]),
            mock.patch.object(manager, "connect_node") as connect,
        ):
            result = manager.maintain_standby_slot()

        self.assertFalse(result)
        connect.assert_not_called()

    def test_consecutive_failures_pause_slot_and_cool_failed_node(self):
        self._activate_a()

        with (
            mock.patch.object(manager, "standby_nodes", return_value=[{"id": "node-b"}]),
            mock.patch.object(
                manager, "connect_node", side_effect=RuntimeError("握手失败")
            ) as connect,
            mock.patch.object(manager, "_record_node_failure") as record,
        ):
            for _ in range(manager.STANDBY_SLOT_MAX_FAILURES):
                self.assertFalse(manager.maintain_standby_slot())
            self.assertEqual(manager.STANDBY_SLOT_MAX_FAILURES, connect.call_count)
            self.assertEqual(manager.STANDBY_SLOT_MAX_FAILURES, record.call_count)
            record.assert_called_with("node-b")
            # 暂停期内不再尝试
            self.assertFalse(manager.maintain_standby_slot())
            self.assertEqual(manager.STANDBY_SLOT_MAX_FAILURES, connect.call_count)
            self.assertGreater(
                manager.standby_slot_paused_until["B"],
                time.time() + manager.STANDBY_SLOT_PAUSE_SECONDS - 5,
            )

    # ---- 无活动指针（fail-closed 中）→ 不建，避免无指针空转 ----

    def test_no_active_pointer_skips(self):
        manager.SLOT_RUNTIME["A"]["process"] = FakeProcess()
        manager.SLOT_RUNTIME["A"]["node_id"] = "node-a"

        with (
            mock.patch.object(manager, "standby_nodes", return_value=[{"id": "node-b"}]),
            mock.patch.object(manager, "connect_node") as connect,
        ):
            result = manager.maintain_standby_slot()

        self.assertFalse(result)
        connect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
