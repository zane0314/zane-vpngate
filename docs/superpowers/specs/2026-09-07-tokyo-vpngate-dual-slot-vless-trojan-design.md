# 东京 VPNGate 双槽出口与 VLESS/Trojan 入口设计

## 1. 目标

在东京 VPS 上把现有 AimiliVPN 改造成一个严格失败关闭的日本住宅出口控制器：

```text
VPN Gate 官方 CSV
  -> AimiliVPN 预筛、真实出口验证和评分
  -> 两条独立 OpenVPN 隧道（单一生产出口 + 一个热备）
  -> 稳定本地代理 127.0.0.1:7928
  -> 现有 3x-ui/Xray
  -> VLESS Reality Vision TCP / Trojan TLS TCP
```

对外始终只发布一个当前最优住宅出口，不向客户端暴露候选列表或热备槽位。活动 VPNGate 节点失效时自动切换到已验证热备；没有合格出口时严格失败，不回落东京 VPS 默认公网。

本轮不实现东京整机切换云途，不修改 DNS 做跨 VPS 容灾。

## 2. 已确认约束

- 客户端地址统一使用 `<PUBLIC_HOSTNAME>`，分享链接不得写 VPS IP。
- VLESS：原生 `vless://`，Reality + Vision + TCP，SNI/目标使用 `<REALITY_HANDSHAKE_HOST>:443`。
- Trojan：原生 `trojan://`，TLS + TCP，SNI 使用 `<PUBLIC_HOSTNAME>`。
- 两条链接相互独立，不建立合并订阅。
- 不验收 FlClash、v2rayN、NekoBox、Surge 等具体客户端兼容性，只验收标准链接语法、通用 Xray/sing-box 握手及服务端行为。
- VLESS/Trojan 的 UDP、QUIC 请求必须显式拒绝；AimiliVPN 不实现 SOCKS5 UDP ASSOCIATE。
- VPNGate 仅作为服务端出站。3x-ui 的入站、Reality 密钥、TLS 证书、分享链接和订阅继续使用原生管线，不新增平行配置或导出系统。
- 新链路验收后，旧 WS/XHTTP/Codex Trojan/HY2/ARGO 等 VPNGate 住宅入口退出正式发布；先禁用并保留回滚材料，不立即删除。直出节点及其服务不变。
- 东京管理页不得继续以明文 HTTP 暴露公网；默认改为回环监听，通过 SSH 隧道管理。除非已有安全反代可直接复用，否则不新增管理域名。

## 3. 不做的内容

- 不复制或安装完整 `fanout`。
- 不为每个候选节点建立常驻隧道。
- 不提供多出口负载均衡。
- 不做云途接管、DNS 自动切换或 Cloudflare 跨机容灾。
- 不增加客户端协议转换和专用兼容层。
- 不因第三方评分 API 暂时失败而断开当前健康出口。

## 4. 节点数据来源

直接读取 VPN Gate 官方接口：

```text
https://www.vpngate.net/api/iphone/
```

不得抓取网页 HTML。CSV 与网站表格同源，至少保留以下官方上报字段：

- `HostName`
- `IP`
- `Score`
- `Ping`
- `Speed`
- `CountryLong` / `CountryShort`
- `NumVpnSessions`
- `Uptime`
- `TotalUsers`
- `TotalTraffic`
- `LogType`
- `Operator`
- `Message`
- `OpenVPN_ConfigData_Base64`

数据必须区分两个命名空间：

- `reported_*`：VPN Gate 官方上报值，只用于预筛和展示。
- `verified_*`：东京实际建隧道后测得的出口 IP、国家、ASN、IP 类型、风险、延迟、吞吐、成功次数和测试时间，决定是否可用。

为控制改动量，可保留现有 `score/ping/speed` 字段作为官方原始字段，但 UI 和 API 必须明确标注“官网上报”；不得把它们显示成东京实测值。

## 5. 筛选和评分

### 5.1 官方数据预筛

1. 仅接收 `CountryShort=JP`、配置可解码且 OpenVPN 目标有效的节点。
2. 官方 `Score` 为主要预筛指标，`Ping`、`Speed`、在线时长和会话数只作次级排序。
3. 每轮最多保留前 20 个候选，实际测试前 6 个，并发上限保持 2。
4. 官方接口失败时保留当前健康主备，不清空池、不触发切换。

### 5.2 东京实测硬门槛

候选只有同时满足以下条件才可进入 observation/trusted 或成为热备：

- OpenVPN 进程稳定、专属 TUN 存在。
- TCP HTTPS 探测成功。
- 实际出口 IPv4 与东京 VPS 默认公网 IP 不同。
- 实际出口国家为日本。
- 实际类型为 residential。
- 无 hosting/datacenter、Tor、bogon 等硬风险。
- SOCKS5 UDP ASSOCIATE 和协议内 UDP/QUIC 均不能形成公网出口。

VPN/anonymous 等软标记降低评分，但不单独否决节点；含硬风险的节点直接淘汰。

### 5.3 排名和晋级

- 延用现有综合评分：IP 质量 60%、稳定性 25%、东京实测网络表现 15%。
- `TRUST_MIN_IP_SCORE` 从不适配当前加权模型的 90 调整为 65，仍保留全部硬门槛和至少 2 次连续成功要求。
- IP 质量差超过 5 分时优先质量；差不超过 5 分时，候选吞吐至少高 30% 且连续胜出 2 轮才允许性能择优切换。
- 活动与热备的实际出口 IP 必须不同；ASN 不同为优先条件，在无第二 ASN 时允许同 ASN，但 UI 标记故障域未分散。

## 6. 双槽运行模型

### 6.1 槽位

只维护两个固定槽位：

| 槽位 | 设备 | 角色 |
| --- | --- | --- |
| A | `tun0` | active 或 standby |
| B | `tun1` | active 或 standby |

每个槽位独立保存 OpenVPN 进程、节点 ID、设备、实际出口 IP、ASN、最近验证时间和健康状态。任一时刻只能有一个 `active`，另一个为不承载新生产流量的 `standby`。

### 6.2 稳定本地入口

保留现有 `127.0.0.1:7928`，3x-ui/Xray 的 SOCKS outbound 地址不变。

`proxy_server.py` 不再硬编码 `tun0`，而是在接受每条新连接时原子读取当前活动设备，并让该连接的 DNS 和 TCP socket 始终绑定同一设备。切换只更新一个持久化的活动槽指针：

```text
active_slot=A -> 新连接绑定 tun0
active_slot=B -> 新连接绑定 tun1
```

不增加 `17928/17929` 等内部 SOCKS 服务。已经建立的 TCP 连接继续使用创建时选定的设备；旧槽进入排空状态，最长保留 60 秒后才允许重建为新热备。

活动指针必须采用临时文件 + `fsync` + 原子替换写入。任何空值、未知设备或槽位未通过健康验证时，`7928` 直接拒绝连接，不使用默认路由。

### 6.3 启动恢复

服务重启后先进入 fail-closed：

1. 读取上次主备状态，但不直接信任。
2. 优先重建上次 active 和 standby。
3. 分别重新验证实际出口和住宅硬门槛。
4. active 验证成功后才开放 `7928`；否则提升已验证 standby。
5. 两槽均失败时保持关闭，并按受控节奏补池。

状态文件不得保存私钥、密码、完整分享链接或 OpenVPN 内嵌凭据。

## 7. 健康检查和故障归因

### 7.1 三类独立探针

- VPNGate 探针：直接绑定 active TUN，检查 OpenVPN 进程、设备、TCP HTTPS、实际出口 IP/国家；不得通过 `7928` 形成自我依赖。
- VLESS 探针：使用通用 Xray/sing-box 客户端完成 Reality/VLESS 握手，目标为 VPS 上仅回环可达的健康响应端点，不经过 `7928`。
- Trojan 探针：使用通用 Xray/sing-box 客户端完成 Trojan/TLS 握手，目标同样为回环健康端点，不经过 `7928`。

3x-ui 路由只允许两个新 inbound tag 对“回环地址 + 固定健康端口”走本地直连，其余 TCP 全部进入 `127.0.0.1:7928`；UDP 全部进入 blackhole。该例外不得允许任何公网直出。

### 7.2 判定窗口

- 单次超时只记瞬时异常。
- 连续 2 次 VPNGate 探针失败才进入故障归因；两次间隔 5 秒。
- 前端探针在归因时各执行 2 次，只有连续失败才记为该前端失败。
- 切换成功后设置 10 分钟冷却；冷却期内只因硬故障切换，不做性能择优。
- 同一节点失败后进入 6 小时冷却；人工解锁除外。

### 7.3 故障矩阵

| VPNGate | VLESS | Trojan | 归因 | 动作 | 节点失败历史 |
| --- | --- | --- | --- | --- | --- |
| 正常 | 失败 | 正常 | VLESS 单协议故障 | 保持当前状态，不切换；告警 | 不写入 |
| 正常 | 正常 | 失败 | Trojan 单协议故障 | 保持当前状态，不切换；告警 | 不写入 |
| 正常 | 失败 | 失败 | Xray/主机入口故障 | 保持当前 VPNGate 主备，暂停性能切换；告警 | 不写入 |
| 失败 | 正常 | 正常 | VPNGate 出口故障 | 验证并提升热备，随后补充热备 | 计 active 失败 |
| 失败 | 失败 | 失败 | 东京主机/Xray/网络故障 | 暂停 VPNGate 轮换，等待前端恢复 | 不写入 |
| 失败 | 失败 | 正常 | VPNGate 故障 + VLESS 单协议故障 | 切换 VPNGate；VLESS 单独告警 | 只计 active VPNGate 失败 |
| 失败 | 正常 | 失败 | VPNGate 故障 + Trojan 单协议故障 | 切换 VPNGate；Trojan 单独告警 | 只计 active VPNGate 失败 |

最后两行采用“至少一个独立前端健康即可证明入口栈未整体失效”的默认规则，以避免单协议故障阻止真正的 VPNGate 容灾。

### 7.4 切换顺序

1. active 连续失败后执行故障矩阵。
2. 允许切换时，对 standby 做一次快速复验。
3. 复验成功则原子更新活动槽指针。
4. 原 active 进入排空，之后记录失败并按冷却规则处理。
5. 原槽清理后从合格池选择下一候选，建隧道并完成全部硬门槛验证，成为新 standby。
6. standby 复验失败时，不先切断 active 指针；测试下一候选。
7. 无可用候选时清空活动指针并 fail-closed，绝不回落默认公网。

## 8. 3x-ui/Xray 集成

Kimi 实施前必须先备份 3x-ui 数据库、Xray 配置、证书路径、监听和现有分享链接，并识别现有原生入站/出站/路由生成流程。

新增内容必须通过现有 3x-ui 管线完成：

- 一个 VLESS Reality Vision TCP inbound，使用新端口、新 UUID 和明确 inbound tag。
- 一个 Trojan TLS TCP inbound，使用新端口、新密码和明确 inbound tag。
- 一个复用的 SOCKS outbound 指向 `127.0.0.1:7928`，仅允许 TCP。
- 两个新 inbound tag 的 TCP 路由指向该 SOCKS outbound。
- 两个新 inbound tag 的 UDP 路由优先指向 blackhole。
- 两个健康探针目标的精确回环例外规则必须排在 SOCKS 路由之前。

不得手工建立与 3x-ui 数据库脱节的分享链接或订阅。端口在实施时从当前未占用 TCP 高位端口中选择，不在设计文档中预定。

## 9. 状态、操作和通知

管理状态页只增加必要信息：

- active/standby 节点、设备、实际出口 IP/ASN、评分和数据新鲜度。
- VPNGate、VLESS、Trojan 三类健康状态。
- 当前是否 fail-closed、暂停轮换或处于冷却。
- 最近一次切换时间和最小故障原因。

保留以下操作：立即测试、手动切换、临时锁定、解除锁定、加入/移出黑名单、导出脱敏诊断。

Telegram 仅在已经配置凭据时启用，只通知：成功切换、全部出口关闭、轮换因双前端失败而暂停、恢复、热备长期缺失。未配置或发送失败不得影响切换状态机。

## 10. 安全和资源边界

- 双隧道、候选测试和测速总并发仍为 2；禁止并发创建无上限 TUN/OpenVPN 进程。
- 所有公网流量 socket 必须显式绑定经验证的活动 TUN。
- 活动指针、节点状态和失败计数原子写入；写入失败时保持旧状态或 fail-closed。
- 日志不得包含 UUID、Trojan 密码、Reality 私钥、Telegram token、完整分享链接或 OpenVPN 凭据。
- 诊断导出仅保留节点 ID 摘要、时间、分数、非敏感网络属性和错误类别。
- 第三方信誉 API 失败时保留已验证且仍健康的 active，不把“无法重新评分”当作节点失效。
- 实施完成后将管理 UI 限制为 `127.0.0.1`；公网 `8787` 不再监听。

## 11. 实施顺序

1. 重新核对 Git 状态和东京现场基线，保留当前用户删除的文档，不覆盖无关变更。
2. 为双槽状态、动态 TUN 绑定、故障矩阵和失败历史隔离先写最小测试。
3. 改造 AimiliVPN 双槽和 `7928` 动态设备选择，先在本地测试，不接入生产入口。
4. 在东京并行建立 standby，验证双隧道资源、出口与严格失败关闭。
5. 通过 3x-ui 原生流程创建两条新 TCP inbound 和路由。
6. 使用临时通用客户端进行服务端握手、出口、切换、重启和故障矩阵测试。
7. 新链路全部通过后，禁用旧 VPNGate 住宅入口并保留回滚材料；复查直出服务无变化。
8. 收紧管理页监听，更新 VPS 主清单和 `.ai/HANDOFF.md`。
9. 未得到维护者明确授权前，不推送 GitHub、不发布 Release。

## 12. 验收标准

### 12.1 本地与静态验证

1. 全量现有测试通过，新状态机至少覆盖故障矩阵七行。
2. Python 编译、Shell 语法和 `git diff --check` 通过。
3. 3x-ui/Xray 配置检查通过，数据库和导出管线无平行实现。
4. 标准 VLESS/Trojan 分享链接语法可被通用解析器读取，地址均为 `<PUBLIC_HOSTNAME>`。

### 12.2 东京真实链路

1. `tun0`、`tun1` 各自对应不同实际出口 IP；优先不同 ASN。
2. active 与 standby 均满足日本住宅硬门槛，standby 不承载新生产流量。
3. VLESS 和 Trojan 均可完成 TCP 握手并取得与 active 一致的实际出口，且不等于东京 VPS 默认公网。
4. 注入 active VPNGate 故障后自动提升 standby，客户端地址和本地 `7928` 不变；随后自动补出新 standby。
5. 注入单一前端故障时保持 VPNGate 状态不变，节点失败计数不增加。
6. 注入 VPNGate 与双前端同时失败时暂停轮换，节点失败计数不增加；前端恢复后自动恢复判断。
7. 两槽均不可用时 VLESS、Trojan 和 `7928` 均失败，东京默认公网仍可用于主机运维，但代理流量无泄漏。
8. UDP/QUIC 和 SOCKS5 UDP ASSOCIATE 测试失败且未观察到默认公网出口。
9. 重启 AimiliVPN、Xray 和整机后，系统先关闭后复验恢复，不因旧状态直接开放代理。
10. 公网不再监听管理端口 `8787`；原直出、nginx、3x-ui 及无关服务无回归。

### 12.3 完成门槛

必须执行仓库适用的 3x-ui 管线保护、订阅导出验证、运行材料验证和正式部署完成门禁。任何一项无法验证时只能报告未完成，不得以服务进程 active 代替端到端验收。

## 13. 回滚

- 保留升级前 `/opt/aimilivpn`、环境文件、运行数据、systemd、3x-ui 数据库、Xray 配置、监听和防火墙证据。
- AimiliVPN 回滚恢复单 `tun0` 与原 `7928` 实现。
- 3x-ui 回滚先停用两条新 inbound，再恢复数据库/配置备份并验证原分享链接。
- 旧 VPNGate 住宅入口在新链路稳定前只禁用、不删除，可独立恢复。
- 回滚不得更改 `<PUBLIC_HOSTNAME>` DNS，也不启用其他 VPS 接管。

## 14. Kimi 接管要求

Kimi 开始执行前必须：

1. 阅读本设计和 `.ai/HANDOFF.md`。
2. 检查实际 Git 差异、东京服务、3x-ui 数据库和 Xray 运行配置；现场事实优先于本文中的历史记录。
3. 保留四个当前由用户删除的旧云途设计/计划文档，不恢复、不覆盖。
4. 按测试驱动实施，每完成一个可验证工作单元立即更新 `.ai/HANDOFF.md`，当前负责人改为 `kimi`。
5. 遇到端口、证书路径、3x-ui tag 或现网配置与设计不一致时，选择最小兼容改动；若会影响现有直出服务则停止并报告维护者。
6. 不在 Git、交接文件或聊天输出中写入任何凭据。
