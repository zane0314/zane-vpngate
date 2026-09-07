# 云途 VLESS/Trojan 入口与终验实施计划（阶段二）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在云途新增 VLESS Reality Vision TCP 与 Trojan TLS TCP 两条独立入口（sing-box sidecar，出站仅 127.0.0.1:7928），接入故障矩阵前端探针，收紧 8787 为回环监听，完成 v2 设计 §10.2 全表终验。

**Architecture:** 参照既有 `hy2-vpngate` sidecar 模式新建 `vt-vpngate.service`（独立 sing-box 配置：VLESS Reality + Trojan TLS 两个 inbound + 路由）；另建轻量 `vt-prober.service`（sing-box 客户端，两个回环 mixed inbound 分别经 VLESS/Trojan 出站打本机公网入口），前端探针 = curl 经 prober 访问回环健康端点；健康端点为独立 tiny 服务 `aimili-health.service`（127.0.0.1:18978 返回固定 200）。

**Tech Stack:** sing-box 1.15.0-alpha.2（`/etc/v2ray-agent/sing-box/sing-box`）、Python 3、systemd、curl。

**执行依据：** v2 设计 `docs/superpowers/specs/2026-09-07-tokyo-vpngate-dual-slot-vless-trojan-design-v2.md` + 阶段一计划。阶段一已完成（云途双槽运行中，HEAD=b55b224）。

## Global Constraints

- 复用现有 TLS 证书 `<TLS_CERT_PATH>.{crt,key}`；Trojan SNI 与客户端地址用 `<PUBLIC_HOSTNAME>`；VLESS Reality dest/SNI 用 `<REALITY_HANDSHAKE_HOST>:443`，客户端地址同样用 `<PUBLIC_HOSTNAME>`（不用 VPS IP）。
- 两条入口独立，不做合并订阅；端口现场选未占用 TCP 高位端口，不预定。
- 新 sidecar 的 TCP 全部路由到 SOCKS `127.0.0.1:7928`；UDP 一律拒绝；唯一例外：两个 inbound tag 到 `127.0.0.1:18978`（健康端点）走 direct，且该规则必须排在最前。
- 不改动 v2ray-agent 主 sing-box、hy2-vpngate、nginx 等现有服务配置；新增服务全部独立 unit。
- 探针绝不经过 7928；prober 只监听回环。
- 新凭据（Reality 私钥、UUID、Trojan 密码）只落在云途 root-only 文件与本地 `重要信息`/yaml 汇总，不进 git、不进 HANDOFF、不进聊天明文（报告中打码）。
- 云途资源紧：1 vCPU / 960MB，新增 2 个 sing-box 进程须实测内存可承受。

---

### Task 1: 前端探针真实实现（health_probes.probe_frontend 升级）

**Files:**
- Modify: `health_probes.py`（probe_frontend 约 60 行附近）
- Test: `tests/test_health_probes.py`（追加用例）

**Interfaces:**
- cfg schema 升级为 `{"probe_proxy": "socks5h://127.0.0.1:18081", "probe_url": "http://127.0.0.1:18978/", "expect": "ok", "timeout": 8}`；实现改为 `subprocess` 调 `curl -s --max-time <timeout> -x <probe_proxy> <probe_url>`，返回码 0 且响应体含 `expect` 才 ok；保持向后兼容：cfg 无 `probe_proxy` 时退回既有 TCP 连接占位行为；cfg 为 None 仍返回 `not-configured`。
- `FRONTEND_PROBE_CONFIG` JSON 文件格式：`{"vless": {…cfg…}, "trojan": {…cfg…}}`（run_failure_attribution 已按名读取，不变）。

- [ ] **Step 1:** 追加失败测试：mock subprocess.run——curl 返回 0 且 stdout 含 "ok" → ok=True；curl 返回非 0 / 超时 / 响应体不含 expect → ok=False；无 probe_proxy 键 → 走旧 TCP 分支；cfg=None → not-configured。
- [ ] **Step 2:** `python3 -m unittest tests.test_health_probes -v` 确认新用例失败。
- [ ] **Step 3:** 实现 probe_frontend 升级。
- [ ] **Step 4:** 全量 `python3 -m unittest discover -s tests` 绿（现有 153 个不回归）。
- [ ] **Step 5:** 提交 `feat: probe frontends through local prober proxies via curl`。

### Task 2: 管理页监听地址可配置（UI_HOST）

**Files:**
- Modify: `vpngate_manager.py`（`UI_PORT` 常量附近与 `main()` 中 DualStackHTTPServer 绑定处，约 161 行与 6660 行附近）
- Test: `tests/test_ui_host.py`

**Interfaces:**
- Produces: 模块级 `UI_HOST = os.environ.get("UI_HOST", 当前默认值)`；`main()` 绑定 `(UI_HOST, UI_PORT)`。默认值保持现状（不破坏东京既有部署），云途 env 里显式设 `127.0.0.1`。

- [ ] **Step 1:** 失败测试：默认 UI_HOST 等于现状值；env UI_HOST=127.0.0.1 时绑定地址为元组 `("127.0.0.1", UI_PORT)`（mock DualStackHTTPServer 断言构造参数）。
- [ ] **Step 2–5:** TDD 节奏同前；全量绿后提交 `feat: make management UI bind host configurable via UI_HOST`。

### Task 3: 云途部署 sidecar 栈（健康端点 + vt-vpngate + vt-prober）

**Files（远端云途）:**
- Create: `/etc/aimili-health/health_server.py` + `/etc/systemd/system/aimili-health.service`
- Create: `/etc/vt-vpngate/config.json` + `/etc/vt-vpngate/credential`（root-only 0600，存 Reality 私钥/UUID/Trojan 密码）+ `/etc/systemd/system/vt-vpngate.service`
- Create: `/etc/vt-prober/config.json` + `/etc/systemd/system/vt-prober.service`
- Create: `/etc/aimilivpn/frontend-probes.json`（FRONTEND_PROBE_CONFIG 指向它）
- Modify: `/etc/default/aimilivpn`（追加 `FRONTEND_PROBE_CONFIG=/etc/aimilivpn/frontend-probes.json`、`UI_HOST=127.0.0.1`）
- 同步部署本地新代码（Task 1–2 的提交）

- [ ] **Step 1: 部署前基线**：9 项既有服务 active、双槽健康（tun0/tun1、指针、standby 日志）；新建备份 `/root/aimili-backup-$(date +%Y%m%d-%H%M%S)`。部署本地 HEAD 代码到 /opt/aimilivpn（git archive 方式，sha256 核对），但**先不重启 aimilivpn、先不改 UI_HOST**（8787 收紧留到 Task 5 验证通过后；env 里 UI_HOST 此行 Task 5 才追加）。
- [ ] **Step 2: 健康端点**：`aimili-health.service` 跑一个约 30 行 python http.server（绑定 127.0.0.1:18978，任意 GET 返回 200  body `ok`）。`curl -s http://127.0.0.1:18978/` 返回 ok；确认公网不可达（它只听回环）。
- [ ] **Step 3: vt-vpngate sidecar**：用 sing-box 生成 Reality 密钥对（`sing-box generate reality-keypair`）与随机 UUID、Trojan 密码（openssl rand -hex 16）；现场选两个未占用 TCP 高位端口（`ss -tln` 确认）。配置要点：
  - inbound 1：`type=vless`，tag `vless-vpngate-in`，listen `::`，`users[0].flow=xtls-rprx-vision`，`tls.enabled=true`、`tls.reality.enabled=true`、`reality.handshake.server=<REALITY_HANDSHAKE_HOST> server_port=443`、`reality.private_key`、short_id 随机 8 hex
  - inbound 2：`type=trojan`，tag `trojan-vpngate-in`，`tls.enabled=true`、`certificate_path/key_path` 复用现有证书、`server_name=<PUBLIC_HOSTNAME>`
  - route.rules 顺序：① inbound∈两tag 且 ip_is_private/目标 127.0.0.1:18978 → outbound `direct-health`；② inbound∈两tag 且 network=udp → action reject；③ inbound∈两tag → outbound `aimilivpn`(socks 127.0.0.1:7928 version 5)。final=direct-health 不存在则拒绝兜底（用 `final` 指向 socks 即可，规则已穷尽）
  - sing-box 1.15 规则语法用 `rule action`（`"action": "reject"`）形式，以 `sing-box check -c` 实际通过为准
  - `sing-box check -c /etc/vt-vpngate/config.json` 通过后 enable+start；`ss -tlnp` 确认两端口在听
- [ ] **Step 4: vt-prober sidecar**：两个回环 mixed inbound（127.0.0.1:18081 → outbound vless（reality public key、short_id、uuid、flow vision、server=<PUBLIC_HOSTNAME>、server_port=VLESS 端口）；127.0.0.1:18082 → outbound trojan（密码、tls server_name=<PUBLIC_HOSTNAME>））。check 通过后 enable+start；确认只听回环。
- [ ] **Step 5: 本机握手验证**：`curl -s --max-time 10 -x socks5h://127.0.0.1:18081 http://127.0.0.1:18978/` 与 18082 均返回 `ok`（证明 VLESS/Trojan 握手 + 回环例外路由通）；再 `curl -s --max-time 15 -x socks5h://127.0.0.1:18081 https://api.ipify.org` 与 18082 的出口 = 当前 active 槽实测出口 ≠ 云途直连基线。
- [ ] **Step 6: 接入故障矩阵**：写 `/etc/aimilivpn/frontend-probes.json`（vless→18081、trojan→18082，probe_url=http://127.0.0.1:18978/，expect=ok），/etc/default/aimilivpn 追加 `FRONTEND_PROBE_CONFIG=...`；重启 aimilivpn；`journalctl -u aimilivpn` 确认前端探针不再报 not-configured。
- [ ] **Step 7: 回归**：全部既有服务 active、双槽健康、7928 出口正常、hy2-vpngate 实测仍通；`free -m` 内存可承受（avail >150MB）。
- [ ] **Step 8:** 更新 `.ai/HANDOFF.md`（证据 + 下一步 Task 4）。

### Task 4: 外部真实握手与故障矩阵终验

- [ ] **Step 1: 外部握手**：从 `<TEST_SOURCE_HOST>`（凭据在 `<LOCAL_CREDENTIALS_DIR>`）跑临时 sing-box 客户端，分别完成 VLESS Reality 与 Trojan TLS 握手：出口 = active 槽实测出口 ≠ VPS 默认公网。测试后清理临时文件/进程。
- [ ] **Step 2: 标准链接语法**：按 3x-ui 风格手工组装 `vless://<UUID>@<PUBLIC_HOSTNAME>:<VLESS_PORT>?encryption=none&flow=xtls-rprx-vision&security=reality&sni=<REALITY_HANDSHAKE_HOST>&fp=chrome&pbk=<REALITY_PUBLIC_KEY>&sid=<SHORT_ID>#...` 与 `trojan://<TROJAN_PASSWORD>@<PUBLIC_HOSTNAME>:<TROJAN_PORT>?security=tls&sni=<PUBLIC_HOSTNAME>#...`；用临时 sing-box 客户端直接以链接导入方式验证可解析可握手。
- [ ] **Step 3: 矩阵行注入（vless-only）**：`systemctl stop` vt-vpngate 或防火墙拦 VLESS 端口 → 等 2 个 checker 周期 → 断言：活动指针不变、local-probe-state 无新冷却、state.json `last_alert` 含 vless 字样、无双槽切换。恢复。
- [ ] **Step 4: 矩阵行注入（host-stack）**：kill active 槽 OpenVPN + 停 vt-vpngate → 断言 `rotation_paused=True`、不计节点失败、不切槽；恢复 vt-vpngate 后下一轮归因自动恢复判断并 failover 到 standby。
- [ ] **Step 5: UDP 封堵**：临时客户端经 VLESS/Trojan 发 UDP（sing-box 客户端 `network udp` 拨测或 dns udp over proxy 测试）→ 拒绝；`curl -x socks5h://127.0.0.1:7928` 的 UDP ASSOCIATE 请求（可用 `nc` 手工报文或 python socket 发 \x05\x03）→ 拒绝；均观察不到默认公网出口。
- [ ] **Step 6:** 更新 HANDOFF（证据）。

### Task 5: 8787 收紧与最终回归

- [ ] **Step 1:** `/etc/default/aimilivpn` 追加 `UI_HOST=127.0.0.1`（Task 2 已使代码支持），重启 aimilivpn。
- [ ] **Step 2:** `ss -tlnp` 确认 8787 仅 `127.0.0.1`；从外部 `curl --max-time 5 http://<YUNTU_VPS_IP>:8787/` 连接失败；SSH 隧道 `ssh -L 18787:127.0.0.1:8787 root@<YUNTU_VPS_IP>` 后本地访问 200（验证管理通道可用）。
- [ ] **Step 3: 最终回归**：9 项既有服务 + aimili-health + vt-vpngate + vt-prober 全 active、无 failed unit；双槽健康；7928/VLESS/Trojan/HY2 出口全 ≠ 直连基线；nginx -t 通过。
- [ ] **Step 4: 收尾**：多余备份目录清理（保留最新 2 个）；更新本地节点汇总文件（`<LOCAL_VPS_NOTES_DIR>`，参照 HY2 先例格式，权限 0600）；`.ai/HANDOFF.md` 状态推进为阶段二完成、记录新凭据存放位置（不写凭据本身）。
- [ ] **Step 5:** 本地 `python3 -m unittest discover -s tests` 与 `git diff --check` 终绿；不 push git。

---

## 自检记录

- **规格覆盖**：v2 §8.1 前端探针 → Task 1 + Task 3 Step 4/6；§8.3 矩阵行的现场验证 → Task 4 Step 3/4；§10.2 行 2（入口出口一致）→ Task 3 Step 5 + Task 4 Step 1；行 5/6 → Task 4 Step 3/4；行 8 → Task 4 Step 5；行 10（8787）→ Task 5。§12 状态/通知已部分在阶段一落地，阶段二凭据与汇总文件更新在 Task 5 Step 4。UDP 例外与路由顺序 → Task 3 Step 3。
- **占位符扫描**：Reality 密钥/UUID/端口均为"现场生成/现场选"，属安全约束而非占位。
- **一致性**：FRONTEND_PROBE_CONFIG 的 cfg 键（probe_proxy/probe_url/expect/timeout）在 Task 1 与 Task 3 Step 6 一致；18081/18082/18978 三个回环端口全计划统一。
