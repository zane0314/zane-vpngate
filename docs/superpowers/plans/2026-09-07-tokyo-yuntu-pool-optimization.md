# Tokyo Authority and Yuntu Throughput Pool Optimization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use the existing TDD workflow and execute this plan task-by-task with a verification checkpoint after every task.

**Goal:** Make Tokyo publish only hard-gate-passed VPNGate nodes and make Yuntu measure real local single-connection throughput to choose active/standby nodes, with bounded snapshots and cleanup on both VPSes.

**Architecture:** Tokyo runs local-authority: it fetches VPNGate, performs one complete Japanese-residential hard-gate check, classifies passed nodes as trusted, observation, or overflow candidate, and exports at most 300 passed nodes without throughput fields. Yuntu runs upstream-consumer: it validates Tokyo's snapshot, locally measures one connection per queued node, ranks the measured nodes by local throughput, and keeps the existing dual-slot/fail-closed and public entry pipeline.

**Tech Stack:** Python 3 standard library, JSON snapshots, systemd timers/services, OpenVPN/TUN, existing AimiliVPN vpngate_manager.py, unittest.

## Global Constraints

- Every exported node must have hard_gate=passed; fewer than 300 passed nodes means a shorter snapshot.
- Tokyo does not export throughput measurements; Yuntu owns local_download_mbps and related local probe state.
- candidate means hard-gate-passed but waiting for Yuntu throughput measurement; it is not an unverified node.
- Trusted/observation hard gates remain Japanese residential and risk-free; trusted admission changes to one successful full hard-gate probe.
- A hard failure removes a node from the usable pool immediately; active failover keeps existing short debounce behavior.
- Snapshot writes are atomic, sequence-monotonic, hash-validated, bounded to 300 nodes, and expire by time.
- Tokyo and Yuntu retain only current/previous snapshots and bounded runtime state; stale configs and probe records are pruned.
- Do not modify 3x-ui, nginx, Cloudflare, existing VLESS/Trojan/HY2 entry routing, or unrelated services.
- Do not write VPS credentials, real addresses, private keys, or credential-bearing URLs into the repository or handoff file.
- Production code changes follow red-green TDD: write a failing unittest, run it, implement the smallest change, run the focused test, then run the full suite.

---

### Task 1: Reconcile the working tree and capture the live deployment baseline

**Files:**
- Read: .ai/HANDOFF.md, the approved design document, and both profile files.
- Inspect: local Git state and both VPS runtime state.
- Modify: .ai/HANDOFF.md only.

**Interfaces:**
- Consumes: local inventory files under /Users/zane/Documents/重要信息/vps/, current origin/main, and installed Tokyo/Yuntu services.
- Produces: a verified baseline with one next action; no code behavior changes.

- [ ] Step 1: Reconcile Git without discarding user commits.

Run:

~~~~bash
git status --short --branch
git log --graph --oneline --decorate --all -12
git diff --stat HEAD..origin/main
~~~~

Keep later user documentation changes and the local design commit. Do not reset or checkout over either side.

- [ ] Step 2: Verify both VPS roles read-only.

Read each host from the local inventory, without copying host values into the repository. On each host run:

~~~~bash
hostname
systemctl is-active aimilivpn
systemctl is-active x-ui nginx
grep -E '^(POOL_SOURCE_MODE|LOCAL_PROXY_HOST|LOCAL_PROXY_PORT|STANDBY_TARGET|STANDBY_SCAN_BATCH_SIZE|STANDBY_SCAN_LIMIT)=' /etc/default/aimilivpn
find /opt/aimilivpn/vpngate_data -maxdepth 1 -type f -printf '%f %s bytes\n' | sort
~~~~

Expected baseline: Tokyo is local-authority; Yuntu is upstream-consumer; both preserve existing data directories and entry services.

- [ ] Step 3: Update .ai/HANDOFF.md with the exact verified roles, current Git commit, plan path, and one next action. Do not record credentials or full URLs.

- [ ] Step 4: Checkpoint.

~~~~bash
git status --short --branch
~~~~

Expected: no unexplained source changes.

---

### Task 2: Add the hard-gate-aware snapshot schema and bounded export

**Files:**
- Modify: pool_snapshot.py
- Modify: scripts/export_pool_snapshot.py
- Modify: tests/test_pool_snapshot.py
- Modify: tests/test_pool_sync_scripts.py

**Interfaces:**
- Consumes: Tokyo nodes.json, state pool IDs, and a generated timestamp.
- Produces: schema version 2 snapshots whose every node has hard_gate=passed, gate timestamps, source metadata, and one of the three pool labels.

- [ ] Step 1: Write failing tests for export filtering, required hard-gate fields, expiry, pool overlap, and the 300-node cap.

Use real dictionaries and pool_snapshot functions. The tests must prove that pending, failed, and expired records are not exported and that a short passed set is not padded.

- [ ] Step 2: Run the focused tests and confirm the expected failure.

~~~~bash
python3 -m unittest tests.test_pool_snapshot tests.test_pool_sync_scripts -v
~~~~

Expected: failures because the existing schema accepts unverified nodes and uses schema version 1.

- [ ] Step 3: Implement schema version 2 in pool_snapshot.py.

1. Set SCHEMA_VERSION to 2.
2. Add a helper that accepts a node only when hard_gate is passed, the gate timestamps are valid, hard_gate_expires_at is later than the snapshot generation time, the country is Japan, the type is residential, and risk_free is true.
3. Make build_snapshot accept max_nodes=300 and discard all non-current hard-gate nodes before constructing pools.
4. Preserve trusted/observation ordering, then add hard-gate-passed overflow nodes as candidate until max_nodes is reached.
5. Require all pool references to be exported nodes and reject overlap among primary pools.
6. Export hard_gate, gate timestamps, actual country/type, risk_free, config_hash, and source timestamps. Do not export throughput.
7. Make validate_snapshot reject schema 1, missing or expired hard-gate fields, non-residential records, unknown references, overlap, and more than max_nodes nodes.

- [ ] Step 4: Update scripts/export_pool_snapshot.py to pass max_nodes into build_snapshot, default source_instance to tokyo, validate before replacing the output, and preserve atomic writes.

- [ ] Step 5: Update all snapshot fixtures to schema 2 and run:

~~~~bash
python3 -m unittest tests.test_pool_snapshot tests.test_pool_sync_scripts -v
~~~~

- [ ] Step 6: Commit.

~~~~bash
git add pool_snapshot.py scripts/export_pool_snapshot.py tests/test_pool_snapshot.py tests/test_pool_sync_scripts.py
git commit -m "feat: export only hard-gated pool snapshots"
~~~~

---

### Task 3: Change Tokyo pool admission, overflow candidates, and immediate hard-failure ejection

**Files:**
- Modify: node_pool.py
- Modify: vpngate_manager.py
- Modify: config/aimilivpn.env
- Modify: config/aimilivpn-upstream.env
- Modify: tests/test_node_pool.py
- Modify: relevant manager tests.

**Interfaces:**
- Consumes: full local probe results and current node pool state.
- Produces: one-pass trusted admission, hard-gate timestamps, overflow hard-passed candidates, and immediate source-pool removal on hard failure.

- [ ] Step 1: Write failing tests for one-success trusted admission, one-failure cooldown, hard-passed overflow candidate state, and non-exportability of unprobed candidates.

- [ ] Step 2: Run the focused tests and confirm the old rules fail.

~~~~bash
python3 -m unittest tests.test_node_pool -v
~~~~

Expected: trusted currently requires two successes, hard failures wait for two failures, and eligible overflow is marked cooldown.

- [ ] Step 3: Implement the smallest source-side change.

1. Change TRUST_MIN_SUCCESSES to 1 in runtime defaults and both profiles.
2. Add HARD_GATE_TTL_SECONDS with a two-hour default to both profiles and runtime constants.
3. On every full result, set hard_gate=passed and checked/expiry timestamps only when the real exit is JP, residential, and risk-free; otherwise set hard_gate=failed with a non-secret reason.
4. Reconcile a definitive unavailable/hard-gate failure into cooldown immediately; never-tested nodes remain non-exportable candidates.
5. Mark every eligible node outside trusted/observation limits as candidate, not cooldown.
6. Keep Tokyo source classification independent of throughput.
7. On a successful official refresh, remove inactive nodes that disappeared from the current official list while retaining only a necessary active draining exception.

- [ ] Step 4: Add and test a standard-library helper that deletes only managed .ovpn files outside a retained ID set; unrelated files must remain untouched.

- [ ] Step 5: Run:

~~~~bash
python3 -m unittest tests.test_node_pool tests.test_formal_install_profile -v
~~~~

- [ ] Step 6: Commit.

~~~~bash
git add node_pool.py vpngate_manager.py config/aimilivpn.env config/aimilivpn-upstream.env tests/test_node_pool.py tests/test_formal_install_profile.py
git commit -m "feat: admit hard-gated nodes once and eject failures immediately"
~~~~

---

### Task 4: Make Yuntu consume candidate nodes and rank by local single-connection throughput

**Files:**
- Modify: upstream_pool.py
- Modify: vpngate_manager.py
- Modify: config/aimilivpn-upstream.env
- Modify: tests/test_upstream_pool.py
- Modify: tests/test_upstream_manager_mode.py
- Create: tests/test_upstream_throughput_selection.py

**Interfaces:**
- Consumes: schema 2 Tokyo snapshots and local probe state.
- Produces: local throughput fields, candidate probe batches of 10, tier-aware active/standby order, immediate local hard-failure removal, and bounded probe state.

- [ ] Step 1: Write failing tests for candidate connectability after hard-gate pass, local throughput ranking, observation fallback, a maximum ten-node probe queue, persisted speed fields, immediate hard-failure removal, and stale state/config cleanup.

- [ ] Step 2: Run focused tests and confirm the old behavior fails.

~~~~bash
python3 -m unittest tests.test_upstream_pool tests.test_upstream_manager_mode tests.test_upstream_throughput_selection -v
~~~~

Expected: candidate is rejected, the consumer probe is only a 1 MB lightweight check, and no local throughput ordering exists.

- [ ] Step 3: Extend upstream_pool.py.

1. Map schema 2 hard-gate fields into local nodes.
2. Include candidate_ids after trusted and observation IDs in the allowed order.
3. Reject expired/non-passed hard-gate records and local cooldowns, but do not reject candidate solely because of its source label.
4. Add probe_queue_ids(nodes, limit=10, now=...) that prefers unmeasured hard-gate-passed nodes, then expired measurements, with trusted/observation before candidate.
5. Add rank_local_nodes(nodes, now=...) using fresh local_download_mbps within the selected tier, then freshness and deterministic IDs. Allow a candidate to challenge the current preferred node only when its local speed exceeds the current best by the existing 30% gain threshold.
6. Add prune_probe_state() to remove records whose IDs/config hashes are absent from the current snapshot or beyond retention.

- [ ] Step 4: Extend vpngate_manager.py consumer probing.

1. Keep Tokyo's reputation decision authoritative; never call score_ip_reputation in consumer mode.
2. Replace the 1 MB lightweight check with an isolated TUN single-connection probe using UPSTREAM_LOCAL_PROBE_BYTES=25,000,000 and one sample.
3. Persist local_download_mbps, local_ttfb_ms, local_measured_at, local_probe_bytes, local_probe_path, last_probe_success, last_probe_error_class, and config_hash.
4. Overlay local fields when refresh_upstream_pool_state rebuilds nodes.json.
5. Remove the candidate-only rejection from validate_node_allowed_by_routing.
6. Add maintain_upstream_consumer_pool() to refresh the snapshot, test at most ten queued nodes at the configured interval, persist results, prune stale state/configs, update standby ordering, and invoke existing failover only when there is no healthy active slot.
7. Make standby_nodes use local throughput ranking and keep active-node stickiness. Do not switch merely because a new node appears unless the existing speed-gain and daily switch guard is satisfied.

- [ ] Step 5: Add these bounded consumer settings:

~~~~dotenv
UPSTREAM_SNAPSHOT_MAX_AGE_SECONDS=7200
UPSTREAM_LOCAL_PROBE_BATCH_SIZE=10
UPSTREAM_LOCAL_PROBE_INTERVAL_SECONDS=300
UPSTREAM_LOCAL_PROBE_BYTES=25000000
UPSTREAM_LOCAL_PROBE_SAMPLES=1
UPSTREAM_LOCAL_PROBE_RETENTION_SECONDS=172800
~~~~

Keep UPSTREAM_SNAPSHOT_MAX_NODES=300. Runtime source host/key values remain only in VPS-local /etc/default/aimilivpn and key files.

- [ ] Step 6: Run focused tests and commit.

~~~~bash
python3 -m unittest tests.test_upstream_pool tests.test_upstream_manager_mode tests.test_upstream_throughput_selection -v
git add upstream_pool.py vpngate_manager.py config/aimilivpn-upstream.env tests/test_upstream_pool.py tests/test_upstream_manager_mode.py tests/test_upstream_throughput_selection.py
git commit -m "feat: rank upstream nodes by local throughput"
~~~~

---

### Task 5: Change snapshot cadence and install cleanup-safe systemd wiring

**Files:**
- Modify: systemd/aimilivpn-pool-export.timer
- Modify: systemd/aimilivpn-pool-sync.timer
- Modify: systemd/aimilivpn-pool-export.service
- Modify: tests/test_formal_install_profile.py
- Modify: tests/test_pool_sync_scripts.py

**Interfaces:**
- Consumes: schema 2 exporter and puller.
- Produces: hourly Tokyo export and Yuntu sync, source label tokyo, atomic previous-snapshot fallback, and no empty-snapshot replacement on source failure.

- [ ] Step 1: Write failing timer/source assertions for hourly Persistent timers, tokyo source labeling, and the new bounded consumer settings.

- [ ] Step 2: Run the focused tests and confirm the existing three-times-daily timers fail the new assertions.

~~~~bash
python3 -m unittest tests.test_formal_install_profile tests.test_pool_sync_scripts -v
~~~~

- [ ] Step 3: Change both timers to hourly with Persistent=true, change the export service to call export_pool_snapshot.py --source-instance tokyo, and retain all existing systemd hardening and restricted snapshot reading.

- [ ] Step 4: Run focused tests and commit.

~~~~bash
python3 -m unittest tests.test_formal_install_profile tests.test_pool_sync_scripts -v
git add systemd/aimilivpn-pool-export.timer systemd/aimilivpn-pool-sync.timer systemd/aimilivpn-pool-export.service tests/test_formal_install_profile.py tests/test_pool_sync_scripts.py
git commit -m "feat: refresh pool snapshots hourly"
~~~~

---

### Task 6: Run the complete local regression gate and update operational documentation

**Files:**
- Modify: docs/云途双槽与VLESS-Trojan入口实施说明.md
- Modify: design/spec only if implementation details require clarification.
- Read: all changed Python and systemd files.

- [ ] Step 1: Run:

~~~~bash
python3 -m unittest discover -s tests -p 'test_*.py' -v
python3 -m compileall -q .
bash -n install.sh install-zane.sh install-local-package.sh scripts/*.sh
git diff --check
~~~~

Expected: all tests pass, compilation and shell syntax pass, and no whitespace errors are reported.

- [ ] Step 2: Scan the staged tree for private-key headers, credential-bearing URLs, non-placeholder VPS addresses, passwords, UUIDs, and tokens. Do not scan or commit the local VPS inventory directory.

- [ ] Step 3: Document only non-secret facts: Tokyo authority, Yuntu local throughput, schema 2 hard-gate contract, hourly cleanup, candidate batch size 10, max 300 passed nodes, and rollback boundaries.

- [ ] Step 4: Commit the documentation and update .ai/HANDOFF.md with the tested commit and one next action.

---

### Task 7: Package and deploy the verified runtime to Tokyo first

**Systems:**
- Read: the Tokyo VPS inventory file.
- Remote modify: Tokyo /opt/aimilivpn, /etc/default/aimilivpn, and pool-export unit files.
- Preserve: Tokyo vpngate_data, UI credentials, x-ui/nginx/Cloudflare/ARGO services.

**Interfaces:**
- Consumes: the exact verified local release commit and current Tokyo data.
- Produces: a Tokyo local-authority schema 2 export with only hard-gate-passed nodes and an hourly export timer.

- [ ] Step 1: Create an archive and adjacent SHA-256 from the exact verified commit; verify locally before upload.

- [ ] Step 2: Create a dated Tokyo rollback backup containing the application tree, profile, export units, and a redacted state summary. Do not copy credential files into chat or the repository.

- [ ] Step 3: Upload to a fresh remote temporary directory, verify SHA-256 remotely, extract, and run install-local-package.sh with the local-authority profile. Preserve vpngate_data and UI credentials.

- [ ] Step 4: Set the runtime source label to tokyo, install/reload the export service and timer, run one export, and verify:

~~~~bash
python3 - <<'PY'
import json
from pathlib import Path
p = Path('/var/lib/aimilivpn-export/pool-snapshot.json')
v = json.loads(p.read_text())
assert v['schema_version'] == 2
assert v['source_instance'] == 'tokyo'
assert len(v['nodes']) <= 300
assert all(n['hard_gate'] == 'passed' for n in v['nodes'].values())
PY
~~~~

- [ ] Step 5: Verify aimilivpn, x-ui, nginx, ARGO, and HY2 are active, the local proxy is loopback-only, the export timer is enabled, failed units are empty, and no pending/failed/expired node is exported.

- [ ] Step 6: If installation or export validation fails, restore the dated backup, disable only the new export timer, restart only the affected AimiliVPN service, and leave unrelated entry services untouched. Update .ai/HANDOFF.md with actual output and rollback path.

---

### Task 8: Switch Yuntu from the old source to Tokyo and validate local throughput

**Systems:**
- Read: the Yuntu VPS inventory file.
- Remote modify: Yuntu /opt/aimilivpn, /etc/default/aimilivpn, pool-sync units, and the restricted SSH source access.
- Preserve: Yuntu vpngate_data, vt-vpngate, vt-prober, aimili-health, hy2-vpngate, nginx, and existing entry ports.

**Interfaces:**
- Consumes: Tokyo schema 2 snapshot through the existing restricted SSH reader.
- Produces: Yuntu source tokyo, local single-connection measurements, throughput-ranked active/standby, and bounded cleanup.

- [ ] Step 1: Create a dated Yuntu rollback backup of the application tree, profile, sync units, snapshots, and a redacted service/listener summary.

- [ ] Step 2: Upload the same checksum-verified archive, run the upstream-consumer profile installer, and preserve Yuntu vpngate_data and existing sidecars.

- [ ] Step 3: Set the Yuntu runtime UPSTREAM_SYNC_HOST from the local inventory to Tokyo, verify Tokyo's restricted aimili-sync reader accepts only the fixed snapshot command, install Tokyo's host key in the managed known-hosts file, and keep private keys outside the repository.

- [ ] Step 4: Run the sync service manually and assert on Yuntu:

~~~~bash
python3 - <<'PY'
import json
from pathlib import Path
p = Path('/opt/aimilivpn/vpngate_data/upstream-snapshot.json')
v = json.loads(p.read_text())
assert v['schema_version'] == 2
assert v['source_instance'] == 'tokyo'
assert len(v['nodes']) <= 300
assert all(n['hard_gate'] == 'passed' for n in v['nodes'].values())
PY
~~~~

- [ ] Step 5: Verify the next consumer cycle queues at most ten unmeasured nodes, stores local throughput values, does not call local reputation scoring, and can consider a measured candidate without changing its upstream pool label.

- [ ] Step 6: Verify trusted highest local throughput is preferred, second trusted is standby, observation fills when trusted is empty, hard-failed nodes are removed immediately, stale configs/probe records are pruned, and active/standby failover remains fail-closed.

- [ ] Step 7: Verify vt-vpngate, vt-prober, aimili-health, hy2-vpngate, nginx, loopback 7928, no default-route leak, no failed units, and existing sidecar exports. Do not expose management port 8787.

Update both VPS inventory handoff files with non-secret runtime facts, backup paths, source sequence/count, service status, and verification time. Preserve each login block exactly.

---

### Task 9: Final verification, documentation checkpoint, and handoff

**Files:**
- Read all changed files and design/plan documents.
- Modify .ai/HANDOFF.md and VPS inventory files only with non-secret runtime state.

- [ ] Step 1: Run the fresh local verification gate:

~~~~bash
python3 -m unittest discover -s tests -p 'test_*.py' -v
python3 -m compileall -q .
bash -n install.sh install-zane.sh install-local-package.sh scripts/*.sh
git diff --check
~~~~

- [ ] Step 2: Run fresh remote acceptance checks for service state, timers, schema 2 source tokyo, node count, all hard-gate statuses, local probe batch size, listener ownership, failed units, no-leak, and fail-closed behavior.

- [ ] Step 3: Verify Git state before any push:

~~~~bash
git status --short --branch
git log --oneline --decorate -8
git diff --check origin/main..HEAD
~~~~

Do not push automatically.

- [ ] Step 4: Set .ai/HANDOFF.md to 已完成 only if both VPS gates and local tests pass. Otherwise record the exact failing gate, rollback path, and one actionable next step.
