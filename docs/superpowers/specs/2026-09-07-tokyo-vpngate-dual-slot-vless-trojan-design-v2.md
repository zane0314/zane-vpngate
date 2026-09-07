# 东京 VPNGate 双槽住宅出口与 VLESS/Trojan 入口设计（v2）

> 本文为重写版本，替代 `2026-09-07-tokyo-vpngate-dual-slot-vless-trojan-design.md`。
> 目标架构不变，结构与取舍重新组织，验收标准由本文重新定义（见第 10 节）。

## 1. 一句话目标

东京 VPS 对外只发布**一个**经过实测验证的日本住宅 IP 出口，通过 VLESS Reality 与 Trojan TLS 两条独立链接提供；住宅出口失效时秒级切换到热备，没有任何合格出口时彻底关闭代理，永不回落 VPS 默认公网。

```text
VPN Gate 官方 CSV
  → 预筛（官网上报数据）
  → 实测验证（东京真实建隧道）
  → 双槽 OpenVPN 隧道（active + standby，tun0/tun1）
  → 本地 SOCKS5 127.0.0.1:7928（动态绑定活动 TUN，fail-closed）
  → 3x-ui / Xray
  → VLESS Reality Vision TCP ＋ Trojan TLS TCP（两条独立分享链接）
```

## 2. 设计原则（全文的取舍依据）

1. **Fail-closed 优先于可用性**：任何不确定状态下，宁可拒绝连接也不走默认路由。
2. **实测优先于上报**：VPN Gate 官方数据只用于预筛排序，能否上岗完全由东京实测决定。
3. **单出口语义**：客户端永远只看到"一个日本住宅出口"，不暴露候选池、槽位、切换过程。
4. **最小侵入 3x-ui**：入站、密钥、证书、分享链接全部走 3x-ui 原生管线，不建平行系统。
5. **状态机可测试**：双槽、切换、故障归因全部是可单测的纯逻辑，网络操作收敛到薄执行层。

## 3. 已确认约束（沿用，不变）

- 客户端地址统一 `<PUBLIC_HOSTNAME>`，分享链接不得出现 VPS IP。
- VLESS：原生 `vless://`，Reality + Vision + TCP，SNI/dest 使用 `<REALITY_HANDSHAKE_HOST>:443`。
- Trojan：原生 `trojan://`，TLS + TCP，SNI 为 `<PUBLIC_HOSTNAME>`。
- 两条链接相互独立，不做合并订阅。
- 只验收标准链接语法和通用 Xray/sing-box 握手，不验收具体客户端（FlClash、v2rayN 等）兼容性。
- UDP/QUIC 一律显式拒绝；SOCKS5 不实现 UDP ASSOCIATE。
- 新链路验收后，旧 WS/XHTTP/Codex Trojan/HY2/ARGO 等住宅入口**先禁用不删除**，保留回滚材料；直出节点不动。
- 管理页最终只监听回环，公网 8787 关闭；通过 SSH 隧道访问。
- 本轮不做整机切换云途、不做 DNS 跨机容灾。

## 4. 明确不做

- 不安装完整 fanout，不为候选节点建常驻隧道，不做多出口负载均衡。
- 不做云途接管、DNS 自动切换、Cloudflare 跨机容灾。
- 不做客户端协议转换层。
- 第三方评分 API 失败时，不因此断开当前健康出口。

## 5. 数据模型

### 5.1 数据来源

仅使用 VPN Gate 官方 CSV 接口 `https://www.vpngate.net/api/iphone/`，不抓 HTML。
保留字段：`HostName, IP, Score, Ping, Speed, CountryLong, CountryShort, NumVpnSessions, Uptime, TotalUsers, TotalTraffic, LogType, Operator, Message, OpenVPN_ConfigData_Base64`。

### 5.2 两个命名空间（硬约定）

| 前缀 | 含义 | 用途 |
| --- | --- | --- |
| `reported_*` | VPN Gate 官方上报值 | 仅预筛排序与 UI 展示，必须标注"官网上报" |
| `verified_*` | 东京实测值（出口 IP、国家、ASN、类型、风险、延迟、吞吐、成功次数、测试时间） | 唯一上岗依据 |

现有 `score/ping/speed` 字段可保留作为 `reported_*` 原始值，UI/API 不得把它们显示成实测值。

### 5.3 节点状态机

```text
candidate → testing → observation → trusted（可作为 standby/active）
                ↘ rejected（含冷却 6h 的失败记录）
```

状态与失败计数全部原子写入（tmp + fsync + rename）；状态文件**不得**保存私钥、密码、完整分享链接或 OpenVPN 内嵌凭据。

## 6. 筛选与评分

### 6.1 预筛（仅官方数据）

1. `CountryShort=JP`，配置可 base64 解码且 OpenVPN 目标有效。
2. 按官方 `Score` 主排序，`Ping/Speed/Uptime/NumVpnSessions` 次级。
3. 每轮最多留 20 个候选，实测前 6 个，全程并发上限 2。
4. 官方接口拉取失败：保留当前健康主备，不清池、不切换。

### 6.2 实测硬门槛（一票否决，全部满足才可晋级）

- OpenVPN 进程稳定、专属 TUN 存在。
- TCP HTTPS 探测成功。
- 实测出口 IPv4 ≠ 东京 VPS 默认公网 IP。
- 实测国家 = 日本。
- 实测类型 = residential。
- 无 hosting/datacenter、Tor、bogon 等硬风险。
- 经该隧道的 UDP/QUIC 与 SOCKS5 UDP ASSOCIATE 均无法形成公网出口。

软标记（VPN/anonymous 等）只降分，不否决。

### 6.3 评分与晋级

- 综合分 = IP 质量 60% + 稳定性 25% + 东京实测网络表现 15%。
- 上岗线 `TRUST_MIN_IP_SCORE = 65`（原 90 与加权模型不适配），硬门槛与"至少连续 2 次实测成功"不变。
- 择优切换：质量差 > 5 分时质量优先；差 ≤ 5 分时，候选吞吐需高 ≥ 30% 且连续 2 轮胜出才允许切换。
- active 与 standby 实测出口 IP 必须不同；优先不同 ASN，无第二 ASN 时允许同 ASN 但 UI 标注"故障域未分散"。

## 7. 双槽运行模型

### 7.1 槽位

固定两槽：`A=tun0`、`B=tun1`，角色在 active/standby 间互换。每槽独立记录：OpenVPN 进程、节点 ID、设备、实测出口 IP/ASN、最近验证时间、健康状态。任意时刻至多一个 active。

### 7.2 稳定本地入口 7928

- 3x-ui 的 SOCKS outbound 永远指向 `127.0.0.1:7928`，不感知切换。
- `proxy_server.py` 在**每条新连接 accept 时**原子读取活动槽指针，该连接的 DNS 与 TCP socket 绑定同一 TUN 设备；不再硬编码 tun0。
- 已建立的连接继续走建连时的设备；旧槽排空，最长 60 秒后才允许重建为新 standby。
- 活动指针写入必须 tmp + fsync + 原子替换；指针为空、设备未知或槽位未通过健康验证时，7928 直接拒绝连接。
- 不新增 17928/17929 等内部 SOCKS 服务。

### 7.3 启动恢复（先关后验）

1. 读上次主备状态但不信任。
2. 优先重建上次 active/standby 对应隧道。
3. 分别重跑全部实测硬门槛。
4. active 验证通过才开放 7928；否则提升已验证 standby。
5. 两槽均失败：保持关闭，按受控节奏补池。

## 8. 健康检查与故障归因

### 8.1 三类独立探针

| 探针 | 路径 | 注意 |
| --- | --- | --- |
| VPNGate | 直接绑定 active TUN，查进程/设备/TCP HTTPS/实测出口 IP 与国家 | 不得经过 7928，避免自我依赖 |
| VLESS | 通用 Xray/sing-box 客户端完成 Reality/VLESS 握手 | 目标是 VPS 上仅回环可达的健康端点，不经过 7928 |
| Trojan | 通用客户端完成 Trojan/TLS 握手 | 同上 |

3x-ui 路由中，仅允许两个新 inbound tag 对"回环地址 + 固定健康端口"走本地直连的精确例外，且必须排在 SOCKS 路由之前；其余 TCP 全部进 7928，UDP 全部进 blackhole。例外规则不得放行任何公网直出。

### 8.2 判定窗口

- 单次超时只记瞬时异常。
- VPNGate 探针连续 2 次失败（间隔 5 秒）才进入故障归因。
- 前端探针归因时各测 2 次，连续失败才记为该前端失败。
- 切换成功后 10 分钟冷却，冷却期内只响应硬故障，不做性能择优。
- 同一节点失败后冷却 6 小时，人工解锁除外。

### 8.3 故障矩阵

| VPNGate | VLESS | Trojan | 归因 | 动作 | 节点失败历史 |
| --- | --- | --- | --- | --- | --- |
| 正常 | 失败 | 正常 | VLESS 单协议故障 | 不切换；告警 | 不写入 |
| 正常 | 正常 | 失败 | Trojan 单协议故障 | 不切换；告警 | 不写入 |
| 正常 | 失败 | 失败 | Xray/入口故障 | 保持主备，暂停性能切换；告警 | 不写入 |
| 失败 | 正常 | 正常 | VPNGate 出口故障 | 提升热备，随后补热备 | 计 active 失败 |
| 失败 | 失败 | 失败 | 主机/Xray/网络故障 | 暂停轮换，等前端恢复 | 不写入 |
| 失败 | 失败 | 正常 | VPNGate 故障 + VLESS 故障 | 切 VPNGate；VLESS 单独告警 | 只计 active VPNGate 失败 |
| 失败 | 正常 | 失败 | VPNGate 故障 + Trojan 故障 | 切 VPNGate；Trojan 单独告警 | 只计 active VPNGate 失败 |

默认规则：至少一个独立前端健康即证明入口栈未整体失效，避免单协议故障阻塞真正的 VPNGate 容灾。

### 8.4 切换顺序

1. active 连续失败 → 跑故障矩阵。
2. 允许切换时，对 standby 做一次快速复验。
3. 复验通过 → 原子更新活动槽指针。
4. 原 active 进入排空（≤60s），记录失败并按冷却处理。
5. 原槽清理后从合格池选下一候选，建隧道并过全部硬门槛，成为新 standby。
6. standby 复验失败：不动 active 指针，测下一候选。
7. 无可用候选：清空活动指针，fail-closed，绝不回落默认公网。

## 9. 3x-ui/Xray 集成

实施前先备份：3x-ui 数据库、Xray 配置、证书路径、监听端口、现有分享链接，并梳理现有入站/出站/路由生成流程。

新增内容全部通过 3x-ui 原生管线完成：

- 1 个 VLESS Reality Vision TCP inbound（新端口、新 UUID、明确 tag）。
- 1 个 Trojan TLS TCP inbound（新端口、新密码、明确 tag）。
- 1 个复用 SOCKS outbound → `127.0.0.1:7928`，仅 TCP。
- 两个新 inbound tag 的 TCP 路由 → 该 SOCKS outbound。
- 两个新 inbound tag 的 UDP 路由 → blackhole（优先级高于 TCP 路由）。
- 健康探针回环例外规则排在最前。

端口实施时从当前未占用 TCP 高位端口现场选择，不在文档中预定。不得手工建立与 3x-ui 数据库脱节的链接或订阅。

## 10. 验收标准（本版自定义）

### 10.1 代码与静态验收

1. 现有全部测试通过；新增状态机单测覆盖：双槽切换、排空、活动指针原子写、故障矩阵 7 行、失败冷却、启动恢复 5 步。
2. `python -m compileall`、shell 语法检查、`git diff --check` 全部通过。
3. Xray 配置经 `xray -test`（或等效）校验通过；3x-ui 数据库无平行配置实现。
4. 两条分享链接可被通用解析器解析，`address` 均为 `<PUBLIC_HOSTNAME>`，VLESS 含 Reality/Vision 参数，Trojan 含 TLS SNI。

### 10.2 功能验收（东京实测）

| # | 场景 | 通过标准 |
| --- | --- | --- |
| 1 | 双隧道并存 | tun0/tun1 各通，实测出口 IP 不同、均为日本住宅，且 ≠ VPS 默认公网 |
| 2 | 单出口语义 | VLESS、Trojan 各自出口 IP 相同且 = active 槽实测 IP |
| 3 | 故障切换 | 杀掉 active 的 OpenVPN 进程，60 秒内 7928 恢复到 standby 出口；客户端配置无需变动 |
| 4 | 自动补备 | 切换完成后 15 分钟内自动建立新 standby 并通过硬门槛 |
| 5 | 单前端故障 | 仅停 VLESS inbound：VPNGate 不切换、节点失败计数不增、有告警 |
| 6 | 全面失败 | 停 active 隧道 + 双前端：暂停轮换、不计节点失败；恢复前端后自动恢复判断 |
| 7 | 双槽尽废 | 两槽隧道均杀：7928、VLESS、Trojan 全部拒绝/失败；`curl --interface` 之外的主机运维网络正常；代理侧无任何默认公网泄漏（连续探测 5 分钟） |
| 8 | UDP 封堵 | VLESS/Trojan 的 UDP、QUIC、SOCKS5 UDP ASSOCIATE 全部失败，且不出现默认公网出口 |
| 9 | 重启恢复 | 依次重启 AimiliVPN、Xray、整机：均为先关闭、复验通过后才开放 7928；旧状态不被直接信任 |
| 10 | 管理面收紧 | 公网扫不到 8787；`127.0.0.1:8787` 经 SSH 隧道可访问；直出节点、nginx、3x-ui 无关服务无回归 |

### 10.3 验收红线（任一不满足即不验收）

- 任何场景下代理流量走东京默认公网出口。
- 分享链接、日志、状态文件、诊断导出中出现 IP 形式的服务端地址、UUID、Trojan 密码、Reality 私钥、Telegram token 或 OpenVPN 凭据。
- 以服务进程 `active (running)` 代替上述端到端验证。

## 11. 运行、通知与安全边界

- 总并发 ≤ 2（双隧道 + 候选测试 + 测速合计）；禁止无上限创建 TUN/OpenVPN 进程。
- 所有公网 socket 显式绑定已验证的活动 TUN。
- 日志与诊断导出脱敏：仅保留节点 ID 摘要、时间、分数、非敏感网络属性、错误类别。
- 第三方信誉 API 失败：保留当前健康 active，不把"无法重新评分"当节点失效。
- Telegram 仅已有凭据时启用，只通知：切换成功、全部出口关闭、轮换暂停、恢复、热备长期缺失；发送失败不影响状态机。
- 管理页仅显示：主备节点/设备/实测出口 IP/ASN/评分/数据新鲜度、三类健康状态、fail-closed/暂停/冷却标记、最近切换时间与最小故障原因。保留操作：立即测试、手动切换、临时锁定/解锁、黑名单、导出脱敏诊断。

## 12. 实施顺序

1. 核对 Git 状态与东京现场基线；保留用户已删除的旧文档，不覆盖无关变更。
2. 先写最小测试：双槽状态、动态 TUN 绑定、故障矩阵、失败历史隔离。
3. 改造 AimiliVPN 双槽与 7928 动态设备选择，本地测试，不接生产入口。
4. 东京并行建立 standby，验证双隧道资源、出口与 fail-closed。
5. 3x-ui 原生流程创建两条新 TCP inbound 与路由。
6. 临时通用客户端跑握手、出口、切换、重启、故障矩阵测试（对应 10.2 全表）。
7. 全部通过后禁用旧住宅入口、保留回滚材料；复查直出服务无变化。
8. 管理页收紧回环监听；更新 VPS 主清单与 `.ai/HANDOFF.md`。
9. 未经维护者明确授权，不推 GitHub、不发 Release。

## 13. 回滚

- 保留升级前 `/opt/aimilivpn`、环境文件、运行数据、systemd、3x-ui 数据库、Xray 配置、监听与防火墙证据。
- AimiliVPN 回滚：恢复单 tun0 与原 7928 实现。
- 3x-ui 回滚：先停两条新 inbound，再恢复数据库/配置备份并验证原分享链接。
- 旧住宅入口在新链路稳定前只禁用不删除，可独立恢复。
- 回滚不改 `<PUBLIC_HOSTNAME>` DNS，不启用其他 VPS 接管。

## 14. 执行要求（Kimi）

1. 开始前读本设计与 `.ai/HANDOFF.md`；现场事实（Git 差异、东京服务、3x-ui 数据库、Xray 运行配置）优先于任何文档记录。
2. 保留四个用户已删除的旧云途设计/计划文档，不恢复、不覆盖。
3. 测试驱动实施；每完成一个可验证工作单元立即更新 `.ai/HANDOFF.md`，负责人记 `kimi`。
4. 端口、证书路径、tag 等现网配置与设计不一致时取最小兼容改动；可能影响现有直出服务时停止并报告维护者。
5. Git、交接文件、聊天输出中均不得写入任何凭据。
