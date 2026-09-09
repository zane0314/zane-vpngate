from __future__ import annotations

import json
import math
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from node_pool import config_hash


SCHEMA_VERSION = 2
POOL_KEYS = ("trusted_ids", "observation_ids", "candidate_ids", "standby_ids")
PRIMARY_POOL_KEYS = ("trusted_ids", "observation_ids", "candidate_ids")
FUTURE_SKEW_SECONDS = 600


class SnapshotValidationError(ValueError):
    pass


def _iso_timestamp(value: float | str | datetime) -> str:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, (int, float)):
        parsed = datetime.fromtimestamp(float(value), timezone.utc)
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError as exc:
            raise SnapshotValidationError("generated_at is invalid") from exc
    if parsed.tzinfo is None:
        raise SnapshotValidationError("generated_at must include timezone")
    return parsed.astimezone(timezone.utc).isoformat()


def _timestamp(value: Any) -> float:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise SnapshotValidationError("generated_at is invalid") from exc
    if parsed.tzinfo is None:
        raise SnapshotValidationError("generated_at must include timezone")
    return parsed.timestamp()


def _required_timestamp(raw: dict[str, Any], key: str) -> float:
    try:
        value = float(raw[key])
    except (KeyError, TypeError, ValueError) as exc:
        raise SnapshotValidationError(f"{key} is invalid") from exc
    if not math.isfinite(value) or value <= 0:
        raise SnapshotValidationError(f"{key} is invalid")
    return value


def _hard_gate_passed(raw: dict[str, Any], generated_at: float) -> bool:
    if raw.get("hard_gate") != "passed":
        return False
    try:
        checked_at = _required_timestamp(raw, "hard_gate_checked_at")
        expires_at = _required_timestamp(raw, "hard_gate_expires_at")
    except SnapshotValidationError:
        return False
    country = str(raw.get("actual_country") or "").strip().upper()
    ip_type = str(raw.get("actual_ip_type") or "").strip().lower()
    return (
        checked_at <= generated_at + FUTURE_SKEW_SECONDS
        and expires_at > generated_at
        and country in {"JP", "JAPAN", "日本"}
        and ip_type == "residential"
        and raw.get("risk_free") is True
    )


def _ordered_known_ids(values: Any, known: set[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    if not isinstance(values, list):
        return result
    for value in values:
        node_id = str(value or "")
        if node_id and node_id in known and node_id not in seen:
            result.append(node_id)
            seen.add(node_id)
    return result


def build_snapshot(
    nodes: list[dict[str, Any]],
    state: dict[str, Any],
    sequence: int,
    generated_at: float | str | datetime,
    source_instance: str,
    max_nodes: int = 300,
) -> dict[str, Any]:
    if int(sequence) < 1:
        raise SnapshotValidationError("sequence must be positive")
    source = str(source_instance or "").strip()
    if not source:
        raise SnapshotValidationError("source_instance is required")
    if int(max_nodes) < 1:
        raise SnapshotValidationError("max_nodes must be positive")
    generated_iso = _iso_timestamp(generated_at)
    generated_ts = _timestamp(generated_iso)

    exported: dict[str, dict[str, Any]] = {}
    for raw in nodes:
        node_id = str(raw.get("id") or "").strip()
        text = str(raw.get("config_text") or "")
        if (
            not node_id
            or not text
            or node_id in exported
            or not _hard_gate_passed(raw, generated_ts)
        ):
            continue
        digest = config_hash(text)
        checked_at = _required_timestamp(raw, "hard_gate_checked_at")
        expires_at = _required_timestamp(raw, "hard_gate_expires_at")
        source_seen_at = float(
            raw.get(
                "source_seen_at",
                raw.get("last_seen_at", raw.get("last_success_at", checked_at)),
            )
            or checked_at
        )
        exported[node_id] = {
            "config_text": text,
            "config_hash": digest,
            "upstream_pool_state": "candidate",
            "upstream_score": int(raw.get("composite_score", 0) or 0),
            "upstream_verified_at": float(
                raw.get("last_success_at", raw.get("probed_at", 0)) or 0
            ),
            "upstream_expires_at": expires_at,
            "hard_gate": "passed",
            "hard_gate_checked_at": checked_at,
            "hard_gate_expires_at": expires_at,
            "actual_country": str(raw.get("actual_country") or "").strip().upper(),
            "actual_ip_type": str(raw.get("actual_ip_type") or "").strip().lower(),
            "risk_free": True,
            "source_seen_at": source_seen_at,
        }

    known = set(exported)
    trusted = _ordered_known_ids(state.get("trusted_pool_ids", []), known)
    observation = _ordered_known_ids(state.get("observation_pool_ids", []), known)
    assigned = set(trusted) | set(observation)
    candidate = [
        node_id
        for node_id, raw in exported.items()
        if node_id not in assigned
    ]
    selected_ids = (trusted + observation + candidate)[: int(max_nodes)]
    selected = set(selected_ids)
    trusted = [node_id for node_id in trusted if node_id in selected]
    observation = [node_id for node_id in observation if node_id in selected]
    candidate = [node_id for node_id in candidate if node_id in selected]
    standby = [
        node_id
        for node_id in _ordered_known_ids(state.get("standby_node_ids", []), known)
        if node_id in selected
    ]
    state_by_id = {
        **{node_id: "trusted" for node_id in trusted},
        **{node_id: "observation" for node_id in observation},
        **{node_id: "candidate" for node_id in candidate},
    }
    for node_id, raw in list(exported.items()):
        if node_id in selected:
            raw["upstream_pool_state"] = state_by_id[node_id]
        else:
            del exported[node_id]

    return {
        "schema_version": SCHEMA_VERSION,
        "sequence": int(sequence),
        "generated_at": generated_iso,
        "source_instance": source,
        "nodes": exported,
        "pools": {
            "trusted_ids": trusted,
            "observation_ids": observation,
            "candidate_ids": candidate,
            "standby_ids": standby,
        },
    }


def validate_snapshot(
    snapshot: Any,
    *,
    now: float,
    max_age_seconds: int,
    max_nodes: int,
) -> dict[str, Any]:
    if not isinstance(snapshot, dict):
        raise SnapshotValidationError("snapshot must be an object")
    if snapshot.get("schema_version") != SCHEMA_VERSION:
        raise SnapshotValidationError("unsupported schema_version")
    sequence = snapshot.get("sequence")
    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1:
        raise SnapshotValidationError("sequence must be a positive integer")
    if not str(snapshot.get("source_instance") or "").strip():
        raise SnapshotValidationError("source_instance is required")

    generated = _timestamp(snapshot.get("generated_at"))
    if generated > float(now) + FUTURE_SKEW_SECONDS:
        raise SnapshotValidationError("generated_at is too far in the future")
    if float(now) - generated > int(max_age_seconds):
        raise SnapshotValidationError("snapshot is expired")

    nodes = snapshot.get("nodes")
    if not isinstance(nodes, dict):
        raise SnapshotValidationError("nodes must be an object")
    if len(nodes) > int(max_nodes):
        raise SnapshotValidationError("node count exceeds limit")
    for node_id, raw in nodes.items():
        if not isinstance(node_id, str) or not node_id or not isinstance(raw, dict):
            raise SnapshotValidationError("invalid node entry")
        text = raw.get("config_text")
        digest = raw.get("config_hash")
        if not isinstance(text, str) or not text:
            raise SnapshotValidationError(f"node {node_id} has no config_text")
        if digest != config_hash(text):
            raise SnapshotValidationError(f"node {node_id} config_hash mismatch")
        if raw.get("hard_gate") != "passed":
            raise SnapshotValidationError(f"node {node_id} hard gate is not passed")
        checked_at = _required_timestamp(raw, "hard_gate_checked_at")
        expires_at = _required_timestamp(raw, "hard_gate_expires_at")
        source_seen_at = _required_timestamp(raw, "source_seen_at")
        if checked_at > generated + FUTURE_SKEW_SECONDS:
            raise SnapshotValidationError(f"node {node_id} hard gate check is in the future")
        if expires_at <= generated:
            raise SnapshotValidationError(f"node {node_id} hard gate is expired")
        if source_seen_at > generated + FUTURE_SKEW_SECONDS:
            raise SnapshotValidationError(f"node {node_id} source timestamp is in the future")
        country = str(raw.get("actual_country") or "").strip().upper()
        ip_type = str(raw.get("actual_ip_type") or "").strip().lower()
        if country not in {"JP", "JAPAN", "日本"} or ip_type != "residential":
            raise SnapshotValidationError(f"node {node_id} is not Japanese residential")
        if raw.get("risk_free") is not True:
            raise SnapshotValidationError(f"node {node_id} is not risk-free")
        if any(key in raw for key in ("download_mbps", "upload_mbps", "local_download_mbps")):
            raise SnapshotValidationError(f"node {node_id} contains throughput data")

    pools = snapshot.get("pools")
    if not isinstance(pools, dict) or set(pools) != set(POOL_KEYS):
        raise SnapshotValidationError("pools must contain exactly four collections")
    known = set(nodes)
    primary_seen: set[str] = set()
    for key in POOL_KEYS:
        values = pools.get(key)
        if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
            raise SnapshotValidationError(f"{key} must be a string list")
        if len(values) != len(set(values)):
            raise SnapshotValidationError(f"{key} contains duplicate IDs")
        unknown = set(values) - known
        if unknown:
            raise SnapshotValidationError(f"{key} references unknown nodes")
        if key in PRIMARY_POOL_KEYS:
            overlap = primary_seen.intersection(values)
            if overlap:
                raise SnapshotValidationError("primary pool IDs must be disjoint")
            primary_seen.update(values)

    return snapshot


def load_snapshot_file(
    path: Path,
    *,
    max_bytes: int,
    now: float,
    max_age_seconds: int,
    max_nodes: int,
) -> dict[str, Any]:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise SnapshotValidationError(f"snapshot file unavailable: {exc}") from exc
    if size > int(max_bytes):
        raise SnapshotValidationError("snapshot file size exceeds limit")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SnapshotValidationError(f"snapshot JSON is invalid: {exc}") from exc
    return validate_snapshot(
        value,
        now=now,
        max_age_seconds=max_age_seconds,
        max_nodes=max_nodes,
    )


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def apply_snapshot_file(
    source: Path,
    current: Path,
    previous: Path,
    *,
    max_bytes: int,
    now: float,
    max_age_seconds: int,
    max_nodes: int,
) -> dict[str, Any]:
    incoming = load_snapshot_file(
        source,
        max_bytes=max_bytes,
        now=now,
        max_age_seconds=max_age_seconds,
        max_nodes=max_nodes,
    )
    current_bytes: bytes | None = None
    if current.exists():
        if current.stat().st_size > int(max_bytes):
            raise SnapshotValidationError("snapshot file size exceeds limit")
        current_bytes = current.read_bytes()
        try:
            current_snapshot = json.loads(current_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SnapshotValidationError("current snapshot is invalid") from exc
        if not isinstance(current_snapshot, dict):
            raise SnapshotValidationError("current snapshot is invalid")
        if current_snapshot.get("schema_version") == SCHEMA_VERSION:
            current_sequence = current_snapshot.get("sequence")
            if isinstance(current_sequence, bool) or not isinstance(current_sequence, int) or current_sequence < 1:
                raise SnapshotValidationError("current snapshot sequence is invalid")
            if incoming["sequence"] <= current_sequence:
                raise SnapshotValidationError("sequence must increase")
        elif current_snapshot.get("schema_version") != 1:
            raise SnapshotValidationError("current snapshot schema is invalid")

    incoming_bytes = json.dumps(
        incoming, ensure_ascii=False, indent=2, sort_keys=True
    ).encode("utf-8") + b"\n"
    if current_bytes is not None:
        _atomic_write(previous, current_bytes)
    _atomic_write(current, incoming_bytes)
    return incoming
