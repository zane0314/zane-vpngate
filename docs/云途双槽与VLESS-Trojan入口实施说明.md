# 云途双槽住宅出口与 VLESS/Trojan 入口实施说明

> 2026-09-07 由 kimi 实施完成。执行依据：`docs/superpowers/specs/2026-09-07-tokyo-vpngate-dual-slot-vless-trojan-design-v2.md`；实施计划：`docs/superpowers/plans/2026-09-07-yuntu-dual-slot-core-implementation.md`（阶段一）与 `docs/superpowers/plans/2026-09-07-yuntu-vless-trojan-entry-implementation.md`（阶段二）。本文档不含任何凭据。

## 1. 架构总览

```text
东京（local-authority，VPNGate 官方节点与日本住宅硬门槛权威）
  → 受限 SSH 池快照同步
云途（upstream-consumer）：
  schema v2 快照 → 本地单连接吞吐复核（OpenVPN 握手/TUN/出口 IP，不重评分）
  → 双槽 OpenVPN 隧道：槽 A=tun0、槽 B=tun1（active + 热备，角色可互换）
  → 127.0.0.1:7928（SOCKS5，每条新连接原子读活动槽指针并绑定对应 TUN，无指针即拒绝）
  → 入口（全部仅 TCP 出站进 7928，UDP 一律拒绝）：
     ├─ vt-vpngate sidecar：VLESS Reality Vision TCP :30441 ＋ Trojan TLS TCP :30552
     └─ hy2-vpngate sidecar：Hysteria2 UDP :32166（既有，不变）
```

核心语义：对外只有一个当前最优日本住宅出口；active 失效自动提升已验证热备；无合格出口时 fail-closed，永不回落云途默认公网。

## 2. 代码改动（本地仓库提交 e4cd246 → 0c023d0，共 10 个）

| 提交 | 内容 |
| --- | --- |
| e4cd246 | `slot_state.py`：槽位定义（SLOT_DEVICES/SLOT_TABLES）、活动槽指针与 slot_states.json 的 tmp+fsync+rename 原子写 |
| a276dbc | `proxy_server.py`：7928 按连接动态绑定活动槽设备（DNS 与 TCP 同设备）；无指针/设备缺失在绑定前抛 3004（fail-closed 前置） |
| bdff2a0 | `vpngate_manager.py` 槽位化：SLOT_RUNTIME、策略路由表拆分（tun0→100/tun1→101）、`connect_node(slot=)`、`stop_slot_openvpn`、`verify_slot_egress` |
| 5e7303c | `health_probes.py`：VPNGate 槽探针（绑设备、不经 7928）、前端探针接口、7 行故障矩阵纯函数 `classify_failure` |
| ddd8e84 | 切换编排：`promote_slot_to_active`（唯一写指针入口）、`failover_to_standby`、排空（60s）、切换冷却（10min）、节点失败冷却（6h）、启动先关后验恢复 |
| 2e12e90 | 状态暴露：state.json/gateway_status 新增 active_slot/slots/draining_slot/cooldown/rotation_paused 等；check_proxy_health 改读活动槽 |
| f9d733c | 修复①：首次启动接管死锁（connect 成功后经 `_adopt_slot_after_connect` 接管指针） |
| 9a00c31 | 修复②：standby 槽自动建隧道（`maintain_standby_slot`，30s 周期，同槽连错 3 次停 10 分钟）；附带修复 standby 建备失败误拆 active 的连带缺陷 |
| b55b224 | 修复③：隧道失效统一走 `handle_active_tunnel_loss`→故障归因（attribution_lock 防竞态），消除旧路径抢跑；死进程判定改 `poll()` |
| 0c023d0 | 修复④：前端看门狗 `run_frontend_watchdog`（60s 节流、连续 2 次失败产 last_alert、纯观测不动状态机） |

本地验证：168/168 unittest 全绿、compileall 通过、`git diff --check` 通过（每次提交前均跑全量）。

### 2.1 池子优化实现（2026-09-07）

- 东京只导出 `hard_gate=passed` 的日本住宅、无风险节点；硬门槛记录 `hard_gate_checked_at`、`hard_gate_expires_at`，快照 schema 为 v2，最多 300 条，不携带吞吐字段。
- 可信池硬门槛保持 IP 质量分数不低于 90，但完整探测成功 1 次即可入池；超出可信池/观察池限额的硬门槛通过节点进入 `candidate`，不再错误标记为 cooldown。
- 云途候选也可连接，但只接受东京快照中的硬门槛通过节点；每 5 分钟最多测速 10 个未测或已过期节点，单连接下载探测 25 MB、1 次样本。
- 云途本地保存 `local_download_mbps`、`local_ttfb_ms`、测量时间、探测字节数和探测路径；可信池优先按本地吞吐排序，观察池作为无可信节点时的回退，候选只有超过当前最佳 30% 才能挑战。
- 两端只保留当前/上一份快照，并按节点 ID、配置 hash 和保留时间清理本地测速状态与受管 `.ovpn` 文件；东京与云途每小时刷新一次快照。

池优化实现的本地回归结果：184/184 unittest、compileall、shell 语法检查和 `git diff --check` 均通过。

池优化最终远端验收：东京与云途均部署本地验证提交 `87995f4`；东京 sequence=83、10 条节点全部硬门槛通过且快照不含吞吐；云途同步同一 sequence，10 条节点完成本地测速，当前 active/standby=1/9，测速队列为 0，受管配置数量与当前节点数一致。云途回环代理出口请求成功且与直连公网出口不同；AimiliVPN、入口 sidecar、nginx、同步/导出 timer 均 active/enabled，无 failed unit。东京当前可信池为 0 时按观察池继续提供容灾，符合降级策略。

## 3. 云途部署清单（`<YUNTU_VPS_IP>`）

新增服务（均 enabled）：

| 服务 | 内容 | 监听 |
| --- | --- | --- |
| vt-vpngate | sing-box VLESS Reality + Trojan TLS，路由：回环健康例外 direct → UDP reject → 其余 TCP 进 7928 | TCP 30441 / 30552 |
| vt-prober | sing-box 探针客户端，两个回环 mixed inbound 分别经 VLESS/Trojan 出站 | 127.0.0.1:18081 / 18082 |
| aimili-health | 30 行 python 健康端点（探针目标） | 127.0.0.1:18978 |

关键文件：

- `/etc/vt-vpngate/config.json`、`/etc/vt-vpngate/credential`（root-only 0600，Reality 私钥/UUID/Trojan 密码）
- `/etc/vt-prober/config.json`（0600）
- `/etc/aimilivpn/frontend-probes.json`（前端探针配置，由 `FRONTEND_PROBE_CONFIG` 指向）
- `/etc/default/aimilivpn` 新增：`DRAIN_SECONDS=60`、`SWITCH_COOLDOWN_SECONDS=600`、`NODE_FAILURE_COOLDOWN_SECONDS=21600`、`VPNGATE_PROBE_FAIL_THRESHOLD=2`、`STANDBY_SLOT_MAX_FAILURES=3`、`STANDBY_SLOT_PAUSE_SECONDS=600`、`FRONTEND_PROBE_CONFIG=/etc/aimilivpn/frontend-probes.json`、`UI_HOST=127.0.0.1`
- `vpngate_data/ui_auth.json` 的 `host` 已改 `127.0.0.1`（注意：该字段覆盖 env UI_HOST）
- 运行态文件：`vpngate_data/active_slot.json`（活动指针）、`slot_states.json`（槽位状态）
- 池运行态：`vpngate_data/upstream-snapshot.json`（云途当前快照）、`local-probe-state.json`（仅云途本地吞吐与冷却状态）；东京导出文件位于 `/var/lib/aimilivpn-export/pool-snapshot.json`。

收紧：管理页 8787 由 `*:8787` 改为仅 `127.0.0.1:8787`；公网访问已不可达，管理方式：`ssh -L 18787:127.0.0.1:8787 root@<YUNTU_VPS_IP>` 后访问 `http://127.0.0.1:18787/<SECRET_PATH>`。

未改动：v2ray-agent 主 sing-box、hy2-vpngate、nginx、yuntu-argo、cf-dynamic-sub、xhttp-cdn、codex-trojan-ws、anyvps-agent。

## 4. 实测验收结论（2026-09-07，全部云途实测）

| 验收项 | 结果 |
| --- | --- |
| 双隧道并存、出口互异且 ≠ 默认公网 | 通过 |
| 7928/VLESS/Trojan 出口 = active 槽出口 | 通过（外部握手从 BitsFlow 独立验证） |
| kill active → 自动提升热备 | 通过，10 秒完成（限 60 秒），切换记录/冷却齐全 |
| 自动补新热备 | 通过，2.5 分钟（限 15 分钟） |
| 双槽尽废 fail-closed | 通过：5.5 分钟 22 次探测零默认公网泄漏；可用池恢复后 8 秒自愈 |
| 单前端故障（拦 VLESS 端口） | 通过：不切换、不计节点失败、102 秒产 last_alert，恢复后自动清除 |
| 隧道+双前端同挂（host-stack） | 通过：rotation_paused、不计失败；恢复后自动续判并完成切换 |
| UDP/QUIC/UDP ASSOCIATE | 全部拒绝，无默认公网出口 |
| systemctl 重启 / 整机 reboot | 均先关后验恢复；reboot 后 SSH 45 秒恢复，12 项服务自动 active |
| 8787 公网 | 已关闭；SSH 隧道管理验证 200 |
| 既有服务回归 | 9 项既有服务全程无回归、无 failed unit |

## 5. 运维速查

- 看状态：`cat /opt/aimilivpn/vpngate_data/active_slot.json slot_states.json`；state.json 里 `active_slot/slots/draining_slot/rotation_paused/switch_cooldown_until/last_alert/frontend_health`。
- 看日志：`journalctl -u aimilivpn -f`，关键模块标签 `[Slot]`（接管）、`Attribution`（故障归因）、`Standby`（热备补建）。
- 看池：东京检查 schema v2、`source_instance=tokyo`、节点数不超过 300 且所有节点 `hard_gate=passed`；云途检查本地测速字段、测速队列不超过 10 和 `last_upstream_probe_at`。
- 切换节奏：kill active 后预期 ≤60 秒切换；切换后 10 分钟内不做性能择优；失败节点 6 小时内不再被选中。
- 池更新节奏：每小时同步快照；云途每 5 分钟最多测 10 个节点。东京不上传吞吐结论，云途不重做 IP 声誉分类。
- 回滚点：云途 `/root/aimili-backup-20260907-180455`、`/root/aimili-backup-20260907-183851`（恢复 /opt/aimilivpn 与 /etc/default/aimilivpn 后 `systemctl restart aimilivpn`；新 sidecar 停用即可：`systemctl disable --now vt-vpngate vt-prober aimili-health`）。

## 6. 遗留事项

1. 池优化代码与两端真机验收已完成；后续只需观察 hourly 快照、测速队列和 active/standby 状态。
2. 公开仓库提交前必须再次执行凭据和部署信息扫描；本地操作指令保存在未跟踪的 `.ai/` 中。
3. `FRONTEND_WATCHDOG_STATE` 为内存态，aimilivpn 重启后前端失败计数清零重新积累（可接受的观测语义）。
