"""双槽槽位定义与原子活动槽指针（云途双槽改造阶段一 Task 1）。"""

import json
import os
import tempfile
import time

# B 槽用 tun2：tun1 预留给 x-ui 内置 VPNGate 缓存隧道（该隧道常驻且不可配置设备名）
SLOT_DEVICES = {"A": "tun0", "B": "tun2"}
SLOT_TABLES = {"A": 100, "B": 101}

_ACTIVE_SLOT_FILENAME = "active_slot.json"
_SLOT_STATES_FILENAME = "slot_states.json"


def device_for_slot(slot):
    return SLOT_DEVICES[slot]


def slot_for_device(device):
    for slot, dev in SLOT_DEVICES.items():
        if dev == device:
            return slot
    return None


def _atomic_write_json(data_dir, filename, payload):
    fd, tmp_path = tempfile.mkstemp(dir=data_dir)
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(payload, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, os.path.join(data_dir, filename))
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def read_active_slot(data_dir):
    path = os.path.join(data_dir, _ACTIVE_SLOT_FILENAME)
    try:
        with open(path) as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    slot = data.get("slot")
    if slot is None:
        return None
    if slot not in SLOT_DEVICES:
        return None
    if data.get("device") != SLOT_DEVICES[slot]:
        return None
    return {
        "slot": slot,
        "node_id": data.get("node_id"),
        "device": data.get("device"),
        "updated_at": data.get("updated_at"),
    }


def write_active_slot(data_dir, slot, node_id):
    if slot not in SLOT_DEVICES:
        raise ValueError("invalid slot: %r" % (slot,))
    payload = {
        "slot": slot,
        "node_id": node_id,
        "device": SLOT_DEVICES[slot],
        "updated_at": time.time(),
    }
    _atomic_write_json(data_dir, _ACTIVE_SLOT_FILENAME, payload)


def clear_active_slot(data_dir):
    _atomic_write_json(data_dir, _ACTIVE_SLOT_FILENAME, {"slot": None})


def read_slot_states(data_dir):
    path = os.path.join(data_dir, _SLOT_STATES_FILENAME)
    try:
        with open(path) as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def write_slot_states(data_dir, states):
    _atomic_write_json(data_dir, _SLOT_STATES_FILENAME, states)
