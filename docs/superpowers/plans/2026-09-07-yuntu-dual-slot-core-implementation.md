# 云途双槽核心改造实施计划（阶段一）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 AimiliVPN 从单 tun0 改造为 tun0/tun1 双槽主备 + 7928 按连接动态绑定活动槽 + 7 行故障矩阵 + fail-closed，并在云途完成部署与故障注入验收。

**Architecture:** 新增 `slot_state.py`（槽位定义 + 原子活动槽指针）与 `health_probes.py`（VPNGate 槽探针 + 前端探针接口 + 纯函数故障矩阵）；改造 `proxy_server.py` 每条新连接读取活动槽指针绑定对应 TUN；`vpngate_manager.py` 的单隧道全局态槽位化，策略路由表按槽拆分（tun0→100，tun1→101）。云途为 upstream-consumer 模式，本地不重新评分。

**Tech Stack:** Python 3 标准库（unittest + unittest.mock）、OpenVPN、sing-box 1.15、systemd。

**执行依据：** `docs/superpowers/specs/2026-09-07-tokyo-vpngate-dual-slot-vless-trojan-design-v2.md`（下称 v2 设计）。云途适配：前端探针在本阶段为可选配置（sidecar 属阶段二另一份计划）；未配置前端探针时故障矩阵退化为仅以 VPNGate 探针判定出口故障，仍遵守"不确定即不切换、不计节点失败"原则。

## Global Constraints

- 任何情况下代理流量不得回落云途默认公网；活动槽指针为空/非法/槽位未验证时 7928 必须拒绝连接。
- 状态写入一律 tmp + fsync + rename 原子替换；状态文件不保存私钥、密码、完整分享链接或 OpenVPN 内嵌凭据。
- 总并发 ≤2（双隧道 + 候选测试 + 测速合计）；禁止无上限创建 TUN/OpenVPN 进程。
- 切换后旧槽排空最长 60 秒才允许重建为新 standby；切换成功后 10 分钟冷却期内只响应硬故障；同一节点失败冷却 6 小时。
- active 与 standby 实测出口 IP 必须不同。
- 测试框架为 unittest，从仓库根目录运行 `python3 -m unittest discover -s tests -v`；需要数据目录隔离时在 import vpngate_manager 之前设置 `os.environ["VPNGATE_DATA_DIR"]` 到 `tempfile.TemporaryDirectory()`。
- 不修改用户已删除的 4 个旧文档；不推送 GitHub。
- 本计划只覆盖阶段一（双槽核心 + 云途部署）。阶段二（云途 VLESS/Trojan sing-box sidecar + 前端探针接入 + 8787 收紧 + 端到端终验）在阶段一验收后另写计划。

---

### Task 1: slot_state 模块（槽位定义 + 原子活动槽指针）

**Files:**
- Create: `slot_state.py`
- Test: `tests/test_slot_state.py`

**Interfaces:**
- Produces:
  - `SLOT_DEVICES = {"A": "tun0", "B": "tun1"}`、`SLOT_TABLES = {"A": 100, "B": 101}`
  - `device_for_slot(slot: str) -> str`、`slot_for_device(device: str) -> str | None`
  - `read_active_slot(data_dir: str) -> dict | None` — 返回 `{"slot": "A"|"B", "node_id": str, "device": str, "updated_at": float}`；文件缺失/损坏/slot 非法/device 与槽位不匹配时返回 `None`
  - `write_active_slot(data_dir: str, slot: str, node_id: str) -> None` — tmp+fsync+rename；slot 非法抛 `ValueError`
  - `clear_active_slot(data_dir: str) -> None` — 写入 `{"slot": null}` 而非删文件（读者将 null 视为无活动槽）
  - `read_slot_states(data_dir: str) -> dict`、`write_slot_states(data_dir: str, states: dict) -> None` — 每槽 `{node_id, device, exit_ip, asn, verified_at, health}`，同一原子写协议，文件名 `slot_states.json`

- [ ] **Step 1: 写失败测试** `tests/test_slot_state.py`：读写往返；损坏 JSON 返回 None；非法 slot（"C"）抛 ValueError；device 与 slot 不匹配返回 None；clear 后 read 返回 None；写入后 tmp 文件无残留；`SLOT_DEVICES`/`SLOT_TABLES` 恰好两个槽且 device/table 不重复。
- [ ] **Step 2: 运行确认失败**：`python3 -m unittest tests.test_slot_state -v`，预期 `ModuleNotFoundError`。
- [ ] **Step 3: 实现 `slot_state.py`**：`write_active_slot` 用 `tempfile.mkstemp(dir=data_dir)` + `os.fsync` + `os.replace`；`read_active_slot` 校验 `slot in SLOT_DEVICES` 且 `device == SLOT_DEVICES[slot]`。
- [ ] **Step 4: 运行确认通过**：同上命令，全 PASS。
- [ ] **Step 5: 提交**：`git add slot_state.py tests/test_slot_state.py && git commit -m "feat: add dual-slot state module with atomic active slot pointer"`

### Task 2: proxy_server 按连接动态绑定活动槽

**Files:**
- Modify: `proxy_server.py`（核心触点 87–117、181–215 行）
- Test: `tests/test_proxy_dynamic_slot.py`

**Interfaces:**
- Consumes: `slot_state.read_active_slot(data_dir)`
- Produces（供 manager/测试使用）:
  - `resolve_active_device(data_dir: str) -> str` — 读指针并校验 `/sys/class/net/<device>` 存在；无指针或设备缺失抛 `ProxyError(3004, ...)`（沿用现有 3004 语义）
  - `create_connection(address, timeout=20, device=None)` — `device=None` 时内部调 `resolve_active_device`；显式传 device 供探针/测试绕过指针
  - `resolve_dns_over_tun(host, qtype="A", dns_server="8.8.8.8", timeout=5, device=None)` — 同上的 DNS 版本；保留 `resolve_dns_over_tun0(host, ...)` 与 `dns_query_over_tun0(...)` 作为不传 device 的兼容包装（现有测试 `tests/test_residential_policy.py:124` mock 旧名，不得破坏）
  - 模块级 `PROXY_DATA_DIR = os.environ.get("VPNGATE_DATA_DIR", ...)`，运行时读取以便测试注入

- [ ] **Step 1: 写失败测试** `tests/test_proxy_dynamic_slot.py`：
  - 指针指向 A → `resolve_active_device` 返回 `tun0`（mock `/sys/class/net` 存在性检查与 read_active_slot）
  - 指针为 None / 损坏 / 设备不存在 → 抛 3004，且 socket 构造绝不被调用（mock `socket.socket` 断言未调用，证明 fail-closed 在绑定前生效）
  - `create_connection` 对 `SO_BINDTODEVICE` 使用指针解析出的设备名（mock socket，断言 `setsockopt` 收到 `b"tun1"`）
  - DNS 查询 socket 与 TCP socket 使用同一 device
  - 兼容包装 `resolve_dns_over_tun0` 仍可调
- [ ] **Step 2: 运行确认失败**（函数不存在）。
- [ ] **Step 3: 实现**：把 112 行与 207 行的 `b"tun0"` 字面量替换为 `device.encode()`；device 在每次 `create_connection`/`resolve_dns_over_tun` 调用开头解析一次并贯穿该连接。**不改** 236–296 行 SOCKS5 协议逻辑。
- [ ] **Step 4: 运行新测试 + 既有全量** `python3 -m unittest discover -s tests -v`，全 PASS（特别注意 `test_residential_policy.py` 不回归）。
- [ ] **Step 5: 提交**：`git commit -m "feat: bind 7928 connections to dynamically resolved active slot device"`

### Task 3: manager 槽位化（策略路由表拆分 + per-slot 隧道状态）

**Files:**
- Modify: `vpngate_manager.py`（182–188 全局态、1081/1224 openvpn 启动、1320–1358 策略路由、2185–2410 connect_node、1360 stop_active_openvpn、1484 enter_fail_closed）
- Test: `tests/test_dual_slot_manager.py`

**Interfaces:**
- Consumes: `slot_state` 全部接口。
- Produces:
  - `SLOT_RUNTIME = {"A": {"process": None, "node_id": None, "draining_since": None}, "B": {...}}`（替代全局 `active_openvpn_process/active_openvpn_node_id`；保留旧全局名作为指向 active 槽的只读属性或删除并更新全部引用——由实现者搜索全文件引用点统一替换）
  - `setup_policy_routing(interface, table)` / `cleanup_policy_routing(interface, table)` — table 参数化；tun0→100、tun1→101（取 `slot_state.SLOT_TABLES`）；`cleanup_test_artifacts` 的 2002+ 清理范围不变
  - `connect_node(node_id, manual=False, slot="A")` — 在指定槽建隧道：`openvpn_command(..., dev=SLOT_DEVICES[slot])` → `setup_policy_routing(dev, SLOT_TABLES[slot])` → 出口验证 → 写 `slot_states.json`
  - `stop_slot_openvpn(slot)` — 只停指定槽进程并清该槽路由规则；`stop_active_openvpn()` 改为停当前 active 槽（保持旧签名供既有调用点/UI API 使用）
  - `verify_slot_egress(slot) -> dict` — 复用 `vpn_utils.probe_tunnel_egress(device, download_bytes=小, upload_bytes=0, samples=1)` 取实测出口 IP/国家；consumer 模式校验"出口 IP ≠ 本机默认公网 IP 且与另一槽不同"（不做住宅重评分，遵守 consumer 约束）

- [ ] **Step 1: 写失败测试** `tests/test_dual_slot_manager.py`：
  - `setup_policy_routing("tun1", 101)` 调 `ip route add default dev tun1 table 101` 与 `ip rule add oif tun1 table 101`（mock subprocess，断言参数）
  - `connect_node(..., slot="B")` 全流程 mock 后：`openvpn_command` 收到 `dev="tun1"`、写指针前 `verify_slot_egress` 被调、成功后 `slot_states.json` B 槽有 node_id 与 exit_ip
  - 出口验证失败时不写指针、不进 active（fail-closed）
  - 两槽出口 IP 相同时 `connect_node` 拒绝并记原因
  - `stop_slot_openvpn("A")` 不影响 B 槽 process
- [ ] **Step 2: 运行确认失败。**
- [ ] **Step 3: 实现**。`connect_node` 内部把 2229 行无条件 `stop_active_openvpn()` 改为 `stop_slot_openvpn(slot)`；成功路径 2400–2407 的 `set_state` 之外增加 `write_slot_states`；**只有** `promote_slot_to_active`（Task 5）负责写活动指针，connect_node 本身不写指针。
- [ ] **Step 4: 全量测试通过。**
- [ ] **Step 5: 提交**：`git commit -m "feat: slot-aware tunnel management with per-slot policy routing"`

### Task 4: health_probes 模块（VPNGate 槽探针 + 纯函数故障矩阵）

**Files:**
- Create: `health_probes.py`
- Test: `tests/test_health_probes.py`

**Interfaces:**
- Consumes: `slot_state`、`vpn_utils.probe_tunnel_egress`
- Produces:
  - `ProbeResult = dict`（`{"ok": bool, "detail": str, "checked_at": float}`）
  - `probe_vpngate_slot(slot: str, data_dir: str) -> ProbeResult` — 检查槽进程存活、`/sys/class/net/<dev>` 存在、经该 dev 的 TCP HTTPS 与出口 IP（绑定设备，**不得经过 7928**）
  - `probe_frontend(name: str, cfg: dict) -> ProbeResult` — 阶段一实现"未配置返回 `{"ok": True, "detail": "not-configured"}` + 已配置时 TCP 连接 `cfg["host"]:cfg["port"]` 的占位实现；sing-box 握手实现属阶段二
  - `classify_failure(vpngate_ok: bool, vless_ok: bool, trojan_ok: bool) -> dict` — 返回 `{"attribution": str, "action": "hold"|"failover"|"pause_rotation", "count_node_failure": bool, "alert": str|None}`，严格按下表（v2 设计 §8.3）：

| vpngate | vless | trojan | attribution | action | count_node_failure |
| --- | --- | --- | --- | --- | --- |
| True | False | True | vless-only | hold | False |
| True | True | False | trojan-only | hold | False |
| True | False | False | frontend-stack | hold（并暂停性能择优） | False |
| False | True | True | vpngate-exit | failover | True |
| False | False | False | host-stack | pause_rotation | False |
| False | False | True | vpngate-exit+vless | failover | True |
| False | True | False | vpngate-exit+trojan | failover | True |

- [ ] **Step 1: 写失败测试**：矩阵 7 行逐行断言（参数化子测试）；`probe_vpngate_slot` 在进程死/设备缺失/出口 IP 等于基线公网 IP 时分别 `ok=False`；未配置前端探针返回 `not-configured`。
- [ ] **Step 2: 运行确认失败。**
- [ ] **Step 3: 实现**。`classify_failure` 为无副作用纯函数；探针函数所有 subprocess/socket 走注入点便于 mock。
- [ ] **Step 4: 全量测试通过。**
- [ ] **Step 5: 提交**：`git commit -m "feat: add health probes and 7-row failure classification matrix"`

### Task 5: 切换编排（promote/drain/refill + 冷却 + 启动恢复）

**Files:**
- Modify: `vpngate_manager.py`（`background_proxy_checker` 5774–5828、`auto_switch_node` 2062、`enter_fail_closed` 1484、`main` 6553–6665 启动序列）
- Test: `tests/test_failover_orchestration.py`

**Interfaces:**
- Consumes: Task 1–4 全部接口。
- Produces:
  - `promote_slot_to_active(slot, node_id) -> None` — 唯一写活动指针的入口；写前必须 `probe_vpngate_slot(slot)` 通过，否则抛 `RuntimeError` 且不动旧指针
  - `failover_to_standby() -> bool` — 待机槽快速复验 → promote → 原 active 槽标 `draining_since=now`（保留隧道 ≤`DRAIN_SECONDS=60` 供已建连接）→ 记节点失败 + 6h 冷却 → 触发补备
  - `run_failure_attribution() -> dict` — 连续 2 次 VPNGate 探针失败（间隔 5 秒）后执行：各前端探针 2 次取连续失败 → `classify_failure` → 按 action 分发（failover / hold+alert / pause_rotation）；`count_node_failure=False` 时绝不写节点失败历史
  - 冷却开关：切换成功后写 state `switch_cooldown_until = now + 600`；`maybe_switch_to_better_node` 在冷却期内直接返回
  - 启动恢复：`_recover_slots_on_boot()` — 读 `slot_states.json` 但不信任 → 逐槽重建隧道并 `verify_slot_egress` → active 验证通过才 `promote_slot_to_active`；两槽皆败保持无指针（7928 自然拒绝）；`main()` 在 proxy 线程就绪后、collector 启动前调用
  - env：`DRAIN_SECONDS`(60)、`SWITCH_COOLDOWN_SECONDS`(600)、`NODE_FAILURE_COOLDOWN_SECONDS`(21600)、`VPNGATE_PROBE_FAIL_THRESHOLD`(2)、`FRONTEND_PROBE_CONFIG`（JSON 文件路径，缺省=None）

- [ ] **Step 1: 写失败测试**：
  - standby 复验失败 → 指针不变、active 不断
  - 复验成功 → 指针原子切换、旧槽进入 draining、失败节点写 6h 冷却
  - draining 槽超过 DRAIN_SECONDS 后才允许被 refill 复用
  - 无可用候选 → `clear_active_slot` + `fail_closed=True`
  - 启动恢复：slot_states 伪造"上次 active=A 健康"但重建验证失败 → 提升已验证 B；两槽均失败 → 无指针
  - 冷却期内 `maybe_switch_to_better_node` 不动作
  - 矩阵分发：`host-stack` 时 `pause_rotation=True` 且不写节点失败；`vless-only` 时保持状态且告警字段非空
- [ ] **Step 2: 运行确认失败。**
- [ ] **Step 3: 实现**。`background_proxy_checker` 的失败分支改为调 `run_failure_attribution` 而非直接 `mark_blacklisted + auto_switch_node`；`enter_fail_closed` 增加 `clear_active_slot`；alert 仅写 state `last_alert`（Telegram 已有凭据才发，发送失败吞掉异常）。
- [ ] **Step 4: 全量测试通过。**
- [ ] **Step 5: 提交**：`git commit -m "feat: add failover orchestration with drain, cooldowns and boot recovery"`

### Task 6: 状态暴露与配置（state.json / gateway_status / env profile）

**Files:**
- Modify: `vpngate_manager.py`（`get_state` 571、`/api/gateway_status` 5997–6107、`check_proxy_health` 5646–5772）、`config/aimilivpn-upstream.env`
- Test: `tests/test_dual_slot_status.py`

**Interfaces:**
- Produces:
  - state.json 新增键：`active_slot`、`slots`（A/B 各含 node_id/device/exit_ip/verified_at/health）、`draining_slot`、`switch_cooldown_until`、`rotation_paused`、`last_switch_at`、`last_failover_reason`
  - `/api/gateway_status` 响应含上述字段；`check_proxy_health` 的 tun0 存在性检查改为读活动槽设备（5682–5687 行）
  - `config/aimilivpn-upstream.env` 追加：`DRAIN_SECONDS=60`、`SWITCH_COOLDOWN_SECONDS=600`、`NODE_FAILURE_COOLDOWN_SECONDS=21600`、`VPNGATE_PROBE_FAIL_THRESHOLD=2`

- [ ] **Step 1: 写失败测试**：`get_state` 含新键默认值；无活动槽时 `check_proxy_health` 返回错误且不触碰 7928；env 缺省时默认值正确。
- [ ] **Step 2–5:** 同前节奏（失败→实现→全量绿→提交 `feat: expose dual-slot status in state and gateway API`）。

### Task 7: 本地总验收

- [ ] **Step 1:** `python3 -m unittest discover -s tests -v` 全绿；`python3 -m compileall .` 无错；`git diff --check` 通过；`bash -n` 检查改动过的 shell（若有）。
- [ ] **Step 2:** 故障矩阵 7 行、启动恢复、排空、冷却在测试列表中逐条点名核对存在且通过。
- [ ] **Step 3:** 更新 `.ai/HANDOFF.md`（已完成证据 + 下一步指向 Task 8）。

### Task 8: 云途备份与部署

**Files:**
- Modify（远端）: `/opt/aimilivpn/`、`/etc/default/aimilivpn`

- [ ] **Step 1: 只读复核现场**：SSH `root@<YUNTU_VPS_IP>`（公钥），确认 `systemctl is-active aimilivpn sing-box nginx hy2-vpngate` 全 active、无 failed unit、`tun0` 存在、7928 正常；与基线不符则停止报告。
- [ ] **Step 2: 回滚备份**：创建 `/root/aimili-backup-$(date +%Y%m%d-%H%M%S)`，含 `/opt/aimilivpn` 全量、`/etc/default/aimilivpn`、systemd unit、当前 `ss -tlnp`/`ip rule`/`ip route` 证据。
- [ ] **Step 3: 部署代码**：rsync 本仓库 `*.py`、`scripts/`、`config/` 到 `/opt/aimilivpn/`（保留远端 `vpngate_data/` 不动）；向 `/etc/default/aimilivpn` 追加 Task 6 的 4 个新 env（保留全部现有行）。
- [ ] **Step 4: 重启并观察**：`systemctl restart aimilivpn`；预期启动序列先 fail-closed（7928 拒绝），复验原活动节点后写指针恢复；`journalctl -u aimilivpn --since -5m` 无 traceback；既有服务回归：sing-box、nginx、hy2-vpngate、yuntu-argo、cf-dynamic-sub、xhttp-cdn 全 active。
- [ ] **Step 5: 更新 HANDOFF**。

### Task 9: 云途双隧道与故障注入验收

- [ ] **Step 1: 双隧道**：触发建立 standby（调用既有补池路径或 UI 立即测试），确认 `tun0`、`tun1` 并存、各自 `probe_tunnel_egress` 实测出口 IP 不同且都不等于云途默认公网；standby 不承载 7928 新流量（指针只指向 active 槽）。
- [ ] **Step 2: 切换注入**：杀掉 active 槽 OpenVPN 进程；60 秒内 7928 恢复且出口 = 原 standby；旧槽排空≤60 秒；15 分钟内自动补出新 standby。客户端侧（经现有 HY2 sidecar 或本机 socks5h://127.0.0.1:7928 curl）验证出口切换、入口不变。
- [ ] **Step 3: fail-closed 注入**：两槽隧道均杀 → 7928、经 7928 的一切请求失败；云途默认公网直连正常；连续探测 5 分钟无默认公网泄漏；随后恢复并确认自动回到双槽。
- [ ] **Step 4: 重启恢复**：`systemctl restart aimilivpn` 与必要时整机 reboot（先确认无其他风险），验证先关后验、不信任旧状态。
- [ ] **Step 5: 清理与记录**：测试进程/临时文件清理；结果写入 `.ai/HANDOFF.md`；下一步指向阶段二计划（VLESS/Trojan sidecar）。

---

## 自检记录（写计划时完成）

- **规格覆盖**：v2 设计 §5.1/§6/§7/§8.2/§8.3/§8.4/§10（安全边界）→ Task 1–7；§10.2 中不依赖新 sidecar 的行（1、3、4、7、9 与 2 的出口一致性部分）→ Task 8–9。依赖 VLESS/Trojan sidecar 的验收行（2 的入口部分、5、6、8、10 的 8787 项）明确划入阶段二。§8.1 前端探针的 sing-box 握手实现亦属阶段二，Task 4 只落接口与占位。
- **类型一致性**：`read_active_slot/write_active_slot/clear_active_slot`、`resolve_active_device`、`connect_node(slot=)`、`stop_slot_openvpn`、`verify_slot_egress`、`probe_vpngate_slot/probe_frontend/classify_failure`、`promote_slot_to_active/failover_to_standby/run_failure_attribution` 在 Task 1–6 间签名一致。
- **占位符扫描**：无 TBD/TODO；"前端探针握手"与"阶段二"为显式范围划分而非占位。
