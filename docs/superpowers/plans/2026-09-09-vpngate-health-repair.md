# VPNGate Health Repair Implementation Plan

> **For agentic workers:** Follow red-green TDD and verify each production host after deployment.

**Goal:** Repair snapshot recovery, policy lifetime, UI binding, and log rotation with the smallest compatible change.

**Architecture:** Keep the existing snapshot and installer pipelines. Change only validation of the replaced snapshot, pinned policy values, safe upgrade migration, and native logrotate installation.

**Tech Stack:** Python 3 standard library, unittest, Bash, systemd, logrotate.

## Global Constraints

- Preserve `vpngate_data`, UI credentials, existing ingress services, and fail-closed routing.
- Do not expose secrets in repository files, logs, or handoff notes.
- Create dated remote backups before deployment and restart only AimiliVPN when required.

### Task 1: Snapshot recovery

**Files:** `tests/test_pool_snapshot.py`, `pool_snapshot.py`

- [ ] Add a test where current schema v2 is expired and incoming schema v2 is fresh with a higher sequence.
- [ ] Run the focused test and confirm it fails with `snapshot is expired`.
- [ ] Parse current JSON within `max_bytes`, validate schema and positive integer sequence without freshness validation, and retain monotonic comparison.
- [ ] Run all snapshot tests.

### Task 2: Policy, UI and log rotation

**Files:** `tests/test_formal_install_profile.py`, `tests/test_ui_host.py`, `config/*.env`, `vpngate_manager.py`, `install-zane.sh`, `scripts/verify-formal-install.sh`, `systemd/aimilivpn.logrotate`

- [ ] Change tests to require TTL 108000, loopback UI default, safe wildcard-host migration, installed logrotate config, and verifier checks.
- [ ] Run focused tests and confirm failures reflect the old behavior.
- [ ] Apply the minimum profile/default/installer changes and add the static logrotate file.
- [ ] Run focused tests, then the full unit/syntax/diff gate.

### Task 3: Package and deploy

**Systems:** 东京、弗里蒙特、云途

- [ ] Commit the verified source, build a commit-addressed archive and checksum, then verify archive contents.
- [ ] Push the exact commit to private `main`, publish matching private release assets, and redownload/checksum them.
- [ ] Back up each host's app, environment, UI config, units and log state.
- [ ] Deploy local-authority to 东京/弗里蒙特 and upstream-consumer to 云途, preserving host-specific settings and credentials.
- [ ] Force logrotate once, run export/sync once, and verify UI listeners, service/timer state, snapshots and unrelated ingress services.

### Task 4: Handoff

- [ ] Update the three VPS main txt files with release, backup, policy, listener and verification state without secrets.
- [ ] Mark `.ai/HANDOFF.md` complete only after fresh local and remote gates pass.
