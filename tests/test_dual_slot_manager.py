import io
import json
import os
import tempfile
import unittest
from unittest import mock

_DATA_TEMP = tempfile.TemporaryDirectory()
os.environ.setdefault("VPNGATE_DATA_DIR", _DATA_TEMP.name)

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


def sample_node(node_id="node-b"):
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
        "config_hash": "hash-b",
        "upstream_pool_state": "trusted",
        "asn": "AS12345",
    }


class DualSlotManagerTests(unittest.TestCase):
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
        for name in ("active_slot.json", "slot_states.json"):
            try:
                os.unlink(manager.DATA_DIR / name)
            except OSError:
                pass

    def tearDown(self):
        for slot, state in self._runtime_backup.items():
            manager.SLOT_RUNTIME[slot].update(state)
        manager.POOL_SOURCE_MODE = self.old_mode
        manager._sync_legacy_active_state()

    def test_slot_runtime_structure(self):
        self.assertEqual({"A", "B"}, set(manager.SLOT_RUNTIME))
        for state in manager.SLOT_RUNTIME.values():
            self.assertIn("process", state)
            self.assertIn("node_id", state)
            self.assertIn("draining_since", state)

    def test_setup_policy_routing_uses_given_table(self):
        with mock.patch.object(manager.subprocess, "run") as run:
            manager.setup_policy_routing("tun1", 101)
        commands = [call.args[0] for call in run.call_args_list]
        self.assertIn(["ip", "route", "add", "default", "dev", "tun1", "table", "101"], commands)
        self.assertIn(["ip", "rule", "add", "oif", "tun1", "table", "101"], commands)
        for command in commands:
            self.assertNotIn("100", [str(part) for part in command])

    def test_cleanup_policy_routing_uses_given_table(self):
        with mock.patch.object(manager.subprocess, "run") as run:
            manager.cleanup_policy_routing("tun1", 101)
        commands = [call.args[0] for call in run.call_args_list]
        self.assertIn(["ip", "rule", "del", "table", "101"], commands)
        self.assertIn(["ip", "route", "flush", "table", "101"], commands)

    def _connect_patches(self, node, egress):
        version_result = mock.Mock(stdout="OpenVPN 2.6.1", stderr="", returncode=0)
        return [
            mock.patch.object(manager, "read_nodes", return_value=[node]),
            mock.patch.object(manager, "write_json"),
            mock.patch.object(manager, "load_ui_config", return_value={"routing_mode": "auto", "routing_ip_type": "all"}),
            mock.patch.object(manager, "validate_node_allowed_by_routing"),
            mock.patch.object(manager.upstream_pool, "load_probe_state", return_value={}),
            mock.patch.object(manager.upstream_pool, "probe_is_current", return_value=True),
            mock.patch.object(manager.upstream_pool, "record_probe_result", return_value={}),
            mock.patch.object(manager, "direct_public_ip", return_value="203.0.113.1"),
            mock.patch.object(manager.subprocess, "run", return_value=version_result),
            mock.patch.object(manager, "setup_policy_routing"),
            mock.patch.object(manager, "verify_slot_egress", return_value=egress),
            mock.patch.object(manager.vpn_utils, "ping_latency_ms", return_value=20),
            mock.patch.object(manager, "refresh_pool_state"),
            mock.patch.object(manager, "update_standby_pool_state"),
            mock.patch.object(manager, "enter_fail_closed"),
            mock.patch.object(
                manager.health_probes, "probe_vpngate_slot",
                return_value={"ok": True, "detail": "ok", "checked_at": 1.0},
            ),
        ]

    def _start_connect(self, node, egress, slot):
        patches = self._connect_patches(node, egress)
        spawned = []

        def fake_popen(command, **kwargs):
            process = ready_process()
            spawned.append((command, process))
            return process

        patches.append(mock.patch.object(manager.subprocess, "Popen", side_effect=fake_popen))
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        result = manager.connect_node(node["id"], slot=slot)
        return result, spawned, patches

    def test_connect_node_slot_b_full_flow(self):
        node = sample_node()
        egress = {"ok": True, "exit_ip": "198.51.100.7", "ttfb_ms": 33, "slot": "B", "device": "tun1"}
        result, spawned, patches = self._start_connect(node, egress, "B")

        self.assertEqual(f"Connected {node['id']}", result)
        self.assertEqual(1, len(spawned))
        command = spawned[0][0]
        dev_index = command.index("--dev")
        self.assertEqual("tun1", command[dev_index + 1])

        manager.setup_policy_routing.assert_called_once_with("tun1", 101)
        manager.verify_slot_egress.assert_called_once_with("B")

        self.assertEqual(spawned[0][1], manager.SLOT_RUNTIME["B"]["process"])
        self.assertEqual(node["id"], manager.SLOT_RUNTIME["B"]["node_id"])
        self.assertIsNone(manager.SLOT_RUNTIME["A"]["process"])

        # 无有效指针时 connect_node 验证通过后经 promote_slot_to_active 接管活动指针
        pointer = slot_state.read_active_slot(str(manager.DATA_DIR))
        self.assertIsNotNone(pointer)
        self.assertEqual("B", pointer["slot"])
        self.assertEqual(node["id"], pointer["node_id"])

        states = slot_state.read_slot_states(str(manager.DATA_DIR))
        self.assertIn("B", states)
        self.assertEqual(node["id"], states["B"]["node_id"])
        self.assertEqual("tun1", states["B"]["device"])
        self.assertEqual("198.51.100.7", states["B"]["exit_ip"])
        self.assertEqual("ok", states["B"]["health"])
        self.assertGreater(states["B"]["verified_at"], 0)

    def test_connect_node_verification_failure_keeps_fail_closed(self):
        node = sample_node("node-fail")
        egress = {"ok": False, "error": "出口与默认公网相同", "slot": "B", "device": "tun1"}
        with self.assertRaises(RuntimeError):
            self._start_connect(node, egress, "B")

        self.assertIsNone(slot_state.read_active_slot(str(manager.DATA_DIR)))
        states = slot_state.read_slot_states(str(manager.DATA_DIR))
        self.assertNotIn("B", states)
        manager.enter_fail_closed.assert_called_once()
        self.assertIn("出口与默认公网相同", manager.enter_fail_closed.call_args.args[0])

    def test_connect_node_rejects_same_exit_ip_as_other_slot(self):
        slot_state.write_slot_states(str(manager.DATA_DIR), {
            "A": {"node_id": "node-a", "device": "tun0", "exit_ip": "198.51.100.7",
                  "asn": "", "verified_at": 1.0, "health": "ok"},
        })
        node = sample_node("node-dup")
        version_result = mock.Mock(stdout="OpenVPN 2.6.1", stderr="", returncode=0)

        def fake_popen(command, **kwargs):
            return ready_process()

        with (
            mock.patch.object(manager, "read_nodes", return_value=[node]),
            mock.patch.object(manager, "write_json"),
            mock.patch.object(manager, "load_ui_config", return_value={"routing_mode": "auto", "routing_ip_type": "all"}),
            mock.patch.object(manager, "validate_node_allowed_by_routing"),
            mock.patch.object(manager.upstream_pool, "load_probe_state", return_value={}),
            mock.patch.object(manager.upstream_pool, "probe_is_current", return_value=True),
            mock.patch.object(manager, "direct_public_ip", return_value="203.0.113.1"),
            mock.patch.object(manager.subprocess, "run", return_value=version_result),
            mock.patch.object(manager.subprocess, "Popen", side_effect=fake_popen),
            mock.patch.object(manager, "setup_policy_routing"),
            mock.patch.object(manager.vpn_utils, "probe_tunnel_egress", return_value={"ok": True, "exit_ip": "198.51.100.7"}),
            mock.patch.object(manager.vpn_utils, "ping_latency_ms", return_value=20),
            mock.patch.object(manager, "refresh_pool_state"),
            mock.patch.object(manager, "update_standby_pool_state"),
            mock.patch.object(manager, "enter_fail_closed"),
        ):
            with self.assertRaisesRegex(RuntimeError, "相同"):
                manager.connect_node(node["id"], slot="B")
            self.assertTrue(manager.enter_fail_closed.called)

        states = slot_state.read_slot_states(str(manager.DATA_DIR))
        self.assertNotIn("B", states)
        self.assertIsNone(slot_state.read_active_slot(str(manager.DATA_DIR)))

    def test_verify_slot_egress_rejects_baseline_exit(self):
        with (
            mock.patch.object(manager.vpn_utils, "probe_tunnel_egress", return_value={"ok": True, "exit_ip": "203.0.113.1"}),
            mock.patch.object(manager, "direct_public_ip", return_value="203.0.113.1"),
        ):
            result = manager.verify_slot_egress("A")
        self.assertFalse(result["ok"])
        self.assertEqual("203.0.113.1", result["exit_ip"])

    def test_verify_slot_egress_probe_args_and_success(self):
        with (
            mock.patch.object(manager.vpn_utils, "probe_tunnel_egress", return_value={"ok": True, "exit_ip": "198.51.100.9"}) as probe,
            mock.patch.object(manager, "direct_public_ip", return_value="203.0.113.1"),
        ):
            result = manager.verify_slot_egress("B")
        self.assertTrue(result["ok"])
        self.assertEqual("198.51.100.9", result["exit_ip"])
        self.assertEqual("tun1", result["device"])
        self.assertEqual("tun1", probe.call_args.args[0])
        self.assertEqual(0, probe.call_args.kwargs.get("upload_bytes"))
        self.assertEqual(1, probe.call_args.kwargs.get("samples"))
        self.assertLessEqual(probe.call_args.kwargs.get("download_bytes"), 1_000_000)

    def test_stop_slot_openvpn_isolates_other_slot(self):
        process_a = ready_process()
        process_b = ready_process()
        manager.SLOT_RUNTIME["A"]["process"] = process_a
        manager.SLOT_RUNTIME["A"]["node_id"] = "node-a"
        manager.SLOT_RUNTIME["B"]["process"] = process_b
        manager.SLOT_RUNTIME["B"]["node_id"] = "node-b"
        with mock.patch.object(manager.subprocess, "run") as run:
            manager.stop_slot_openvpn("A")

        self.assertTrue(process_a.terminated or process_a.killed)
        self.assertIsNone(manager.SLOT_RUNTIME["A"]["process"])
        self.assertIsNone(manager.SLOT_RUNTIME["A"]["node_id"])
        self.assertIs(process_b, manager.SLOT_RUNTIME["B"]["process"])
        self.assertEqual("node-b", manager.SLOT_RUNTIME["B"]["node_id"])
        self.assertIsNone(process_b.poll())

        commands = [call.args[0] for call in run.call_args_list]
        self.assertIn(["ip", "rule", "del", "table", "100"], commands)
        self.assertNotIn(["ip", "rule", "del", "table", "101"], commands)

    def test_stop_active_openvpn_falls_back_to_running_slot(self):
        process_b = ready_process()
        manager.SLOT_RUNTIME["B"]["process"] = process_b
        manager.SLOT_RUNTIME["B"]["node_id"] = "node-b"
        with mock.patch.object(manager.subprocess, "run"):
            manager.stop_active_openvpn()
        self.assertTrue(process_b.terminated or process_b.killed)
        self.assertIsNone(manager.SLOT_RUNTIME["B"]["process"])
        self.assertEqual("", manager.active_openvpn_node_id)

    def test_stop_active_openvpn_prefers_pointer_slot(self):
        process_a = ready_process()
        process_b = ready_process()
        manager.SLOT_RUNTIME["A"]["process"] = process_a
        manager.SLOT_RUNTIME["A"]["node_id"] = "node-a"
        manager.SLOT_RUNTIME["B"]["process"] = process_b
        manager.SLOT_RUNTIME["B"]["node_id"] = "node-b"
        slot_state.write_active_slot(str(manager.DATA_DIR), "B", "node-b")
        with mock.patch.object(manager.subprocess, "run"):
            manager.stop_active_openvpn()
        self.assertTrue(process_b.terminated or process_b.killed)
        self.assertIs(process_a, manager.SLOT_RUNTIME["A"]["process"])
        self.assertIsNone(process_a.poll())


if __name__ == "__main__":
    unittest.main()
