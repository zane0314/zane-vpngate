"""切换竞态与死进程残留回归测试（云途 Task 9 第二轮实测缺口 #2/#3）。

缺口 #2（切换竞态）：kill active 槽进程后，collector_loop 消费模式分支与
maintain_valid_nodes 的 `not active_openvpn_running() → auto_switch_node()`
旧路径抢在 background_proxy_checker 的 run_failure_attribution 之前触发，
把原节点重连回同一槽——standby 未 promote、无 draining、无切换记录、无 6h 冷却。
修复：进程死/隧道失效统一先走 handle_active_tunnel_loss()（内部
run_failure_attribution → 故障矩阵 → failover 优先 promote standby），
auto_switch_node 只作"无可用 standby 且无活动隧道"的兜底；
run_failure_attribution 由 attribution_lock 保证并发触发只执行一次归因。

缺口 #3（死进程残留）：外部 kill 的 standby 槽进程对象残留在 SLOT_RUNTIME，
maintain_standby_slot 的 idle 判定只认 process is None，被杀 standby 槽永不重建。
修复：idle/存活判定统一认 process.poll()（None 或 poll() 非 None 即空闲/死亡）。
"""

import os
import tempfile
import threading
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


def ok_probe(slot="B"):
    return {"ok": True, "detail": f"ok: slot {slot}", "checked_at": time.time()}


def failed_probe(slot="A", detail="process-dead"):
    return {"ok": False, "detail": f"{detail}: slot {slot}", "checked_at": time.time()}


class FailoverRaceTests(unittest.TestCase):
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
        self._old_is_connecting = manager.is_connecting
        manager.is_connecting = False
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
        manager.is_connecting = self._old_is_connecting
        manager.POOL_SOURCE_MODE = self.old_mode
        manager._sync_legacy_active_state()
        for name in ("state.json",):
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

    def _kill_active_a_with_standby_b(self):
        """云途 Step 2 注入场景：A 活跃（指针指 A）+ B 热备，kill -9 A 的进程。"""
        self._write_nodes("node-a", "node-b")
        process_a = self._occupy("A", "node-a")
        self._occupy("B", "node-b")
        slot_state.write_active_slot(str(manager.DATA_DIR), "A", "node-a")
        manager._sync_legacy_active_state()
        process_a.kill()
        return process_a

    def _probe_by_slot(self, slot, data_dir, **kwargs):
        if slot == "A":
            return failed_probe("A")
        return ok_probe(slot)

    # ---- 缺口 #2 用例 1：active 进程死亡 → 统一入口走归因+failover，不做同节点重连 ----

    def test_active_process_death_routes_through_attribution_failover(self):
        process_a = self._kill_active_a_with_standby_b()
        before = time.time()

        with (
            mock.patch.object(manager, "VPNGATE_PROBE_INTERVAL_SECONDS", 0),
            mock.patch.object(manager, "direct_public_ip", return_value="203.0.113.1"),
            mock.patch.object(
                manager.health_probes, "probe_vpngate_slot",
                side_effect=self._probe_by_slot,
            ),
            mock.patch.object(manager, "schedule_standby_replenish"),
            mock.patch.object(manager, "auto_switch_node") as auto_switch,
            mock.patch.object(manager, "connect_node") as connect,
        ):
            result = manager.handle_active_tunnel_loss()

        self.assertIsNotNone(result)
        self.assertEqual("failover", result["action"])
        self.assertTrue(result.get("failover_ok"))
        # standby B 被 promote，指针切到 B
        pointer = slot_state.read_active_slot(str(manager.DATA_DIR))
        self.assertEqual("B", pointer["slot"])
        self.assertEqual("node-b", pointer["node_id"])
        # 旧槽 A 标 draining，死进程对象留待 reap
        self.assertGreaterEqual(manager.SLOT_RUNTIME["A"]["draining_since"], before)
        self.assertIs(process_a, manager.SLOT_RUNTIME["A"]["process"])
        # 切换记录齐全
        state = manager.get_state()
        self.assertGreaterEqual(state["last_switch_at"], before)
        self.assertEqual("standby-recheck", state["last_failover_reason"])
        self.assertGreaterEqual(
            state["switch_cooldown_until"], before + manager.SWITCH_COOLDOWN_SECONDS - 5
        )
        # 被杀节点记 6h 失败冷却
        probe_state = manager.upstream_pool.load_probe_state(manager.LOCAL_PROBE_STATE_FILE)
        entry = probe_state["node-a"]
        self.assertFalse(entry["last_probe_success"])
        self.assertGreaterEqual(
            entry["cooldown_until"], before + manager.NODE_FAILURE_COOLDOWN_SECONDS - 5
        )
        # 全程不得走同节点重连兜底（standby 可用）
        auto_switch.assert_not_called()
        connect.assert_not_called()

    # ---- 缺口 #2 用例 2：collector_loop 消费模式分支改走统一入口 ----

    def test_collector_loop_consumer_branch_calls_tunnel_loss_handler(self):
        self._kill_active_a_with_standby_b()

        class LoopExit(Exception):
            pass

        with (
            mock.patch.object(manager, "refresh_upstream_pool_state"),
            mock.patch.object(manager, "handle_active_tunnel_loss") as handle,
            mock.patch.object(manager, "auto_switch_node") as auto_switch,
            mock.patch.object(manager.time, "sleep", side_effect=LoopExit()),
        ):
            with self.assertRaises(LoopExit):
                manager.collector_loop()

        handle.assert_called_once_with()
        auto_switch.assert_not_called()

    # ---- 缺口 #2 用例 3：无可用 standby 时 auto_switch_node 兜底重连 ----

    def test_fallback_auto_switch_only_when_no_standby_and_no_tunnel(self):
        self._write_nodes("node-a")
        process_a = self._occupy("A", "node-a")
        slot_state.write_active_slot(str(manager.DATA_DIR), "A", "node-a")
        manager._sync_legacy_active_state()
        process_a.kill()

        with (
            mock.patch.object(manager, "VPNGATE_PROBE_INTERVAL_SECONDS", 0),
            mock.patch.object(manager, "direct_public_ip", return_value="203.0.113.1"),
            mock.patch.object(
                manager.health_probes, "probe_vpngate_slot",
                side_effect=self._probe_by_slot,
            ),
            mock.patch.object(manager, "failover_to_standby", return_value=False),
            mock.patch.object(manager, "auto_switch_node") as auto_switch,
        ):
            result = manager.handle_active_tunnel_loss()

        self.assertEqual("failover", result["action"])
        auto_switch.assert_called_once_with()

    # ---- 缺口 #2 用例 4：重入保护——并发触发只执行一次归因 ----

    def test_concurrent_triggers_run_attribution_only_once(self):
        self._kill_active_a_with_standby_b()
        entered = threading.Event()
        release = threading.Event()

        def slow_failover():
            entered.set()
            self.assertTrue(release.wait(timeout=5))
            return True

        with (
            mock.patch.object(manager, "VPNGATE_PROBE_INTERVAL_SECONDS", 0),
            mock.patch.object(manager, "direct_public_ip", return_value="203.0.113.1"),
            mock.patch.object(
                manager.health_probes, "probe_vpngate_slot",
                side_effect=self._probe_by_slot,
            ),
            mock.patch.object(
                manager, "failover_to_standby", side_effect=slow_failover
            ) as failover,
            # failover 被 mock 不真实接管隧道，这里模拟切换成功后活动隧道已恢复
            mock.patch.object(manager, "active_openvpn_running", return_value=True),
            mock.patch.object(manager, "auto_switch_node") as auto_switch,
        ):
            results = []

            def trigger():
                results.append(manager.handle_active_tunnel_loss())

            first = threading.Thread(target=trigger)
            first.start()
            self.assertTrue(entered.wait(timeout=5))
            # 第一次归因仍在执行中时并发触发第二次
            results.append(manager.handle_active_tunnel_loss())
            release.set()
            first.join(timeout=5)

        self.assertEqual(1, failover.call_count)
        self.assertEqual(2, len(results))
        actions = sorted(r["action"] for r in results)
        self.assertEqual(["failover", "skip"], actions)
        auto_switch.assert_not_called()

    # ---- 缺口 #3 用例 5：被杀 standby 槽死进程残留 → 判空闲并补建新隧道 ----

    def test_dead_standby_process_residue_counts_as_idle_and_rebuilds(self):
        # A 活跃健康
        manager.SLOT_RUNTIME["A"]["process"] = FakeProcess()
        manager.SLOT_RUNTIME["A"]["node_id"] = "node-a"
        slot_state.write_slot_states(str(manager.DATA_DIR), {
            "A": {"node_id": "node-a", "device": "tun0", "exit_ip": "198.51.100.1",
                  "asn": "", "verified_at": time.time(), "health": "ok"},
        })
        slot_state.write_active_slot(str(manager.DATA_DIR), "A", "node-a")
        manager._sync_legacy_active_state()
        # B 槽被外部 kill：process 对象残留但 poll() 非 None，node_id 未清
        dead_b = self._occupy("B", "node-b")
        dead_b.kill()

        def fake_connect(node_id, manual=False, slot="A"):
            manager.SLOT_RUNTIME[slot]["process"] = FakeProcess()
            manager.SLOT_RUNTIME[slot]["node_id"] = node_id
            manager.SLOT_RUNTIME[slot]["draining_since"] = None
            return f"Connected {node_id}"

        with (
            mock.patch.object(manager, "standby_nodes", return_value=[{"id": "node-c"}]),
            mock.patch.object(manager, "connect_node", side_effect=fake_connect) as connect,
        ):
            result = manager.maintain_standby_slot()

        self.assertTrue(result, "被杀 standby 槽的死进程残留应判为空闲并补建新隧道")
        connect.assert_called_once_with("node-c", slot="B")
        self.assertEqual("node-c", manager.SLOT_RUNTIME["B"]["node_id"])

    # ---- 缺口 #3 用例 6：current_active_slot 回退判定不认死进程残留 ----

    def test_current_active_slot_fallback_ignores_dead_process_residue(self):
        # 无活动指针；A 槽只有死进程残留
        dead_a = self._occupy("A", "node-a")
        dead_a.kill()

        self.assertIsNone(manager.current_active_slot())
        # B 槽进程存活时回退仍生效
        self._occupy("B", "node-b")
        self.assertEqual("B", manager.current_active_slot())

    # ---- 缺口 #3 用例 7：failover 不再尝试 promote 死进程残留的 standby 槽 ----

    def test_failover_skips_dead_standby_residue(self):
        self._write_nodes("node-a", "node-b", "node-c")
        process_a = self._occupy("A", "node-a")
        dead_b = self._occupy("B", "node-b")
        dead_b.kill()
        slot_state.write_active_slot(str(manager.DATA_DIR), "A", "node-a")
        manager._sync_legacy_active_state()
        process_a.kill()

        def fake_connect(node_id, manual=False, slot="A"):
            manager.SLOT_RUNTIME[slot]["process"] = FakeProcess()
            manager.SLOT_RUNTIME[slot]["node_id"] = node_id
            manager.SLOT_RUNTIME[slot]["draining_since"] = None
            return f"Connected {node_id}"

        probed = []

        def probe_recording(slot, data_dir, **kwargs):
            process = manager.SLOT_RUNTIME.get(slot, {}).get("process")
            alive = process is not None and process.poll() is None
            probed.append((slot, alive))
            return failed_probe("A") if slot == "A" else ok_probe(slot)

        with (
            mock.patch.object(manager, "VPNGATE_PROBE_INTERVAL_SECONDS", 0),
            mock.patch.object(manager, "direct_public_ip", return_value="203.0.113.1"),
            mock.patch.object(
                manager.health_probes, "probe_vpngate_slot",
                side_effect=probe_recording,
            ),
            mock.patch.object(manager, "standby_nodes", return_value=[{"id": "node-c"}]),
            mock.patch.object(manager, "connect_node", side_effect=fake_connect) as connect,
            mock.patch.object(manager, "schedule_standby_replenish"),
        ):
            result = manager.handle_active_tunnel_loss()

        self.assertEqual("failover", result["action"])
        self.assertTrue(result.get("failover_ok"))
        # B 槽死进程残留直接判死跳过；对 B 的探针只允许发生在补建成功（进程存活）之后
        self.assertTrue(probed)
        self.assertTrue(all(alive for slot, alive in probed if slot == "B"))
        connect.assert_called_once_with("node-c", slot="B")
        pointer = slot_state.read_active_slot(str(manager.DATA_DIR))
        self.assertEqual("B", pointer["slot"])
        self.assertEqual("node-c", pointer["node_id"])


if __name__ == "__main__":
    unittest.main()
