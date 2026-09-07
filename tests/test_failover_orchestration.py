"""Task 5 失败测试：切换编排（promote/drain/refill + 冷却 + 启动恢复）。"""

import io
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
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


def ok_probe(slot="B"):
    return {"ok": True, "detail": f"ok: slot {slot}", "checked_at": time.time()}


def failed_probe(slot="B", detail="process-dead"):
    return {"ok": False, "detail": f"{detail}: slot {slot}", "checked_at": time.time()}


class FailoverOrchestrationTests(unittest.TestCase):
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
        for name in (
            "active_slot.json",
            "slot_states.json",
            "state.json",
            "local-probe-state.json",
            "nodes.json",
        ):
            try:
                os.unlink(manager.DATA_DIR / name)
            except OSError:
                pass

    def tearDown(self):
        for slot, state in self._runtime_backup.items():
            manager.SLOT_RUNTIME[slot].update(state)
        manager.POOL_SOURCE_MODE = self.old_mode
        manager._sync_legacy_active_state()
        for name in ("state.json", "frontend-probes.json"):
            try:
                os.unlink(manager.DATA_DIR / name)
            except OSError:
                pass

    def _write_nodes(self, *node_ids):
        manager.write_json(
            manager.NODES_FILE,
            [{"id": node_id, "config_hash": f"hash-{node_id}"} for node_id in node_ids],
        )

    def _occupy(self, slot, node_id):
        process = FakeProcess()
        manager.SLOT_RUNTIME[slot]["process"] = process
        manager.SLOT_RUNTIME[slot]["node_id"] = node_id
        return process

    # ---- 计划 Step 1 用例 1：待机复验失败 → 指针不变、active 不断，尝试池中下一候选 ----

    def test_standby_recheck_failure_keeps_pointer_until_pool_candidate_promoted(self):
        self._write_nodes("node-a", "node-b", "node-c")
        process_a = self._occupy("A", "node-a")
        self._occupy("B", "node-b")
        slot_state.write_active_slot(str(manager.DATA_DIR), "A", "node-a")

        def fake_connect(node_id, manual=False, slot="A"):
            manager.SLOT_RUNTIME[slot]["process"] = FakeProcess()
            manager.SLOT_RUNTIME[slot]["node_id"] = node_id
            manager.SLOT_RUNTIME[slot]["draining_since"] = None
            return f"Connected {node_id}"

        with (
            mock.patch.object(
                manager.health_probes, "probe_vpngate_slot",
                side_effect=[failed_probe("B"), ok_probe("B")],
            ) as probe,
            mock.patch.object(manager, "direct_public_ip", return_value="203.0.113.1"),
            mock.patch.object(manager, "standby_nodes", return_value=[{"id": "node-c"}]),
            mock.patch.object(manager, "connect_node", side_effect=fake_connect) as connect,
            mock.patch.object(manager, "schedule_standby_replenish"),
        ):
            result = manager.failover_to_standby()

        self.assertTrue(result)
        connect.assert_called_once_with("node-c", slot="B")
        self.assertEqual(2, probe.call_count)
        pointer = slot_state.read_active_slot(str(manager.DATA_DIR))
        self.assertEqual("B", pointer["slot"])
        self.assertEqual("node-c", pointer["node_id"])
        # 失败路径中旧指针保持到候选验证通过；原 active 隧道未停，仅标记 draining
        self.assertIs(process_a, manager.SLOT_RUNTIME["A"]["process"])
        self.assertFalse(process_a.terminated or process_a.killed)
        self.assertIsNotNone(manager.SLOT_RUNTIME["A"]["draining_since"])

    # ---- 计划 Step 1 用例 2：复验成功 → 原子切换、旧槽 draining、失败节点 6h 冷却 ----

    def test_failover_success_drains_old_slot_and_cools_failed_node(self):
        self._write_nodes("node-a", "node-b")
        process_a = self._occupy("A", "node-a")
        self._occupy("B", "node-b")
        slot_state.write_active_slot(str(manager.DATA_DIR), "A", "node-a")

        before = time.time()
        with (
            mock.patch.object(
                manager.health_probes, "probe_vpngate_slot", return_value=ok_probe("B")
            ),
            mock.patch.object(manager, "direct_public_ip", return_value="203.0.113.1"),
            mock.patch.object(manager, "standby_nodes", return_value=[]),
            mock.patch.object(manager, "schedule_standby_replenish") as replenish,
        ):
            result = manager.failover_to_standby()

        self.assertTrue(result)
        pointer = slot_state.read_active_slot(str(manager.DATA_DIR))
        self.assertEqual("B", pointer["slot"])
        self.assertEqual("node-b", pointer["node_id"])
        self.assertGreaterEqual(manager.SLOT_RUNTIME["A"]["draining_since"], before)
        self.assertIs(process_a, manager.SLOT_RUNTIME["A"]["process"])
        self.assertFalse(process_a.terminated or process_a.killed)

        probe_state = manager.upstream_pool.load_probe_state(manager.LOCAL_PROBE_STATE_FILE)
        entry = probe_state["node-a"]
        self.assertFalse(entry["last_probe_success"])
        self.assertEqual(1, entry["failure_count"])
        self.assertGreaterEqual(
            entry["cooldown_until"], before + manager.NODE_FAILURE_COOLDOWN_SECONDS - 5
        )

        state = manager.get_state()
        self.assertGreaterEqual(
            state["switch_cooldown_until"], before + manager.SWITCH_COOLDOWN_SECONDS - 5
        )
        replenish.assert_called_once()

    # ---- 计划 Step 1 用例 3：draining 槽超过 DRAIN_SECONDS 才允许复用 ----

    def test_reap_drained_slots_respects_drain_seconds(self):
        now = time.time()
        self._occupy("A", "node-a")
        manager.SLOT_RUNTIME["A"]["draining_since"] = now - 10
        slot_state.write_active_slot(str(manager.DATA_DIR), "B", "node-b")

        with mock.patch.object(manager, "stop_slot_openvpn") as stop:
            self.assertEqual([], manager.reap_drained_slots(now))
            stop.assert_not_called()
        self.assertIsNotNone(manager.SLOT_RUNTIME["A"]["draining_since"])

        manager.SLOT_RUNTIME["A"]["draining_since"] = now - manager.DRAIN_SECONDS - 1
        with mock.patch.object(manager, "stop_slot_openvpn") as stop:
            self.assertEqual(["A"], manager.reap_drained_slots(now))
            stop.assert_called_once_with("A")

    # ---- 计划 Step 1 用例 4：无可用候选 → clear_active_slot + fail_closed ----

    def test_failover_without_candidates_clears_pointer_and_enters_fail_closed(self):
        self._write_nodes("node-a")
        process_a = self._occupy("A", "node-a")
        slot_state.write_active_slot(str(manager.DATA_DIR), "A", "node-a")

        with (
            mock.patch.object(manager, "standby_nodes", return_value=[]),
            mock.patch.object(manager.subprocess, "run"),
            mock.patch.object(manager, "update_standby_pool_state"),
        ):
            result = manager.failover_to_standby()

        self.assertFalse(result)
        self.assertIsNone(slot_state.read_active_slot(str(manager.DATA_DIR)))
        state = manager.get_state()
        self.assertTrue(state["fail_closed"])
        self.assertTrue(process_a.terminated or process_a.killed)

    # ---- 计划 Step 1 用例 5：启动恢复不信任旧状态 ----

    def _write_saved_slots(self):
        slot_state.write_slot_states(str(manager.DATA_DIR), {
            "A": {"node_id": "node-a", "device": "tun0", "exit_ip": "198.51.100.1",
                  "asn": "", "verified_at": 1.0, "health": "ok"},
            "B": {"node_id": "node-b", "device": "tun1", "exit_ip": "198.51.100.2",
                  "asn": "", "verified_at": 1.0, "health": "ok"},
        })

    def _boot_patches(self, verifies):
        def fake_connect(node_id, manual=False, slot="A"):
            manager.SLOT_RUNTIME[slot]["process"] = FakeProcess()
            manager.SLOT_RUNTIME[slot]["node_id"] = node_id
            manager.SLOT_RUNTIME[slot]["draining_since"] = None
            return f"Connected {node_id}"

        return [
            mock.patch.object(manager, "connect_node", side_effect=fake_connect),
            mock.patch.object(manager, "verify_slot_egress", side_effect=lambda slot: verifies[slot]),
            mock.patch.object(manager, "stop_slot_openvpn"),
            mock.patch.object(
                manager.health_probes, "probe_vpngate_slot", return_value=ok_probe()
            ),
            mock.patch.object(manager, "direct_public_ip", return_value="203.0.113.1"),
        ]

    def test_boot_recovery_promotes_other_verified_slot_when_active_fails(self):
        self._write_saved_slots()
        slot_state.write_active_slot(str(manager.DATA_DIR), "A", "node-a")
        verifies = {
            "A": {"ok": False, "error": "出口验证失败"},
            "B": {"ok": True, "exit_ip": "198.51.100.2"},
        }
        patches = self._boot_patches(verifies)
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

        manager._recover_slots_on_boot()

        pointer = slot_state.read_active_slot(str(manager.DATA_DIR))
        self.assertEqual("B", pointer["slot"])
        self.assertEqual("node-b", pointer["node_id"])

    def test_boot_recovery_all_slots_failed_keeps_no_pointer(self):
        self._write_saved_slots()
        slot_state.write_active_slot(str(manager.DATA_DIR), "A", "node-a")
        verifies = {
            "A": {"ok": False, "error": "出口验证失败"},
            "B": {"ok": False, "error": "出口验证失败"},
        }
        patches = self._boot_patches(verifies)
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

        manager._recover_slots_on_boot()

        self.assertIsNone(slot_state.read_active_slot(str(manager.DATA_DIR)))

    # ---- 计划 Step 1 用例 6：切换冷却期内性能择优不动作 ----

    def test_switch_cooldown_blocks_performance_switch(self):
        manager.set_state(switch_cooldown_until=time.time() + 600)
        manager.active_openvpn_node_id = "node-a"
        with (
            mock.patch.object(manager, "active_openvpn_running", return_value=True),
            mock.patch.object(manager, "read_nodes") as read_nodes_mock,
        ):
            self.assertFalse(manager.maybe_switch_to_better_node())
        read_nodes_mock.assert_not_called()

    # ---- 计划 Step 1 用例 7：故障矩阵分发 ----

    def _frontend_config_file(self):
        path = Path(manager.DATA_DIR) / "frontend-probes.json"
        path.write_text(json.dumps({
            "vless": {"host": "127.0.0.1", "port": 1},
            "trojan": {"host": "127.0.0.1", "port": 2},
        }), encoding="utf-8")
        return str(path)

    def test_host_stack_pauses_rotation_without_counting_node_failure(self):
        self._write_nodes("node-a")
        self._occupy("A", "node-a")
        slot_state.write_active_slot(str(manager.DATA_DIR), "A", "node-a")

        with (
            mock.patch.object(manager, "FRONTEND_PROBE_CONFIG", self._frontend_config_file()),
            mock.patch.object(manager, "VPNGATE_PROBE_INTERVAL_SECONDS", 0),
            mock.patch.object(manager, "direct_public_ip", return_value="203.0.113.1"),
            mock.patch.object(
                manager.health_probes, "probe_vpngate_slot", return_value=failed_probe("A")
            ) as probe,
            mock.patch.object(
                manager.health_probes, "probe_frontend",
                return_value={"ok": False, "detail": "tcp-failed", "checked_at": time.time()},
            ),
            mock.patch.object(manager, "failover_to_standby") as failover,
        ):
            result = manager.run_failure_attribution()

        self.assertEqual("host-stack", result["attribution"])
        self.assertEqual("pause_rotation", result["action"])
        self.assertEqual(manager.VPNGATE_PROBE_FAIL_THRESHOLD, probe.call_count)
        failover.assert_not_called()
        state = manager.get_state()
        self.assertTrue(state["rotation_paused"])
        self.assertTrue(state["last_alert"]["message"])
        self.assertGreater(state["last_alert"]["ts"], 0)
        probe_state = manager.upstream_pool.load_probe_state(manager.LOCAL_PROBE_STATE_FILE)
        self.assertNotIn("node-a", probe_state)

    def test_vless_only_holds_state_and_records_alert(self):
        self._write_nodes("node-a")
        self._occupy("A", "node-a")
        slot_state.write_active_slot(str(manager.DATA_DIR), "A", "node-a")

        def fake_frontend(name, cfg):
            return {
                "ok": name != "vless",
                "detail": f"{name} checked",
                "checked_at": time.time(),
            }

        with (
            mock.patch.object(manager, "FRONTEND_PROBE_CONFIG", self._frontend_config_file()),
            mock.patch.object(manager, "VPNGATE_PROBE_INTERVAL_SECONDS", 0),
            mock.patch.object(manager, "direct_public_ip", return_value="203.0.113.1"),
            mock.patch.object(
                manager.health_probes, "probe_vpngate_slot", return_value=ok_probe("A")
            ),
            mock.patch.object(
                manager.health_probes, "probe_frontend", side_effect=fake_frontend
            ),
            mock.patch.object(manager, "failover_to_standby") as failover,
        ):
            result = manager.run_failure_attribution()

        self.assertEqual("vless-only", result["attribution"])
        self.assertEqual("hold", result["action"])
        failover.assert_not_called()
        state = manager.get_state()
        self.assertFalse(state.get("rotation_paused", False))
        self.assertTrue(state["last_alert"]["message"])
        pointer = slot_state.read_active_slot(str(manager.DATA_DIR))
        self.assertEqual("A", pointer["slot"])
        self.assertEqual("node-a", pointer["node_id"])
        probe_state = manager.upstream_pool.load_probe_state(manager.LOCAL_PROBE_STATE_FILE)
        self.assertNotIn("node-a", probe_state)

    # ---- promote_slot_to_active 前置探针与 process_alive 注入 ----

    def test_promote_requires_passing_probe_and_keeps_old_pointer(self):
        self._occupy("A", "node-a")
        self._occupy("B", "node-b")
        slot_state.write_active_slot(str(manager.DATA_DIR), "A", "node-a")

        with (
            mock.patch.object(
                manager.health_probes, "probe_vpngate_slot", return_value=failed_probe("B")
            ),
            mock.patch.object(manager, "direct_public_ip", return_value="203.0.113.1"),
        ):
            with self.assertRaises(RuntimeError):
                manager.promote_slot_to_active("B", "node-b")

        pointer = slot_state.read_active_slot(str(manager.DATA_DIR))
        self.assertEqual("A", pointer["slot"])
        self.assertEqual("node-a", pointer["node_id"])

    def test_promote_injects_process_alive_reading_slot_runtime(self):
        captured = {}

        def fake_probe(slot, data_dir, **kwargs):
            captured.update(kwargs)
            return ok_probe(slot)

        with (
            mock.patch.object(
                manager.health_probes, "probe_vpngate_slot", side_effect=fake_probe
            ),
            mock.patch.object(manager, "direct_public_ip", return_value="203.0.113.1"),
        ):
            manager.promote_slot_to_active("B", "node-b")

        process_alive = captured["process_alive"]
        self.assertFalse(process_alive("B", str(manager.DATA_DIR)))
        manager.SLOT_RUNTIME["B"]["process"] = FakeProcess()
        self.assertTrue(process_alive("B", str(manager.DATA_DIR)))
        manager.SLOT_RUNTIME["B"]["process"].terminate()
        self.assertFalse(process_alive("B", str(manager.DATA_DIR)))
        self.assertEqual("203.0.113.1", captured["baseline_ip"])
        pointer = slot_state.read_active_slot(str(manager.DATA_DIR))
        self.assertEqual("B", pointer["slot"])


if __name__ == "__main__":
    unittest.main()
