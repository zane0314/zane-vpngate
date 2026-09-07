from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any


LOCAL_PROBE_RETENTION_SECONDS = 172800


def _unique(values: Any) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    if not isinstance(values, list):
        return result
    for value in values:
        node_id = str(value or "")
        if node_id and node_id not in seen:
            result.append(node_id)
            seen.add(node_id)
    return result


def ordered_allowed_ids(snapshot: dict[str, Any]) -> list[str]:
    pools = snapshot.get("pools", {})
    result: list[str] = []
    seen: set[str] = set()
    for key in ("standby_ids", "trusted_ids", "observation_ids", "candidate_ids"):
        for node_id in _unique(pools.get(key, [])):
            if node_id not in seen:
                result.append(node_id)
                seen.add(node_id)
    return result


def candidate_ids(snapshot: dict[str, Any]) -> set[str]:
    return set(_unique(snapshot.get("pools", {}).get("candidate_ids", [])))


def _safe_filename(node_id: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_.-]+", "_", node_id.strip()).strip("._")
    return value or "node"


def _hard_gate_current(raw: dict[str, Any], now: float) -> bool:
    try:
        expires_at = float(raw.get("hard_gate_expires_at", 0) or 0)
    except (TypeError, ValueError):
        return False
    country = str(raw.get("actual_country") or "").strip().upper()
    ip_type = str(raw.get("actual_ip_type") or "").strip().lower()
    return (
        raw.get("hard_gate") == "passed"
        and expires_at > float(now)
        and country in {"JP", "JAPAN", "日本"}
        and ip_type == "residential"
        and raw.get("risk_free") is True
    )


def snapshot_nodes(
    snapshot: dict[str, Any],
    config_dir: Path,
    probe_state: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    pools = snapshot.get("pools", {})
    membership: dict[str, str] = {}
    for key, state in (
        ("trusted_ids", "trusted"),
        ("observation_ids", "observation"),
        ("candidate_ids", "candidate"),
    ):
        for node_id in _unique(pools.get(key, [])):
            membership[node_id] = state

    result: list[dict[str, Any]] = []
    local_state = probe_state or {}
    for node_id, raw in snapshot.get("nodes", {}).items():
        state = membership.get(node_id, str(raw.get("upstream_pool_state") or "candidate"))
        local = local_state.get(node_id, {})
        result.append(
            {
                "id": node_id,
                "config_text": str(raw.get("config_text") or ""),
                "config_hash": str(raw.get("config_hash") or ""),
                "config_file": str(config_dir / f"{_safe_filename(node_id)}.ovpn"),
                "pool_state": state,
                "upstream_pool_state": state,
                "upstream_score": int(raw.get("upstream_score", 0) or 0),
                "upstream_verified_at": float(raw.get("upstream_verified_at", 0) or 0),
                "upstream_expires_at": float(raw.get("upstream_expires_at", 0) or 0),
                "hard_gate": str(raw.get("hard_gate") or ""),
                "hard_gate_checked_at": float(raw.get("hard_gate_checked_at", 0) or 0),
                "hard_gate_expires_at": float(raw.get("hard_gate_expires_at", 0) or 0),
                "actual_country": str(raw.get("actual_country") or ""),
                "actual_ip_type": str(raw.get("actual_ip_type") or ""),
                "risk_free": raw.get("risk_free") is True,
                "source_seen_at": float(raw.get("source_seen_at", 0) or 0),
                "probe_status": "not_checked",
                "probe_message": "等待云途本地隧道复核",
                "active": False,
                "local_download_mbps": local.get("local_download_mbps"),
                "local_ttfb_ms": local.get("local_ttfb_ms", 0),
                "local_measured_at": float(local.get("local_measured_at", 0) or 0),
                "local_probe_bytes": int(local.get("local_probe_bytes", 0) or 0),
                "local_probe_path": str(local.get("local_probe_path") or ""),
                "last_probe_success": bool(local.get("last_probe_success")),
                "last_probe_error_class": str(local.get("last_error_class") or ""),
            }
        )
        item = result[-1]
        if local.get("config_hash") == item["config_hash"] and local.get("last_probe_success"):
            item["probe_status"] = "available"
            item["probe_message"] = "云途本地隧道测速已通过"
            item["probed_at"] = float(local.get("last_probe_at", 0) or 0)
        elif float(local.get("cooldown_until", 0) or 0) > time.time():
            item["probe_status"] = "unavailable"
            item["probe_message"] = f"云途本地复核失败，冷却至 {int(local['cooldown_until'])}"
    order = {node_id: index for index, node_id in enumerate(ordered_allowed_ids(snapshot))}
    return sorted(
        result,
        key=lambda node: (
            order.get(str(node.get("id") or ""), len(order) + 1),
            str(node.get("id") or ""),
        ),
    )


def load_probe_state(path: Path) -> dict[str, dict[str, Any]]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    if not isinstance(value, dict):
        return {}
    return {
        str(node_id): raw
        for node_id, raw in value.items()
        if node_id and isinstance(raw, dict)
    }


def record_probe_result(
    state: dict[str, dict[str, Any]],
    node_id: str,
    config_hash: str,
    success: bool,
    error_class: str,
    now: float,
    cooldown_seconds: int,
    local_download_mbps: float | None = None,
    local_ttfb_ms: int = 0,
    local_probe_bytes: int = 0,
    local_probe_path: str = "",
) -> dict[str, dict[str, Any]]:
    updated = {key: dict(value) for key, value in state.items()}
    previous = updated.get(node_id, {})
    failures = 0 if success else int(previous.get("failure_count", 0) or 0) + 1
    item = {
        "config_hash": config_hash,
        "last_probe_at": float(now),
        "last_probe_success": bool(success),
        "failure_count": failures,
        "cooldown_until": 0.0 if success else float(now) + int(cooldown_seconds),
        "last_error_class": "" if success else str(error_class or "unknown"),
    }
    if success and local_download_mbps is not None:
        item.update(
            local_download_mbps=float(local_download_mbps),
            local_ttfb_ms=int(local_ttfb_ms or 0),
            local_measured_at=float(now),
            local_probe_bytes=int(local_probe_bytes or 0),
            local_probe_path=str(local_probe_path or ""),
        )
    updated[node_id] = item
    return updated


def probe_is_current(
    state: dict[str, dict[str, Any]], node_id: str, config_hash: str
) -> bool:
    raw = state.get(node_id, {})
    return bool(
        raw.get("last_probe_success")
        and raw.get("config_hash") == config_hash
    )


def select_connectable_nodes(
    snapshot: dict[str, Any],
    probe_state: dict[str, dict[str, Any]],
    now: float,
) -> list[dict[str, Any]]:
    nodes = snapshot.get("nodes", {})
    result: list[dict[str, Any]] = []
    for node_id in ordered_allowed_ids(snapshot):
        raw = nodes.get(node_id)
        if not isinstance(raw, dict):
            continue
        if not _hard_gate_current(raw, now):
            continue
        local = probe_state.get(node_id, {})
        same_config = local.get("config_hash") == raw.get("config_hash")
        if same_config and float(local.get("cooldown_until", 0) or 0) > float(now):
            continue
        item = {"id": node_id, **raw}
        if same_config:
            for key in (
                "last_probe_success",
                "last_probe_at",
                "local_download_mbps",
                "local_ttfb_ms",
                "local_measured_at",
                "local_probe_bytes",
                "local_probe_path",
                "cooldown_until",
                "last_error_class",
            ):
                if key in local:
                    item[key] = local[key]
        result.append(item)
    return result


def _node_probe_state(
    node: dict[str, Any], probe_state: dict[str, dict[str, Any]] | None
) -> dict[str, Any]:
    state = dict((probe_state or {}).get(str(node.get("id") or ""), {}))
    if state.get("config_hash") == node.get("config_hash"):
        return {**node, **state}
    return node


def _pool_tier(node: dict[str, Any]) -> int:
    return {"trusted": 0, "observation": 1, "candidate": 2}.get(
        str(node.get("pool_state") or node.get("upstream_pool_state") or "candidate"), 2
    )


def probe_queue_ids(
    nodes: list[dict[str, Any]],
    limit: int = 10,
    now: float | None = None,
    probe_state: dict[str, dict[str, Any]] | None = None,
) -> list[str]:
    checked_at = time.time() if now is None else float(now)
    candidates: list[tuple[int, int, float, str]] = []
    for node in nodes:
        node_id = str(node.get("id") or "")
        if not node_id or not _hard_gate_current(node, checked_at):
            continue
        local = _node_probe_state(node, probe_state)
        if float(local.get("cooldown_until", 0) or 0) > checked_at:
            continue
        measured_at = float(local.get("local_measured_at", 0) or 0)
        has_measurement = bool(local.get("last_probe_success")) and measured_at > 0
        expired = has_measurement and checked_at - measured_at > LOCAL_PROBE_RETENTION_SECONDS
        if has_measurement and not expired:
            continue
        freshness = 0 if not has_measurement else 1
        candidates.append((_pool_tier(node), freshness, measured_at, node_id))
    candidates.sort(key=lambda item: (item[0], item[1], item[2], item[3]))
    return [item[3] for item in candidates[: max(0, int(limit))]]


def _local_speed(node: dict[str, Any], now: float) -> float:
    measured_at = float(node.get("local_measured_at", 0) or 0)
    if (
        not node.get("last_probe_success")
        or measured_at <= 0
        or now - measured_at > LOCAL_PROBE_RETENTION_SECONDS
    ):
        return 0.0
    try:
        return max(0.0, float(node.get("local_download_mbps", 0) or 0))
    except (TypeError, ValueError):
        return 0.0


def rank_local_nodes(
    nodes: list[dict[str, Any]],
    now: float | None = None,
    speed_gain_percent: int = 30,
) -> list[dict[str, Any]]:
    checked_at = time.time() if now is None else float(now)
    groups: dict[int, list[dict[str, Any]]] = {0: [], 1: [], 2: []}
    for node in nodes:
        if not _hard_gate_current(node, checked_at):
            continue
        if float(node.get("cooldown_until", 0) or 0) > checked_at:
            continue
        groups[_pool_tier(node)].append(node)

    def sort_group(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return sorted(
            items,
            key=lambda node: (
                -_local_speed(node, checked_at),
                -float(node.get("local_measured_at", 0) or 0),
                str(node.get("id") or ""),
            ),
        )

    ordered_groups = [sort_group(groups[tier]) for tier in (0, 1, 2)]
    ordered = [node for group in ordered_groups for node in group]
    primary = ordered_groups[0] or ordered_groups[1]
    candidate = ordered_groups[2]
    if primary and candidate:
        primary_speed = _local_speed(primary[0], checked_at)
        candidate_speed = _local_speed(candidate[0], checked_at)
        if primary_speed > 0 and candidate_speed > primary_speed * (1 + int(speed_gain_percent) / 100):
            ordered.remove(candidate[0])
            ordered.insert(0, candidate[0])
    return ordered


def prune_probe_state(
    state: dict[str, dict[str, Any]],
    nodes: list[dict[str, Any]],
    now: float,
    retention_seconds: int = LOCAL_PROBE_RETENTION_SECONDS,
) -> dict[str, dict[str, Any]]:
    current = {
        str(node.get("id")): str(node.get("config_hash") or "")
        for node in nodes
        if node.get("id")
    }
    cleaned: dict[str, dict[str, Any]] = {}
    for node_id, raw in state.items():
        if node_id not in current or not isinstance(raw, dict):
            continue
        if raw.get("config_hash") != current[node_id]:
            continue
        try:
            last_probe_at = float(raw.get("last_probe_at", 0) or 0)
        except (TypeError, ValueError):
            continue
        if last_probe_at <= 0 or float(now) - last_probe_at > int(retention_seconds):
            continue
        cleaned[node_id] = dict(raw)
    return cleaned
