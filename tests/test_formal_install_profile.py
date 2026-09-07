import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class FormalInstallProfileTests(unittest.TestCase):
    def test_required_policy_values_are_pinned(self):
        values = {}
        for raw_line in (ROOT / "config" / "aimilivpn.env").read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if line and not line.startswith("#"):
                key, value = line.split("=", 1)
                values[key] = value

        self.assertEqual("true", values["STRICT_RESIDENTIAL_ONLY"])
        self.assertEqual("Asia/Shanghai", values["TZ"])
        self.assertEqual("10", values["TRUSTED_POOL_LIMIT"])
        self.assertEqual("10", values["OBSERVATION_POOL_LIMIT"])
        self.assertEqual("1", values["TRUST_MIN_SUCCESSES"])
        self.assertEqual("7200", values["HARD_GATE_TTL_SECONDS"])
        self.assertEqual("10", values["STANDBY_TARGET"])
        self.assertEqual("691200", values["STANDBY_MAX_AGE_SECONDS"])
        self.assertEqual("2", values["STABLE_POOL_PROBE_DAILY_LIMIT"])
        self.assertEqual("25000000", values["STABLE_POOL_PROBE_BYTES"])
        self.assertEqual("3000000", values["STABLE_POOL_PROBE_UPLOAD_BYTES"])
        self.assertEqual("2", values["STABLE_POOL_PROBE_SAMPLES"])
        self.assertEqual("10000000", values["FULL_PROBE_BYTES"])
        self.assertEqual("5", values["QUALITY_TIE_WINDOW"])
        self.assertEqual("30", values["SPEED_SWITCH_GAIN_PERCENT"])
        self.assertEqual("2", values["SWITCH_REQUIRED_WINS"])
        self.assertEqual("2", values["TEST_MAX_WORKERS"])
        self.assertEqual("3", values["DAILY_MAINTENANCE_START_HOUR"])
        self.assertEqual("4", values["DAILY_MAINTENANCE_END_HOUR"])
        self.assertEqual("127.0.0.1", values["LOCAL_PROXY_HOST"])
        self.assertEqual("local-authority", values["POOL_SOURCE_MODE"])

    def test_upstream_profile_is_lightweight_and_bounded(self):
        values = {}
        for raw_line in (ROOT / "config" / "aimilivpn-upstream.env").read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if line and not line.startswith("#"):
                key, value = line.split("=", 1)
                values[key] = value
        self.assertEqual("upstream-consumer", values["POOL_SOURCE_MODE"])
        self.assertEqual("1", values["TEST_MAX_WORKERS"])
        self.assertEqual("7200", values["UPSTREAM_SNAPSHOT_MAX_AGE_SECONDS"])
        self.assertEqual("8388608", values["UPSTREAM_SNAPSHOT_MAX_BYTES"])
        self.assertEqual("300", values["UPSTREAM_SNAPSHOT_MAX_NODES"])
        self.assertEqual("1", values["TRUST_MIN_SUCCESSES"])
        self.assertEqual("7200", values["HARD_GATE_TTL_SECONDS"])
        self.assertEqual("10", values["UPSTREAM_LOCAL_PROBE_BATCH_SIZE"])
        self.assertEqual("300", values["UPSTREAM_LOCAL_PROBE_INTERVAL_SECONDS"])
        self.assertEqual("25000000", values["UPSTREAM_LOCAL_PROBE_BYTES"])
        self.assertEqual("1", values["UPSTREAM_LOCAL_PROBE_SAMPLES"])
        self.assertEqual("172800", values["UPSTREAM_LOCAL_PROBE_RETENTION_SECONDS"])

    def test_upstream_profile_satisfies_common_formal_verifier_contract(self):
        values = {}
        for raw_line in (ROOT / "config" / "aimilivpn-upstream.env").read_text(
            encoding="utf-8"
        ).splitlines():
            line = raw_line.strip()
            if line and not line.startswith("#"):
                key, value = line.split("=", 1)
                values[key] = value

        expected = {
            "TRUSTED_POOL_LIMIT": "10",
            "OBSERVATION_POOL_LIMIT": "10",
            "STANDBY_MAX_AGE_SECONDS": "691200",
            "DAILY_MAINTENANCE_START_HOUR": "3",
            "DAILY_MAINTENANCE_END_HOUR": "4",
            "STABLE_POOL_PROBE_DAILY_LIMIT": "2",
            "STABLE_POOL_PROBE_BYTES": "25000000",
            "STABLE_POOL_PROBE_UPLOAD_BYTES": "3000000",
            "STABLE_POOL_PROBE_SAMPLES": "2",
            "FULL_PROBE_BYTES": "10000000",
            "QUALITY_TIE_WINDOW": "5",
            "SPEED_SWITCH_GAIN_PERCENT": "30",
            "SWITCH_REQUIRED_WINS": "2",
        }
        for key, value in expected.items():
            self.assertEqual(value, values[key], key)

    def test_systemd_timers_run_hourly(self):
        for name in ("aimilivpn-pool-export.timer", "aimilivpn-pool-sync.timer"):
            text = (ROOT / "systemd" / name).read_text(encoding="utf-8")
            self.assertIn("*-*-* *:00:00 Asia/Shanghai", text)
            self.assertNotIn("04,12,20:00:00 Asia/Shanghai", text)
            self.assertIn("Persistent=true", text)

    def test_systemd_services_are_hardened(self):
        for name in ("aimilivpn-pool-export.service", "aimilivpn-pool-sync.service"):
            text = (ROOT / "systemd" / name).read_text(encoding="utf-8")
            self.assertIn("NoNewPrivileges=true", text)
            self.assertIn("PrivateTmp=true", text)
            self.assertIn("ProtectSystem=strict", text)

    def test_exporter_grants_snapshot_read_only_to_sync_group(self):
        text = (ROOT / "systemd" / "aimilivpn-pool-export.service").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            "ExecStartPost=/bin/chown root:aimili-sync "
            "/var/lib/aimilivpn-export/pool-snapshot.json",
            text,
        )
        self.assertIn(
            "ExecStartPost=/bin/chmod 0640 "
            "/var/lib/aimilivpn-export/pool-snapshot.json",
            text,
        )

    def test_export_service_labels_tokyo_as_authority(self):
        text = (ROOT / "systemd" / "aimilivpn-pool-export.service").read_text(
            encoding="utf-8"
        )
        self.assertIn("--source-instance tokyo", text)

    def test_formal_verifier_matches_schema_v2_runtime_profile(self):
        text = (ROOT / "scripts" / "verify-formal-install.sh").read_text(encoding="utf-8")
        self.assertIn("check_env UPSTREAM_SNAPSHOT_MAX_AGE_SECONDS 7200", text)
        self.assertIn("check_env TRUST_MIN_SUCCESSES 1", text)
        self.assertIn("check_env HARD_GATE_TTL_SECONDS 7200", text)

    def test_installer_has_safe_local_and_git_sources(self):
        script = (ROOT / "install-zane.sh").read_text(encoding="utf-8")
        self.assertIn("github.com/OWNER/REPOSITORY.git", script)
        self.assertIn('AIMILI_BRANCH="${AIMILI_BRANCH:-main}"', script)
        self.assertIn("AIMILI_LOCAL_SOURCE_DIR", script)
        self.assertIn(".aimilivpn-managed", script)
        self.assertIn("/dev/net/tun", script)
        self.assertIn("verify-formal-install.sh", script)
        self.assertIn("AIMILI_PROFILE", script)
        self.assertIn("aimilivpn-upstream.env", script)
        self.assertIn("groupadd --system aimili-sync", script)
        self.assertIn("--shell /bin/bash aimili-sync", script)
        self.assertIn(
            "install -d -o root -g aimili-sync -m 0750 "
            "/var/lib/aimilivpn-export",
            script,
        )

    def test_local_package_entrypoint_is_present(self):
        entrypoint = ROOT / "install-local-package.sh"
        self.assertTrue(entrypoint.is_file())
        self.assertIn("AIMILI_LOCAL_SOURCE_DIR", entrypoint.read_text(encoding="utf-8"))

    def test_installer_exports_readonly_profile_without_reassigning_it(self):
        script = (ROOT / "install-zane.sh").read_text(encoding="utf-8")
        self.assertIn("export AIMILI_PROFILE", script)
        self.assertNotIn(
            'AIMILI_PROFILE="$AIMILI_PROFILE" bash '
            '"$AIMILI_INSTALL_DIR/scripts/verify-formal-install.sh"',
            script,
        )


if __name__ == "__main__":
    unittest.main()
