"""首次启动/无指针场景的活动槽接管回归测试（Task 8 云途实测缺陷）。

缺陷：旧数据目录（无 active_slot.json / slot_states.json）首次启动时，
auto_switch_node 建隧道成功但不写活动指针，7928 永久 fail-closed。
修复：connect_node 成功（出口验证通过）后，无有效指针或指针槽不健康时
经 promote_slot_to_active 接管；指针指向另一健康槽时不抢占。
"""

import io
import os
import tempfile
import time
import unittest
from unittest import mock

_DATA_TEMP = tempfile.TemporaryDirectory()
os.environ.setdefault("VPNGATE_DATA_DIR", _DATA_TEMP.name)

import proxy_server
import slot_state
import vpngate_manager as manager


class FakeProcess:
    def __init__(self, lines=("",)):
        self.stdout = io.StringIO("".join(line + "\n" for line in lines))
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


def ready_process():
    return FakeProcess(["OpenVPN starting", "Initialization Sequence Completed"])


def ok_probe(slot="A"):
    return {"ok": True, "detail": f"ok: slot {slot}", "checked_at": time.time()}


def failed_probe(slot="A"):
    return {"ok": False, "detail": f"process-dead: slot {slot}", "checked_at": time.time()}


def sample_node(node_id="node-new"):
    config_path = manager.CONFIG_DIR / f"{node_id}.ovpn"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text("client\nremote example.com 443\n", encoding="utf-8")
    return {
        "id": node_id,
        "ip": "203.0.113.50",
        "remote_host": "203.0.113.50",
        "remote_port": 443,
        "ping": 12,
        "config_file": str(config_path),
        "config_text": "client\nremote example.com 443\n",
        "config_hash": f"hash-{node_id}",
        "upstream_pool_state": "trusted",
        "asn": "AS12345",
    }


class FirstBootAdoptionTests(unittest.TestCase):
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
        for name in ("active_slot.json", "slot_states.json", "state.json"):
            try:
                os.unlink(manager.DATA_DIR / name)
            except OSError:
                pass

    def tearDown(self):
        for slot, state in self._runtime_backup.items():
            manager.SLOT_RUNTIME[slot].update(state)
        manager.POOL_SOURCE_MODE = self.old_mode
        manager._sync_legacy_active_state()

    def _connect_patches(self, node, egress, probe_side_effect=None):
        version_result = mock.Mock(stdout="OpenVPN 2.6.1", stderr="", returncode=0)
        probe_kwargs = {}
        if probe_side_effect is not None:
            probe_kwargs["side_effect"] = probe_side_effect
        else:
            probe_kwargs["return_value"] = ok_probe()
        return [
            mock.patch.object(manager, "read_nodes", return_value=[node]),
            mock.patch.object(manager, "write_json"),
            mock.patch.object(manager, "load_ui_config", return_value={
                "routing_mode": "auto", "routing_ip_type": "all", "connection_enabled": True,
            }),
            mock.patch.object(manager, "validate_node_allowed_by_routing"),
            mock.patch.object(manager.upstream_pool, "load_probe_state", return_value={}),
            mock.patch.object(manager.upstream_pool, "probe_is_current", return_value=True),
            mock.patch.object(manager.upstream_pool, "record_probe_result", return_value={}),
            mock.patch.object(manager, "direct_public_ip", return_value="203.0.113.1"),
            mock.patch.object(manager.subprocess, "run", return_value=version_result),
            mock.patch.object(manager.subprocess, "Popen", side_effect=lambda cmd, **kw: ready_process()),
            mock.patch.object(manager, "setup_policy_routing"),
            mock.patch.object(manager, "verify_slot_egress", return_value=egress),
            mock.patch.object(manager.vpn_utils, "ping_latency_ms", return_value=20),
            mock.patch.object(manager, "refresh_pool_state"),
            mock.patch.object(manager, "update_standby_pool_state"),
            mock.patch.object(manager, "enter_fail_closed"),
            mock.patch.object(manager.health_probes, "probe_vpngate_slot", **probe_kwargs),
        ]

    def _run_connect(self, node, egress, slot, probe_side_effect=None, extra_patches=()):
        patches = self._connect_patches(node, egress, probe_side_effect) + list(extra_patches)
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        return manager.connect_node(node["id"], slot=slot)

    # ---- 用例 1：旧数据目录首次启动，auto_switch_node 成功后必须写活动指针 ----

    def test_first_boot_auto_switch_adopts_active_slot(self):
        node = sample_node("node-first")
        egress = {"ok": True, "exit_ip": "198.51.100.7", "ttfb_ms": 33, "slot": "A", "device": "tun0"}
        # 模拟旧数据目录：无任何槽位文件
        self.assertIsNone(slot_state.read_active_slot(str(manager.DATA_DIR)))
        self.assertEqual({}, slot_state.read_slot_states(str(manager.DATA_DIR)))

        extra = [mock.patch.object(manager, "standby_nodes", return_value=[node])]
        patches = self._connect_patches(node, egress) + extra
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        manager.auto_switch_node()

        pointer = slot_state.read_active_slot(str(manager.DATA_DIR))
        self.assertIsNotNone(pointer, "首次启动建隧道成功后必须写入活动槽指针")
        self.assertEqual("A", pointer["slot"])
        self.assertEqual("tun0", pointer["device"])
        self.assertEqual("node-first", pointer["node_id"])

        states = slot_state.read_slot_states(str(manager.DATA_DIR))
        self.assertEqual("node-first", states["A"]["node_id"])

        # 7928 语义：resolve_active_device 不再抛 3004，能解析出活动槽设备
        with mock.patch("os.path.isdir", return_value=True):
            device = proxy_server.resolve_active_device(str(manager.DATA_DIR))
        self.assertEqual("tun0", device)

    # ---- 用例 2：已有健康 active=A 时 connect_node(B) 成功不抢指针（standby）----

    def test_connect_other_slot_keeps_healthy_active_pointer(self):
        slot_state.write_slot_states(str(manager.DATA_DIR), {
            "A": {"node_id": "node-a", "device": "tun0", "exit_ip": "198.51.100.1",
                  "asn": "", "verified_at": 1.0, "health": "ok"},
        })
        slot_state.write_active_slot(str(manager.DATA_DIR), "A", "node-a")
        manager.SLOT_RUNTIME["A"]["process"] = ready_process()
        manager.SLOT_RUNTIME["A"]["node_id"] = "node-a"

        node = sample_node("node-b")
        egress = {"ok": True, "exit_ip": "198.51.100.2", "ttfb_ms": 40, "slot": "B", "device": "tun1"}
        result = self._run_connect(node, egress, "B")
        self.assertEqual(f"Connected {node['id']}", result)

        pointer = slot_state.read_active_slot(str(manager.DATA_DIR))
        self.assertEqual("A", pointer["slot"], "健康 active 指针不得被 standby 连接抢占")
        self.assertEqual("node-a", pointer["node_id"])
        states = slot_state.read_slot_states(str(manager.DATA_DIR))
        self.assertEqual("node-b", states["B"]["node_id"])
        self.assertEqual("ok", states["B"]["health"])

    # ---- 用例 3：指针指向的槽已不健康时，新连接槽接管指针 ----

    def test_connect_adopts_when_pointed_slot_unhealthy(self):
        slot_state.write_active_slot(str(manager.DATA_DIR), "A", "node-a")
        node = sample_node("node-b")
        egress = {"ok": True, "exit_ip": "198.51.100.2", "ttfb_ms": 40, "slot": "B", "device": "tun1"}
        # 第一次探针 = 指针槽 A 健康复核（失败）；第二次 = promote B 前置探针（成功）
        self._run_connect(node, egress, "B", probe_side_effect=[failed_probe("A"), ok_probe("B")])

        pointer = slot_state.read_active_slot(str(manager.DATA_DIR))
        self.assertEqual("B", pointer["slot"])
        self.assertEqual("node-b", pointer["node_id"])

    # ---- 用例 4：同槽换节点时刷新指针 node_id ----

    def test_connect_same_slot_refreshes_pointer_node_id(self):
        slot_state.write_active_slot(str(manager.DATA_DIR), "A", "node-old")
        node = sample_node("node-new")
        egress = {"ok": True, "exit_ip": "198.51.100.9", "ttfb_ms": 21, "slot": "A", "device": "tun0"}
        self._run_connect(node, egress, "A")

        pointer = slot_state.read_active_slot(str(manager.DATA_DIR))
        self.assertEqual("A", pointer["slot"])
        self.assertEqual("node-new", pointer["node_id"])

    # ---- 用例 5：failover 中 connect_node 已接管时不得双重 promote ----

    def test_failover_skips_promote_when_connect_already_adopted(self):
        manager.SLOT_RUNTIME["A"]["process"] = ready_process()
        manager.SLOT_RUNTIME["A"]["node_id"] = "node-a"
        slot_state.write_active_slot(str(manager.DATA_DIR), "A", "node-a")

        def fake_connect(node_id, manual=False, slot="A"):
            manager.SLOT_RUNTIME[slot]["process"] = ready_process()
            manager.SLOT_RUNTIME[slot]["node_id"] = node_id
            manager.SLOT_RUNTIME[slot]["draining_since"] = None
            # 模拟修复后的 connect_node 内部接管
            slot_state.write_active_slot(str(manager.DATA_DIR), slot, node_id)
            return f"Connected {node_id}"

        with (
            mock.patch.object(manager.health_probes, "probe_vpngate_slot") as probe,
            mock.patch.object(manager, "direct_public_ip", return_value="203.0.113.1"),
            mock.patch.object(manager, "standby_nodes", return_value=[{"id": "node-c"}]),
            mock.patch.object(manager, "connect_node", side_effect=fake_connect),
            mock.patch.object(manager, "schedule_standby_replenish"),
        ):
            result = manager.failover_to_standby()

        self.assertTrue(result)
        probe.assert_not_called(), "connect 已接管指针时 failover 不得再次 promote"
        pointer = slot_state.read_active_slot(str(manager.DATA_DIR))
        self.assertEqual("B", pointer["slot"])
        self.assertEqual("node-c", pointer["node_id"])


if __name__ == "__main__":
    unittest.main()
