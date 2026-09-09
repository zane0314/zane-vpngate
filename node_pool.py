from __future__ import annotations

import hashlib
import os
import re
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any


TRUSTED_LIMIT = int(os.environ.get("TRUSTED_POOL_LIMIT", "10"))
OBSERVATION_LIMIT = int(os.environ.get("OBSERVATION_POOL_LIMIT", "10"))
TRUST_MIN_IP_SCORE = int(os.environ.get("TRUST_MIN_IP_SCORE", "90"))
TRUST_MIN_SUCCESSES = int(os.environ.get("TRUST_MIN_SUCCESSES", "1"))
HARD_GATE_TTL_SECONDS = int(os.environ.get("HARD_GATE_TTL_SECONDS", "108000"))
AUTO_FAVORITE_SCORE = int(os.environ.get("AUTO_FAVORITE_SCORE", "100"))


def config_hash(config_text: str) -> str:
    normalized = "\n".join(line.rstrip() for line in config_text.strip().splitlines()) + "\n"
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def initialize_node(node: dict[str, Any], now: float | None = None) -> dict[str, Any]:
    checked_at = time.time() if now is None else now
    node.setdefault("first_seen_at", checked_at)
    node.setdefault("last_seen_at", checked_at)
    node.setdefault("success_count", 0)
    node.setdefault("failure_count", 0)
    node.setdefault("consecutive_successes", 0)
    node.setdefault("consecutive_failures", 0)
    node.setdefault("failure_days", [])
    node.setdefault("pool_state", "candidate")
    node.setdefault("actual_exit_ip", "")
    node.setdefault("actual_country", "")
    node.setdefault("actual_ip_type", "")
    node.setdefault("risk_free", False)
    node.setdefault("hard_gate", "pending")
    node.setdefault("hard_gate_checked_at", 0.0)
    node.setdefault("hard_gate_expires_at", 0.0)
    node.setdefault("hard_gate_reason", "")
    node.setdefault("ip_quality_score", 0)
    node.setdefault("cleanip_score", None)
    node.setdefault("network_score", 0)
    node.setdefault("stability_score", 0)
    node.setdefault("composite_score", 0)
    text = str(node.get("config_text") or "")
    if text:
        node["config_hash"] = config_hash(text)
    return node


def _consecutive_failure_days(values: list[str], today: date | None = None) -> int:
    expected = today or date.today()
    parsed: set[date] = set()
    for value in values:
        try:
            parsed.add(date.fromisoformat(str(value)))
        except (TypeError, ValueError):
            continue
    count = 0
    while expected in parsed:
        count += 1
        expected -= timedelta(days=1)
    return count


def record_probe(node: dict[str, Any], success: bool, now: float | None = None) -> None:
    checked_at = time.time() if now is None else now
    initialize_node(node, checked_at)
    if success:
        node["success_count"] = int(node.get("success_count", 0) or 0) + 1
        node["consecutive_successes"] = int(node.get("consecutive_successes", 0) or 0) + 1
        node["consecutive_failures"] = 0
        node["last_success_at"] = checked_at
    else:
        node["failure_count"] = int(node.get("failure_count", 0) or 0) + 1
        node["consecutive_failures"] = int(node.get("consecutive_failures", 0) or 0) + 1
        node["consecutive_successes"] = 0
        node["last_failure_at"] = checked_at
        failure_days = [str(value) for value in node.get("failure_days", []) if value]
        today = date.fromtimestamp(checked_at).isoformat()
        if today not in failure_days:
            failure_days.append(today)
        node["failure_days"] = failure_days[-30:]
    refresh_hard_gate(node, checked_at)


def calculate_scores(node: dict[str, Any]) -> None:
    successes = int(node.get("success_count", 0) or 0)
    failures = int(node.get("failure_count", 0) or 0)
    total = successes + failures
    success_rate = successes / total if total else 0.0
    streak_bonus = min(20, int(node.get("consecutive_successes", 0) or 0) * 5)
    stability = min(100, round(success_rate * 80 + streak_bonus))
    node["stability_score"] = stability

    latency = float(node.get("tunnel_latency_ms", node.get("latency_ms", 0)) or 0)
    speed_mbps = float(node.get("download_mbps", 0) or 0)
    loss = float(node.get("packet_loss_pct", 0) or 0)
    latency_score = 0 if latency <= 0 else max(0, min(100, round(110 - latency / 4)))
    speed_score = max(0, min(100, round(speed_mbps * 2)))
    loss_score = max(0, min(100, round(100 - loss * 10)))
    network = round(latency_score * 0.35 + speed_score * 0.45 + loss_score * 0.20)
    node["network_score"] = network

    ip_score = max(0, min(100, int(node.get("ip_quality_score", 0) or 0)))
    node["composite_score"] = round(ip_score * 0.60 + stability * 0.25 + network * 0.15)


def is_hard_eligible(node: dict[str, Any]) -> bool:
    country = str(node.get("actual_country") or "").strip().upper()
    ip_type = str(node.get("actual_ip_type") or "").strip().lower()
    return (
        node.get("probe_status") == "available"
        and country in {"JP", "JAPAN", "日本"}
        and ip_type == "residential"
        and bool(node.get("risk_free"))
    )


def refresh_hard_gate(node: dict[str, Any], now: float | None = None) -> str:
    checked_at = time.time() if now is None else float(now)
    initialize_node(node, checked_at)
    probe_status = str(node.get("probe_status") or "")
    if probe_status in {"available", "unavailable"}:
        if probe_status == "available" and is_hard_eligible(node):
            node.update(
                hard_gate="passed",
                hard_gate_checked_at=checked_at,
                hard_gate_expires_at=checked_at + HARD_GATE_TTL_SECONDS,
                hard_gate_reason="",
            )
        else:
            node.update(
                hard_gate="failed",
                hard_gate_checked_at=checked_at,
                hard_gate_expires_at=0.0,
                hard_gate_reason="hard_gate_conditions_failed",
            )
    elif node.get("hard_gate") == "passed" and float(node.get("hard_gate_expires_at", 0) or 0) <= checked_at:
        node.update(hard_gate="expired", hard_gate_reason="hard_gate_expired")
    return str(node.get("hard_gate") or "pending")


def rank_key(node: dict[str, Any]) -> tuple[float, float, float, float, float, str]:
    complete_weighted_score = str(node.get("score_mode") or "") == "weighted_netcoffee_cleanip"
    return (
        -float(node.get("ip_quality_score", 0) or 0),
        0 if complete_weighted_score else 1,
        -float(node.get("composite_score", 0) or 0),
        -float(node.get("stability_score", 0) or 0),
        -float(node.get("network_score", 0) or 0),
        str(node.get("id") or ""),
    )


def reconcile_pools(nodes: list[dict[str, Any]], now: float | None = None) -> dict[str, list[str]]:
    checked_at = time.time() if now is None else now
    eligible: list[dict[str, Any]] = []
    for node in nodes:
        initialize_node(node, checked_at)
        calculate_scores(node)
        gate_state = refresh_hard_gate(node, checked_at)
        if _consecutive_failure_days(list(node.get("failure_days", []))) >= 7:
            node["pool_state"] = "dormant"
        elif (
            gate_state == "passed"
            and is_hard_eligible(node)
            and float(node.get("hard_gate_expires_at", 0) or 0) > checked_at
        ):
            eligible.append(node)
        elif gate_state in {"failed", "expired"} or node.get("probe_status") == "unavailable":
            node["pool_state"] = "cooldown"
        elif int(node.get("consecutive_failures", 0) or 0) >= 2:
            node["pool_state"] = "cooldown"
        elif node.get("pool_state") not in {"dormant", "cooldown"}:
            node["pool_state"] = "candidate"

    trusted_candidates = sorted(
        [
            node for node in eligible
            if int(node.get("ip_quality_score", 0) or 0) >= TRUST_MIN_IP_SCORE
            and int(node.get("consecutive_successes", 0) or 0) >= TRUST_MIN_SUCCESSES
        ],
        key=rank_key,
    )
    trusted = trusted_candidates[:TRUSTED_LIMIT]
    trusted_ids = {str(node.get("id")) for node in trusted}

    observation_candidates = sorted(
        [node for node in eligible if str(node.get("id")) not in trusted_ids],
        key=rank_key,
    )
    observation = observation_candidates[:OBSERVATION_LIMIT]
    observation_ids = {str(node.get("id")) for node in observation}

    for node in trusted:
        node["pool_state"] = "trusted"
    for node in observation:
        node["pool_state"] = "observation"
    for node in eligible:
        node_id = str(node.get("id"))
        if node_id not in trusted_ids and node_id not in observation_ids:
            node["pool_state"] = "candidate"

    return {
        "trusted": [str(node.get("id")) for node in trusted],
        "observation": [str(node.get("id")) for node in observation],
    }


def auto_favorite_ids(nodes: list[dict[str, Any]], opted_out: set[str]) -> list[str]:
    return [
        str(node.get("id"))
        for node in nodes
        if node.get("pool_state") == "trusted"
        and int(node.get("ip_quality_score", 0) or 0) >= AUTO_FAVORITE_SCORE
        and bool(node.get("risk_free"))
        and str(node.get("id")) not in opted_out
    ]


def should_persist_config(node: dict[str, Any], favorite_ids: set[str], active_id: str = "") -> bool:
    node_id = str(node.get("id") or "")
    return bool(
        node_id
        and (
            node_id == active_id
            or bool(node.get("active"))
            or node_id in favorite_ids
            or node.get("pool_state") in {"trusted", "observation", "cooldown", "dormant"}
        )
    )


def persist_config(node: dict[str, Any]) -> bool:
    text = str(node.get("config_text") or "")
    path_text = str(node.get("config_file") or "")
    if not text or not path_text:
        return False
    path = Path(path_text)
    path.parent.mkdir(exist_ok=True, parents=True)
    expected = config_hash(text)
    if path.exists():
        try:
            if config_hash(path.read_text(encoding="utf-8", errors="replace")) == expected:
                node["config_hash"] = expected
                return False
        except OSError:
            pass
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
    node["config_hash"] = expected
    return True


def _safe_filename(value: str) -> str:
    result = "".join(char if char.isalnum() or char in "._-" else "_" for char in value.strip())
    return result.strip("._") or "node"


_MANAGED_CONFIG_NAME = re.compile(
    r"^[A-Za-z]{2}_[A-Za-z0-9_.:_-]+_[0-9]{1,6}_(?:tcp|udp)$"
)


def managed_config_ids(config_dir: Path) -> set[str]:
    return {
        path.stem
        for path in config_dir.glob("*.ovpn")
        if _MANAGED_CONFIG_NAME.fullmatch(path.stem)
    }


def prune_managed_configs(
    config_dir: Path, managed_ids: set[str], retained_ids: set[str]
) -> list[Path]:
    removed: list[Path] = []
    for node_id in sorted(set(managed_ids) - set(retained_ids)):
        path = config_dir / f"{_safe_filename(node_id)}.ovpn"
        if path.is_file():
            path.unlink()
            removed.append(path)
    return removed
