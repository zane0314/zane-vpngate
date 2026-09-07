# 东京 VPNGate 直连入口设计

## 目标

在东京 VPS 新增两条通过 AimiliVPN 日本住宅出口的客户端入口：

- VLESS Reality + Vision / TCP `30441`
- Trojan TLS / TCP `30552`

东京现有 HY2-VPNGate 已存在，继续使用现有 sing-box 入站 UDP `24444`，不创建第二个 HY2 监听。

## 已确认的现状

- `aimilivpn` active，严格日本住宅与 fail-closed 已生效，本地代理为回环 `127.0.0.1:7928`。
- x-ui/Xray 已有普通直连 VLESS Reality 和 Trojan TLS，但它们不经过 VPNGate。
- `sing-box-bitsflow-hy2` 已提供 HY2-VPNGate：HY2 → SOCKS → `127.0.0.1:7928` → AimiliVPN → VPNGate 日本住宅。
- nginx、CDN、ARGO、现有直连入口和现有 HY2 配置不在本次改动范围内。

## 方案

### VLESS Reality + Vision

使用现有 x-ui/Xray 面板原生入站创建流程新增 TCP `30441`：

- network：TCP
- security：Reality
- flow：`xtls-rprx-vision`
- 使用独立 Reality 密钥对
- 沿用已经验证可用的握手目标/SNI 策略
- 新入站 tag 单独命名并加入既有 `aimili-vpngate` 路由规则

不修改现有直连 Reality 入站，不复用其客户端 UUID 或私钥。

### Trojan TLS

使用现有 x-ui/Xray 面板原生入站创建流程新增 TCP `30552`：

- network：TCP
- security：TLS
- 使用现有东京证书材料和当前 TLS 主机名
- 新入站 tag 单独命名并加入既有 `aimili-vpngate` 路由规则

不修改现有直连 Trojan 入站，不新建第二套证书续期或反向代理服务。

### HY2

保留现有 `sing-box-bitsflow-hy2` 的 HY2-VPNGate 入站 UDP `24444`。只做配置与出口回归验证，不新增端口、密码或服务。

## 订阅边界

- 新 VLESS/Trojan 加入现有独立 VPNGate 订阅管线。
- 现有直连订阅、CDN/WS、XHTTP、ARGO 输出保持原样。
- HY2 继续使用现有 HY2-VPNGate 链路；若现有订阅格式不承载 HY2，则保留现有 HY2 链接，不为本次新增聚合服务。
- 所有分享链接、UUID、密码、Reality 私钥和证书路径只存在于 VPS 运行配置，不写入仓库或交接文档。

## 变更顺序

1. 备份 x-ui/Xray 配置、面板数据库和当前订阅生成状态。
2. 再次确认 TCP `30441`、`30552` 未被占用，确认现有 HY2 UDP `24444` 不变。
3. 通过面板原生登录/API 创建 VLESS 与 Trojan 入站；不直接绕过面板写入数据库。
4. 将两个新入站的 route tag 接入现有 `aimili-vpngate` 出站，并执行 Xray 配置校验与 reload。
5. 通过现有订阅生成管线核对新节点字段；不重建 CDN 或 ARGO 订阅服务。
6. 执行端到端验收并更新东京主交接文件。

## 验收标准

- 面板入站列表出现 2 个新入口，协议、端口、TLS/Reality 字段与设计一致。
- Xray 配置校验通过，x-ui、nginx、sing-box HY2、AimiliVPN 和相关订阅服务均 active，failed units 为 0。
- TCP `30441` 的 Reality + Vision 握手通过，流量经过 `aimili-vpngate`。
- TCP `30552` 的 Trojan TLS 握手通过，流量经过 `aimili-vpngate`。
- 两条新入口的实际出口均为日本住宅且不泄漏东京 VPS 默认出口。
- 现有 HY2-VPNGate UDP `24444` 仍可连接并保持日本住宅出口。
- 现有直连 VLESS、直连 Trojan、CDN/WS、XHTTP 和 ARGO 订阅回归通过。
- 独立 VPNGate 订阅返回 200，解码后包含新 VLESS/Trojan，字段与面板真实入站一致。
- 回滚只恢复本次备份并 reload x-ui/Xray；不删除 AimiliVPN 数据、不重建 HY2、不改 nginx/CDN/ARGO。

## 风险与回滚

- Reality 密钥或 SNI 填写错误会导致握手失败；创建后必须从实际面板输出核对，不手填分享链接。
- 面板 reload 失败时立即停止新增入口验收，保留备份并恢复原 Xray 配置。
- 订阅输出若影响既有节点，回滚订阅生成变更，不修改已验证的 CDN/ARGO 源。
- 任何住宅出口验证失败都视为未完成，不允许回落到东京 VPS 默认出口。
