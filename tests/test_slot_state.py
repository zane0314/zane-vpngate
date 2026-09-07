"""Task 1 失败测试：slot_state 模块（槽位定义 + 原子活动槽指针）。"""

import json
import os
import tempfile
import unittest

import slot_state


class SlotConstantsTest(unittest.TestCase):
    def test_slot_devices_exactly_two_slots(self):
        self.assertEqual(slot_state.SLOT_DEVICES, {"A": "tun0", "B": "tun2"})

    def test_slot_tables_exactly_two_slots(self):
        self.assertEqual(slot_state.SLOT_TABLES, {"A": 100, "B": 101})

    def test_devices_and_tables_are_unique(self):
        self.assertEqual(len(set(slot_state.SLOT_DEVICES.values())),
                         len(slot_state.SLOT_DEVICES))
        self.assertEqual(len(set(slot_state.SLOT_TABLES.values())),
                         len(slot_state.SLOT_TABLES))

    def test_slot_keys_match_between_devices_and_tables(self):
        self.assertEqual(set(slot_state.SLOT_DEVICES), set(slot_state.SLOT_TABLES))


class DeviceSlotMappingTest(unittest.TestCase):
    def test_device_for_slot(self):
        self.assertEqual(slot_state.device_for_slot("A"), "tun0")
        self.assertEqual(slot_state.device_for_slot("B"), "tun2")

    def test_slot_for_device(self):
        self.assertEqual(slot_state.slot_for_device("tun0"), "A")
        self.assertEqual(slot_state.slot_for_device("tun2"), "B")

    def test_slot_for_device_unknown_returns_none(self):
        self.assertIsNone(slot_state.slot_for_device("tun1"))
        self.assertIsNone(slot_state.slot_for_device("eth0"))


class ActiveSlotTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.data_dir = self._tmp.name

    def _listdir(self):
        return os.listdir(self.data_dir)

    def test_read_missing_file_returns_none(self):
        self.assertIsNone(slot_state.read_active_slot(self.data_dir))

    def test_write_read_roundtrip(self):
        slot_state.write_active_slot(self.data_dir, "A", "node-123")
        result = slot_state.read_active_slot(self.data_dir)
        self.assertIsNotNone(result)
        self.assertEqual(result["slot"], "A")
        self.assertEqual(result["node_id"], "node-123")
        self.assertEqual(result["device"], "tun0")
        self.assertIsInstance(result["updated_at"], float)

    def test_write_slot_b_roundtrip(self):
        slot_state.write_active_slot(self.data_dir, "B", "node-456")
        result = slot_state.read_active_slot(self.data_dir)
        self.assertEqual(result["slot"], "B")
        self.assertEqual(result["device"], "tun2")
        self.assertEqual(result["node_id"], "node-456")

    def test_corrupt_json_returns_none(self):
        with open(os.path.join(self.data_dir, "active_slot.json"), "w") as fh:
            fh.write("{not valid json")
        self.assertIsNone(slot_state.read_active_slot(self.data_dir))

    def test_invalid_slot_value_returns_none(self):
        with open(os.path.join(self.data_dir, "active_slot.json"), "w") as fh:
            json.dump({"slot": "C", "node_id": "x", "device": "tun2",
                       "updated_at": 1.0}, fh)
        self.assertIsNone(slot_state.read_active_slot(self.data_dir))

    def test_device_slot_mismatch_returns_none(self):
        # slot=A 但 device 写成 tun2，与 SLOT_DEVICES["A"]=tun0 不一致
        with open(os.path.join(self.data_dir, "active_slot.json"), "w") as fh:
            json.dump({"slot": "A", "node_id": "x", "device": "tun2",
                       "updated_at": 1.0}, fh)
        self.assertIsNone(slot_state.read_active_slot(self.data_dir))

    def test_null_slot_returns_none(self):
        with open(os.path.join(self.data_dir, "active_slot.json"), "w") as fh:
            json.dump({"slot": None}, fh)
        self.assertIsNone(slot_state.read_active_slot(self.data_dir))

    def test_write_invalid_slot_raises_valueerror(self):
        with self.assertRaises(ValueError):
            slot_state.write_active_slot(self.data_dir, "C", "node-x")
        # 抛错后不应留下任何文件
        self.assertEqual(self._listdir(), [])

    def test_clear_writes_null_slot_and_read_returns_none(self):
        slot_state.write_active_slot(self.data_dir, "A", "node-1")
        slot_state.clear_active_slot(self.data_dir)
        self.assertIsNone(slot_state.read_active_slot(self.data_dir))
        # clear 是写入 {"slot": null} 而非删文件
        self.assertIn("active_slot.json", self._listdir())
        with open(os.path.join(self.data_dir, "active_slot.json")) as fh:
            self.assertEqual(json.load(fh).get("slot"), None)

    def test_no_tmp_residue_after_write(self):
        slot_state.write_active_slot(self.data_dir, "A", "node-1")
        leftovers = [name for name in self._listdir() if name != "active_slot.json"]
        self.assertEqual(leftovers, [])

    def test_overwrite_leaves_single_file(self):
        slot_state.write_active_slot(self.data_dir, "A", "node-1")
        slot_state.write_active_slot(self.data_dir, "B", "node-2")
        self.assertEqual(self._listdir(), ["active_slot.json"])
        result = slot_state.read_active_slot(self.data_dir)
        self.assertEqual(result["slot"], "B")
        self.assertEqual(result["node_id"], "node-2")


class SlotStatesTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.data_dir = self._tmp.name

    def test_read_missing_file_returns_empty_dict(self):
        self.assertEqual(slot_state.read_slot_states(self.data_dir), {})

    def test_write_read_roundtrip(self):
        states = {
            "A": {"node_id": "node-1", "device": "tun0",
                  "exit_ip": "1.2.3.4", "asn": 12345,
                  "verified_at": 1700000000.0, "health": "ok"},
            "B": {"node_id": None, "device": "tun2", "exit_ip": None,
                  "asn": None, "verified_at": None, "health": "empty"},
        }
        slot_state.write_slot_states(self.data_dir, states)
        self.assertEqual(slot_state.read_slot_states(self.data_dir), states)

    def test_corrupt_json_returns_empty_dict(self):
        with open(os.path.join(self.data_dir, "slot_states.json"), "w") as fh:
            fh.write("not json at all")
        self.assertEqual(slot_state.read_slot_states(self.data_dir), {})

    def test_no_tmp_residue_after_write(self):
        slot_state.write_slot_states(self.data_dir, {"A": {"node_id": "n"}})
        leftovers = [name for name in os.listdir(self.data_dir)
                     if name != "slot_states.json"]
        self.assertEqual(leftovers, [])

    def test_failed_write_preserves_old_file(self):
        good = {"A": {"node_id": "good", "device": "tun0"}}
        slot_state.write_slot_states(self.data_dir, good)

        class BadObject:
            pass

        with self.assertRaises(TypeError):
            slot_state.write_slot_states(self.data_dir, {"A": BadObject()})
        # 旧文件内容不被破坏，且无 tmp 残留
        self.assertEqual(slot_state.read_slot_states(self.data_dir), good)
        self.assertEqual(os.listdir(self.data_dir), ["slot_states.json"])


if __name__ == "__main__":
    unittest.main()
