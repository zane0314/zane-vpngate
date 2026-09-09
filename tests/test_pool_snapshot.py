import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import pool_snapshot


NOW = datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc).timestamp()


def config_hash(text: str) -> str:
    normalized = "\n".join(line.rstrip() for line in text.strip().splitlines()) + "\n"
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def node(node_id: str, pool_state: str = "candidate") -> dict:
    text = f"client\nremote {node_id}.example 443\n"
    return {
        "id": node_id,
        "config_text": text,
        "config_hash": config_hash(text),
        "pool_state": pool_state,
        "composite_score": 90,
        "probed_at": NOW - 60,
        "last_success_at": NOW - 60,
        "hard_gate": "passed",
        "hard_gate_checked_at": NOW - 60,
        "hard_gate_expires_at": NOW + 3600,
        "actual_country": "JP",
        "actual_ip_type": "residential",
        "risk_free": True,
        "last_seen_at": NOW - 60,
    }


def snapshot(sequence: int = 1, generated_at: float = NOW) -> dict:
    text = "client\nremote one.example 443\n"
    return {
        "schema_version": 2,
        "sequence": sequence,
        "generated_at": datetime.fromtimestamp(generated_at, timezone.utc).isoformat(),
        "source_instance": "tokyo",
        "nodes": {
            "one": {
                "config_text": text,
                "config_hash": config_hash(text),
                "upstream_pool_state": "trusted",
                "upstream_score": 90,
                "upstream_verified_at": NOW - 60,
                "upstream_expires_at": NOW + 3600,
                "hard_gate": "passed",
                "hard_gate_checked_at": NOW - 60,
                "hard_gate_expires_at": NOW + 3600,
                "actual_country": "JP",
                "actual_ip_type": "residential",
                "risk_free": True,
                "source_seen_at": NOW - 60,
            }
        },
        "pools": {
            "trusted_ids": ["one"],
            "observation_ids": [],
            "candidate_ids": [],
            "standby_ids": ["one"],
        },
    }


class PoolSnapshotTests(unittest.TestCase):
    def test_build_snapshot_deduplicates_nodes_and_builds_four_pools(self):
        nodes = [
            node("trusted", "trusted"),
            node("observation", "observation"),
            node("candidate", "candidate"),
            {"id": "missing-config", "pool_state": "candidate"},
        ]
        state = {
            "trusted_pool_ids": ["trusted"],
            "observation_pool_ids": ["observation"],
            "standby_node_ids": ["observation", "trusted"],
        }
        result = pool_snapshot.build_snapshot(
            nodes, state, sequence=7, generated_at=NOW, source_instance="tokyo"
        )
        self.assertEqual({"trusted", "observation", "candidate"}, set(result["nodes"]))
        self.assertEqual(["trusted"], result["pools"]["trusted_ids"])
        self.assertEqual(["observation"], result["pools"]["observation_ids"])
        self.assertEqual(["candidate"], result["pools"]["candidate_ids"])
        self.assertEqual(["observation", "trusted"], result["pools"]["standby_ids"])
        self.assertEqual(7, result["sequence"])
        self.assertTrue(all(raw["hard_gate"] == "passed" for raw in result["nodes"].values()))
        self.assertNotIn("download_mbps", result["nodes"]["trusted"])

    def test_validate_rejects_config_hash_mismatch(self):
        value = snapshot()
        value["nodes"]["one"]["config_hash"] = "0" * 64
        with self.assertRaisesRegex(pool_snapshot.SnapshotValidationError, "config_hash"):
            pool_snapshot.validate_snapshot(value, now=NOW, max_age_seconds=172800, max_nodes=300)

    def test_validate_rejects_bad_schema_references_duplicates_and_bounds(self):
        cases = []
        bad_schema = snapshot()
        bad_schema["schema_version"] = 1
        cases.append(bad_schema)
        missing_ref = snapshot()
        missing_ref["pools"]["trusted_ids"] = ["missing"]
        cases.append(missing_ref)
        duplicate = snapshot()
        duplicate["pools"]["trusted_ids"] = ["one", "one"]
        cases.append(duplicate)
        future = snapshot(generated_at=NOW + 601)
        cases.append(future)
        expired = snapshot(generated_at=NOW - 172801)
        cases.append(expired)
        too_many = snapshot()
        too_many["nodes"]["two"] = dict(too_many["nodes"]["one"])
        cases.append(too_many)
        for value in cases:
            with self.subTest(value=value):
                with self.assertRaises(pool_snapshot.SnapshotValidationError):
                    pool_snapshot.validate_snapshot(value, now=NOW, max_age_seconds=172800, max_nodes=1)

    def test_build_filters_failed_pending_expired_and_caps_without_padding(self):
        passed = [node(f"passed-{index}") for index in range(3)]
        pending = node("pending")
        pending["hard_gate"] = "pending"
        failed = node("failed")
        failed["hard_gate"] = "failed"
        expired = node("expired")
        expired["hard_gate_expires_at"] = NOW - 1
        passed[0]["download_mbps"] = 999
        result = pool_snapshot.build_snapshot(
            passed + [pending, failed, expired],
            {"trusted_pool_ids": ["passed-0"], "observation_pool_ids": []},
            sequence=1,
            generated_at=NOW,
            source_instance="tokyo",
            max_nodes=2,
        )
        self.assertEqual({"passed-0", "passed-1"}, set(result["nodes"]))
        self.assertEqual(["passed-0"], result["pools"]["trusted_ids"])
        self.assertEqual(["passed-1"], result["pools"]["candidate_ids"])
        self.assertNotIn("download_mbps", result["nodes"]["passed-0"])

    def test_validate_rejects_missing_or_expired_hard_gate(self):
        missing = snapshot()
        del missing["nodes"]["one"]["hard_gate"]
        expired = snapshot()
        expired["nodes"]["one"]["hard_gate_expires_at"] = NOW
        for value in (missing, expired):
            with self.assertRaises(pool_snapshot.SnapshotValidationError):
                pool_snapshot.validate_snapshot(value, now=NOW, max_age_seconds=172800, max_nodes=300)

    def test_apply_is_atomic_preserves_previous_and_rejects_old_sequence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "download.json"
            current = root / "upstream-snapshot.json"
            previous = root / "upstream-snapshot.previous.json"
            current.write_text(json.dumps(snapshot(sequence=2)), encoding="utf-8")
            source.write_text(json.dumps(snapshot(sequence=3)), encoding="utf-8")

            applied = pool_snapshot.apply_snapshot_file(
                source,
                current,
                previous,
                max_bytes=1024 * 1024,
                now=NOW,
                max_age_seconds=172800,
                max_nodes=300,
            )
            self.assertEqual(3, applied["sequence"])
            self.assertEqual(2, json.loads(previous.read_text(encoding="utf-8"))["sequence"])

            current_bytes = current.read_bytes()
            source.write_text(json.dumps(snapshot(sequence=2)), encoding="utf-8")
            with self.assertRaisesRegex(pool_snapshot.SnapshotValidationError, "sequence"):
                pool_snapshot.apply_snapshot_file(
                    source,
                    current,
                    previous,
                    max_bytes=1024 * 1024,
                    now=NOW,
                    max_age_seconds=172800,
                    max_nodes=300,
                )
            self.assertEqual(current_bytes, current.read_bytes())

    def test_apply_migrates_legacy_current_snapshot_to_schema_v2(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "download.json"
            current = root / "upstream-snapshot.json"
            previous = root / "upstream-snapshot.previous.json"
            legacy = {"schema_version": 1, "sequence": 99, "nodes": {}}
            current.write_text(json.dumps(legacy), encoding="utf-8")
            source.write_text(json.dumps(snapshot(sequence=1)), encoding="utf-8")

            applied = pool_snapshot.apply_snapshot_file(
                source,
                current,
                previous,
                max_bytes=1024 * 1024,
                now=NOW,
                max_age_seconds=172800,
                max_nodes=300,
            )
            self.assertEqual(2, applied["schema_version"])
            self.assertEqual(2, json.loads(current.read_text(encoding="utf-8"))["schema_version"])
            self.assertEqual(1, json.loads(previous.read_text(encoding="utf-8"))["schema_version"])

    def test_apply_replaces_expired_current_snapshot_when_incoming_is_newer(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "download.json"
            current = root / "upstream-snapshot.json"
            previous = root / "upstream-snapshot.previous.json"
            expired = snapshot(sequence=2, generated_at=NOW - 172801)
            current.write_text(json.dumps(expired), encoding="utf-8")
            source.write_text(json.dumps(snapshot(sequence=3)), encoding="utf-8")

            applied = pool_snapshot.apply_snapshot_file(
                source,
                current,
                previous,
                max_bytes=1024 * 1024,
                now=NOW,
                max_age_seconds=172800,
                max_nodes=300,
            )

            self.assertEqual(3, applied["sequence"])
            self.assertEqual(2, json.loads(previous.read_text(encoding="utf-8"))["sequence"])

    def test_load_rejects_oversized_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "large.json"
            path.write_bytes(b"x" * 11)
            with self.assertRaisesRegex(pool_snapshot.SnapshotValidationError, "size"):
                pool_snapshot.load_snapshot_file(
                    path, max_bytes=10, now=NOW, max_age_seconds=172800, max_nodes=300
                )


if __name__ == "__main__":
    unittest.main()
