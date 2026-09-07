import json
import subprocess
import tempfile
import unittest
from pathlib import Path

import pool_snapshot


ROOT = Path(__file__).resolve().parents[1]
EXPORT = ROOT / "scripts" / "export_pool_snapshot.py"
PULL = ROOT / "scripts" / "pull_pool_snapshot.py"
READ = ROOT / "scripts" / "read_pool_snapshot.sh"
NOW = 1_776_211_200.0


class PoolSyncScriptTests(unittest.TestCase):
    def test_export_builds_valid_snapshot_and_increments_sequence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            nodes = root / "nodes.json"
            state = root / "state.json"
            output = root / "snapshot.json"
            nodes.write_text(
                json.dumps(
                    [
                        {
                            "id": "one",
                            "config_text": "client\nremote one.example 443\n",
                            "pool_state": "trusted",
                            "composite_score": 95,
                            "hard_gate": "passed",
                            "hard_gate_checked_at": NOW - 60,
                            "hard_gate_expires_at": NOW + 3600,
                            "actual_country": "JP",
                            "actual_ip_type": "residential",
                            "risk_free": True,
                            "last_seen_at": NOW - 60,
                        }
                    ]
                ),
                encoding="utf-8",
            )
            state.write_text(
                json.dumps(
                    {
                        "trusted_pool_ids": ["one"],
                        "observation_pool_ids": [],
                        "standby_node_ids": ["one"],
                    }
                ),
                encoding="utf-8",
            )
            command = [
                "python3",
                str(EXPORT),
                "--nodes-file",
                str(nodes),
                "--state-file",
                str(state),
                "--output",
                str(output),
                "--source-instance",
                "tokyo",
                "--now",
                str(NOW),
            ]
            subprocess.run(command, check=True, capture_output=True, text=True)
            subprocess.run(command, check=True, capture_output=True, text=True)
            value = pool_snapshot.load_snapshot_file(
                output,
                max_bytes=1024 * 1024,
                now=NOW,
                max_age_seconds=172800,
                max_nodes=300,
            )
            self.assertEqual(2, value["sequence"])
            self.assertEqual(["one"], value["pools"]["standby_ids"])

    def test_pull_applies_local_fixture_and_rejects_old_sequence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.json"
            current = root / "current.json"
            previous = root / "previous.json"
            base = {
                "schema_version": 2,
                "generated_at": "2026-04-15T00:00:00+00:00",
                "source_instance": "tokyo",
                "nodes": {},
                "pools": {
                    "trusted_ids": [],
                    "observation_ids": [],
                    "candidate_ids": [],
                    "standby_ids": [],
                },
            }
            source.write_text(json.dumps({**base, "sequence": 2}), encoding="utf-8")
            command = [
                "python3",
                str(PULL),
                "--source-file",
                str(source),
                "--current",
                str(current),
                "--previous",
                str(previous),
                "--now",
                str(NOW),
            ]
            subprocess.run(command, check=True, capture_output=True, text=True)
            failed = subprocess.run(command, capture_output=True, text=True)
            self.assertNotEqual(0, failed.returncode)
            self.assertEqual(2, json.loads(current.read_text(encoding="utf-8"))["sequence"])

    def test_pull_uses_strict_noninteractive_ssh_options(self):
        text = PULL.read_text(encoding="utf-8")
        for required in (
            "BatchMode=yes",
            "StrictHostKeyChecking=yes",
            "PasswordAuthentication=no",
            "ClearAllForwardings=yes",
            "PermitLocalCommand=no",
            "UserKnownHostsFile=",
        ):
            self.assertIn(required, text)

    def test_forced_command_reads_only_fixed_snapshot(self):
        text = READ.read_text(encoding="utf-8")
        self.assertIn("SSH_ORIGINAL_COMMAND", text)
        self.assertIn("/var/lib/aimilivpn-export/pool-snapshot.json", text)
        self.assertNotIn('eval ', text)
        self.assertNotIn('bash -c', text)


if __name__ == "__main__":
    unittest.main()
