"""前端看门狗失败测试：健康分支低频前端探针 + 纯观测告警（last_alert/frontend_health）。"""

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

import slot_state
import vpngate_manager as manager


def ok_frontend(name="vless"):
    return {"ok": True, "detail": f"ok: {name}", "checked_at": time.time()}


class FrontendWatchdogTests(unittest.TestCase):
    def setUp(self):
        manager.ensure_dirs()
        manager.FRONTEND_WATCHDOG_STATE.clear()
        for name in (
            "state.json",
            "frontend-probes.json",
            "active_slot.json",
            "slot_states.json",
            "local-probe-state.json",
        ):
            try:
                os.unlink(manager.DATA_DIR / name)
            except OSError:
                pass

    def tearDown(self):
        manager.FRONTEND_WATCHDOG_STATE.clear()
        for name in ("state.json", "frontend-probes.json", "active_slot.json"):
            try:
                os.unlink(manager.DATA_DIR / name)
            except OSError:
                pass

    def _frontend_config_file(self):
        path = Path(manager.DATA_DIR) / "frontend-probes.json"
        path.write_text(json.dumps({
            "vless": {"probe_proxy": "http://127.0.0.1:18081", "probe_url": "http://127.0.0.1:18978/"},
            "trojan": {"probe_proxy": "http://127.0.0.1:18082", "probe_url": "http://127.0.0.1:18978/"},
        }), encoding="utf-8")
        return str(path)

    # ---- 健康分支触发前端探针 ----

    def test_checker_healthy_branch_invokes_frontend_watchdog(self):
        source = inspect.getsource(manager.background_proxy_checker)
        ok_index = source.index('if res["ok"]:')
        else_index = source.index("else:", ok_index)
        self.assertIn("run_frontend_watchdog", source[ok_index:else_index])

    def test_watchdog_probes_each_configured_frontend_and_records_health(self):
        with (
            mock.patch.object(manager, "FRONTEND_PROBE_CONFIG", self._frontend_config_file()),
            mock.patch.object(manager, "FRONTEND_PROBE_MIN_INTERVAL_SECONDS", 0),
            mock.patch.object(
                manager.health_probes, "probe_frontend", return_value=ok_frontend()
            ) as probe,
        ):
            result = manager.run_frontend_watchdog()

        self.assertEqual(["trojan", "vless"], sorted(result["checked"]))
        self.assertEqual(2, probe.call_count)
        probed_names = sorted(call.args[0] for call in probe.call_args_list)
        self.assertEqual(["trojan", "vless"], probed_names)
        state = manager.get_state()
        self.assertTrue(state["frontend_health"]["vless"])
        self.assertTrue(state["frontend_health"]["trojan"])
        self.assertGreater(state["frontend_health"]["checked_at"], 0)
        self.assertIsNone(state.get("last_alert"))

    # ---- 节流 ----

    def test_watchdog_throttled_within_min_interval(self):
        with (
            mock.patch.object(manager, "FRONTEND_PROBE_CONFIG", self._frontend_config_file()),
            mock.patch.object(manager, "FRONTEND_PROBE_MIN_INTERVAL_SECONDS", 60),
            mock.patch.object(
                manager.health_probes, "probe_frontend", return_value=ok_frontend()
            ) as probe,
        ):
            first = manager.run_frontend_watchdog()
            second = manager.run_frontend_watchdog()

        self.assertEqual(2, probe.call_count)
        self.assertEqual(["trojan", "vless"], sorted(first["checked"]))
        self.assertEqual([], second["checked"])

    # ---- 连续失败产 last_alert，纯观测不动指针/失败历史 ----

    def test_consecutive_failures_alert_without_touching_pointer_or_history(self):
        slot_state.write_active_slot(str(manager.DATA_DIR), "A", "node-a")

        def fake_frontend(name, cfg):
            return {
                "ok": name != "vless",
                "detail": f"{name} checked",
                "checked_at": time.time(),
            }

        with (
            mock.patch.object(manager, "FRONTEND_PROBE_CONFIG", self._frontend_config_file()),
            mock.patch.object(manager, "FRONTEND_PROBE_MIN_INTERVAL_SECONDS", 0),
            mock.patch.object(manager, "FRONTEND_PROBE_FAIL_THRESHOLD", 2),
            mock.patch.object(manager.health_probes, "probe_frontend", side_effect=fake_frontend),
        ):
            manager.run_frontend_watchdog()
            self.assertIsNone(manager.get_state().get("last_alert"))
            manager.run_frontend_watchdog()

        state = manager.get_state()
        alert = state.get("last_alert")
        self.assertIsNotNone(alert)
        self.assertIn("vless 前端入口连续失败", alert["message"])
        self.assertIn("不切换", alert["message"])
        self.assertGreater(alert["ts"], 0)
        self.assertFalse(state["frontend_health"]["vless"])
        self.assertTrue(state["frontend_health"]["trojan"])
        self.assertFalse(state.get("rotation_paused", False))
        pointer = slot_state.read_active_slot(str(manager.DATA_DIR))
        self.assertEqual("A", pointer["slot"])
        self.assertEqual("node-a", pointer["node_id"])
        probe_state = manager.upstream_pool.load_probe_state(manager.LOCAL_PROBE_STATE_FILE)
        self.assertNotIn("node-a", probe_state)

    # ---- 恢复后告警清除 ----

    def test_recovery_after_consecutive_successes_clears_alert(self):
        failures = {"vless"}

        def fake_frontend(name, cfg):
            return {
                "ok": name not in failures,
                "detail": f"{name} checked",
                "checked_at": time.time(),
            }

        with (
            mock.patch.object(manager, "FRONTEND_PROBE_CONFIG", self._frontend_config_file()),
            mock.patch.object(manager, "FRONTEND_PROBE_MIN_INTERVAL_SECONDS", 0),
            mock.patch.object(manager, "FRONTEND_PROBE_FAIL_THRESHOLD", 2),
            mock.patch.object(manager.health_probes, "probe_frontend", side_effect=fake_frontend),
        ):
            manager.run_frontend_watchdog()
            manager.run_frontend_watchdog()
            self.assertIsNotNone(manager.get_state().get("last_alert"))
            failures.clear()
            manager.run_frontend_watchdog()
            self.assertIsNotNone(manager.get_state().get("last_alert"))
            manager.run_frontend_watchdog()

        state = manager.get_state()
        self.assertIsNone(state.get("last_alert"))
        self.assertTrue(state["frontend_health"]["vless"])

    # ---- 未配置探针零副作用 ----

    def test_unconfigured_watchdog_has_zero_side_effects(self):
        with (
            mock.patch.object(manager, "FRONTEND_PROBE_CONFIG", None),
            mock.patch.object(manager.health_probes, "probe_frontend") as probe,
        ):
            result = manager.run_frontend_watchdog()

        probe.assert_not_called()
        self.assertEqual([], result["checked"])
        self.assertEqual([], result["alerts"])
        self.assertFalse((manager.DATA_DIR / "state.json").exists())


if __name__ == "__main__":
    unittest.main()
