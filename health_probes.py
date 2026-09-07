"""双槽健康探针与纯函数故障矩阵（云途双槽改造阶段一 Task 4）。"""

import os
import socket
import subprocess
import time

import slot_state
import vpn_utils

ProbeResult = dict

_EGRESS_PROBE_BYTES = 200_000
_FRONTEND_TCP_TIMEOUT = 4


def default_slot_process_alive(slot, data_dir):
    """默认进程存活检查：读 data_dir/openvpn_slot_<slot>.pid 并以 os.kill(pid, 0) 核对。

    health_probes 不 import vpngate_manager（避免循环 import）；调用方可以注入
    读取 manager.SLOT_RUNTIME 的 process_alive callable 覆盖本实现。
    """
    pid_path = os.path.join(data_dir, f"openvpn_slot_{slot}.pid")
    try:
        with open(pid_path) as fh:
            pid = int(fh.read().strip())
    except (OSError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return os.path.exists(f"/proc/{pid}")
    return True


def _default_egress_probe(device):
    return vpn_utils.probe_tunnel_egress(
        device,
        download_bytes=_EGRESS_PROBE_BYTES,
        upload_bytes=0,
        samples=1,
    )


def probe_vpngate_slot(slot, data_dir, *, baseline_ip=None, process_alive=None,
                       egress_probe=None, net_root="/sys/class/net"):
    """探测指定槽 VPNGate 隧道健康：进程存活 → 设备存在 → 绑定设备实测出口 IP。

    出口探测直接绑定槽位 TUN 设备，绝不经过 7928。baseline_ip 必须显式传入；
    无法取得基线时 fail-closed 返回 ok=False。
    """
    if slot not in slot_state.SLOT_DEVICES:
        raise ValueError(f"invalid slot: {slot!r}")
    checked_at = time.time()
    process_alive = process_alive or default_slot_process_alive
    egress_probe = egress_probe or _default_egress_probe
    device = slot_state.SLOT_DEVICES[slot]

    def result(ok, detail):
        return {"ok": ok, "detail": detail, "checked_at": checked_at}

    if not process_alive(slot, data_dir):
        return result(False, f"process-dead: slot {slot} openvpn 进程不存活")
    if not os.path.isdir(os.path.join(net_root, device)):
        return result(False, f"device-missing: {device} 不存在于 {net_root}")
    egress = egress_probe(device)
    if not egress.get("ok"):
        return result(False, str(egress.get("error") or "egress-probe-failed"))
    exit_ip = str(egress.get("exit_ip") or "")
    if not exit_ip:
        return result(False, "egress-probe-failed: 未返回出口 IP")
    if not baseline_ip:
        return result(False, "baseline-unavailable: 缺少默认公网基线 IP，fail-closed")
    if exit_ip == baseline_ip:
        return result(False, f"baseline-match: 出口 {exit_ip} 等于默认公网基线")
    return result(True, f"ok: slot {slot} 经 {device} 出口 {exit_ip}")


def probe_frontend(name, cfg):
    """前端探针：cfg 含 probe_proxy 时经 curl 走本地探针代理做真实握手探测；
    无 probe_proxy 退回 TCP 连接占位；cfg 为 None 或缺配置返回 not-configured。
    """
    checked_at = time.time()
    if not cfg:
        return {"ok": True, "detail": "not-configured", "checked_at": checked_at}
    probe_proxy = cfg.get("probe_proxy")
    if probe_proxy:
        return _probe_frontend_via_proxy(name, cfg, probe_proxy, checked_at)
    if not cfg.get("host") or not cfg.get("port"):
        return {"ok": True, "detail": "not-configured", "checked_at": checked_at}
    host, port = cfg["host"], int(cfg["port"])
    sock = None
    try:
        sock = socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET,
                             socket.SOCK_STREAM)
        sock.settimeout(_FRONTEND_TCP_TIMEOUT)
        sock.connect((host, port))
        return {"ok": True, "detail": f"tcp-ok: {name} {host}:{port}",
                "checked_at": checked_at}
    except OSError as exc:
        return {"ok": False, "detail": f"tcp-failed: {name} {host}:{port}: {exc}",
                "checked_at": checked_at}
    finally:
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass


def _probe_frontend_via_proxy(name, cfg, probe_proxy, checked_at):
    """经本地探针代理（vt-prober mixed inbound）curl probe_url，验证前端真实握手。"""
    probe_url = cfg.get("probe_url")
    if not probe_url:
        return {"ok": True, "detail": "not-configured", "checked_at": checked_at}
    expect = str(cfg.get("expect", "ok"))
    timeout = int(cfg.get("timeout", 8))
    cmd = ["curl", "-s", "--max-time", str(timeout), "-x", probe_proxy, probe_url]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout + 2)
    except subprocess.TimeoutExpired:
        return {"ok": False,
                "detail": f"curl-timeout: {name} 经 {probe_proxy} 超过 {timeout}s",
                "checked_at": checked_at}
    except OSError as exc:
        return {"ok": False, "detail": f"curl-failed: {name}: {exc}",
                "checked_at": checked_at}
    if proc.returncode != 0:
        return {"ok": False,
                "detail": f"curl-exit-{proc.returncode}: {name} 经 {probe_proxy}",
                "checked_at": checked_at}
    if expect not in proc.stdout:
        return {"ok": False,
                "detail": f"expect-missing: {name} 响应体不含 {expect!r}",
                "checked_at": checked_at}
    return {"ok": True,
            "detail": f"proxy-ok: {name} 经 {probe_proxy} 命中 {expect!r}",
            "checked_at": checked_at}


_MATRIX = {
    # (vpngate, vless, trojan): (attribution, action, count_node_failure,
    #                            pause_performance_switch, alert)
    (True, False, True): (
        "vless-only", "hold", False, False,
        "VLESS 前端故障，VPNGate 出口与 Trojan 正常，保持现状观察"),
    (True, True, False): (
        "trojan-only", "hold", False, False,
        "Trojan 前端故障，VPNGate 出口与 VLESS 正常，保持现状观察"),
    (True, False, False): (
        "frontend-stack", "hold", False, True,
        "VLESS/Trojan 前端栈同时故障，VPNGate 出口正常；保持切换并暂停性能择优"),
    (False, True, True): (
        "vpngate-exit", "failover", True, False,
        "VPNGate 出口故障，前端入口正常；执行切换并计入节点失败"),
    (False, False, False): (
        "host-stack", "pause_rotation", False, True,
        "出口与前端全部故障，疑宿主机/宿主机网络问题；暂停轮换"),
    (False, False, True): (
        "vpngate-exit+vless", "failover", True, False,
        "VPNGate 出口与 VLESS 前端故障，Trojan 正常；执行切换并计入节点失败"),
    (False, True, False): (
        "vpngate-exit+trojan", "failover", True, False,
        "VPNGate 出口与 Trojan 前端故障，VLESS 正常；执行切换并计入节点失败"),
}

_HEALTHY = {
    "attribution": "healthy",
    "action": "hold",
    "count_node_failure": False,
    "pause_performance_switch": False,
    "alert": None,
}


def classify_failure(vpngate_ok, vless_ok, trojan_ok):
    """纯函数故障归因矩阵（v2 设计 §8.3，7 行 + healthy 行）。无副作用。"""
    key = (bool(vpngate_ok), bool(vless_ok), bool(trojan_ok))
    if key == (True, True, True):
        return dict(_HEALTHY)
    attribution, action, count, pause_perf, alert = _MATRIX[key]
    return {
        "attribution": attribution,
        "action": action,
        "count_node_failure": count,
        "pause_performance_switch": pause_perf,
        "alert": alert,
    }
