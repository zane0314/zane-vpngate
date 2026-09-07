#!/usr/bin/env python3
"""Measure the two existing XHTTP/CDN paths without creating another inbound."""
from __future__ import annotations

import argparse
import base64
import json
import os
import socket
import statistics
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path


DEFAULT_PATHS = {
    "xhttp_cdn_direct": Path("/etc/jp-cdn-sub/xhttp_state.json"),
    "xhttp_cdn_vpngate": Path("/etc/jp-cdn-sub/xhttp_vpngate_state.json"),
}


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def first_link(state: dict) -> str:
    url = f"https://{state['cdn_host']}/{state['sub_path']}?refresh=1"
    request = urllib.request.Request(url, headers={"User-Agent": "AimiliVPN/full-path-speedtest"})
    with urllib.request.urlopen(request, timeout=30) as response:
        decoded = base64.b64decode(response.read()).decode("utf-8")
    links = [line.strip() for line in decoded.splitlines() if line.strip()]
    if not links:
        raise RuntimeError("subscription contains no nodes")
    return links[0]


def xray_config(link: str, port: int) -> dict:
    parsed = urllib.parse.urlsplit(link)
    query = urllib.parse.parse_qs(parsed.query)
    if parsed.scheme != "vless" or query.get("type") != ["xhttp"]:
        raise RuntimeError("subscription node is not VLESS XHTTP")
    return {
        "log": {"loglevel": "warning"},
        "inbounds": [{"listen": "127.0.0.1", "port": port, "protocol": "socks", "settings": {"udp": False}}],
        "outbounds": [{
            "protocol": "vless",
            "settings": {"vnext": [{"address": parsed.hostname, "port": parsed.port, "users": [{"id": parsed.username, "encryption": "none"}]}]},
            "streamSettings": {
                "network": "xhttp", "security": "tls",
                "tlsSettings": {"serverName": query["sni"][0], "fingerprint": "chrome", "alpn": ["h2"]},
                "xhttpSettings": {"path": query["path"][0], "host": query["host"][0], "mode": query.get("mode", ["packet-up"])[0]},
            },
        }],
    }


def curl_metrics(proxy: str, download_bytes: int, upload_bytes: int, samples: int) -> dict:
    exit_ip = subprocess.run(
        ["curl", "-4", "-fsS", "--max-time", "30", "--proxy", proxy, "https://api.ipify.org"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    if not exit_ip or ":" in exit_ip:
        raise RuntimeError("full path did not return an IPv4 exit")
    downloads: list[float] = []
    uploads: list[float] = []
    ttfbs: list[float] = []
    down_total = up_total = 0
    for _ in range(samples):
        result = subprocess.run([
            "curl", "-4", "-fsS", "--max-time", "60", "--proxy", proxy, "-o", os.devnull,
            "-w", "%{speed_download} %{time_starttransfer} %{size_download}",
            f"https://speed.cloudflare.com/__down?bytes={download_bytes}",
        ], check=True, capture_output=True, text=True)
        speed, ttfb, size = [float(value) for value in result.stdout.split()[:3]]
        downloads.append(speed * 8 / 1_000_000)
        ttfbs.append(ttfb * 1000)
        down_total += round(size)
        if upload_bytes:
            result = subprocess.run([
                "curl", "-4", "-fsS", "--max-time", "60", "--proxy", proxy, "-o", os.devnull,
                "-X", "POST", "-H", "Content-Type: application/octet-stream", "--data-binary", "@-",
                "-w", "%{speed_upload} %{size_upload}", "https://speed.cloudflare.com/__up",
            ], input=b"\0" * upload_bytes, check=True, capture_output=True)
            speed, size = [float(value) for value in result.stdout.decode().split()[:2]]
            uploads.append(speed * 8 / 1_000_000)
            up_total += round(size)
    return {
        "ok": True,
        "exit_ip": exit_ip,
        "download_mbps": round(statistics.median(downloads), 2),
        "upload_mbps": round(statistics.median(uploads), 2) if uploads else 0,
        "ttfb_ms": round(statistics.median(ttfbs)),
        "samples": samples,
        "bytes_down": down_total,
        "bytes_up": up_total,
        "bytes_total": down_total + up_total,
    }


def probe_path(name: str, state_path: Path, xray: str, download_bytes: int, upload_bytes: int, samples: int) -> dict:
    state = json.loads(state_path.read_text(encoding="utf-8"))
    port = free_port()
    config = xray_config(first_link(state), port)
    with tempfile.TemporaryDirectory(prefix=f"aimili-{name}-") as directory:
        config_path = Path(directory) / "config.json"
        config_path.write_text(json.dumps(config), encoding="utf-8")
        subprocess.run([xray, "run", "-test", "-c", str(config_path)], check=True, capture_output=True)
        process = subprocess.Popen([xray, "run", "-c", str(config_path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            for _ in range(40):
                with socket.socket() as ready:
                    if ready.connect_ex(("127.0.0.1", port)) == 0:
                        break
                time.sleep(0.25)
            else:
                raise RuntimeError("temporary Xray client did not listen")
            return curl_metrics(f"socks5h://127.0.0.1:{port}", download_bytes, upload_bytes, samples)
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--xray", default="/usr/local/x-ui/bin/xray-linux-amd64")
    parser.add_argument("--download-bytes", type=int, default=10_000_000)
    parser.add_argument("--upload-bytes", type=int, default=1_000_000)
    parser.add_argument("--samples", type=int, default=1)
    parser.add_argument("--output", default="/var/lib/aimilivpn/full_path_speed.json")
    args = parser.parse_args()
    report = {"checked_at": time.time(), "ipv4_only": True, "paths": {}}
    for name, state_path in DEFAULT_PATHS.items():
        try:
            report["paths"][name] = probe_path(
                name, state_path, args.xray,
                max(1_000_000, min(args.download_bytes, 50_000_000)),
                max(0, min(args.upload_bytes, 10_000_000)),
                max(1, min(args.samples, 3)),
            )
        except Exception as exc:
            report["paths"][name] = {"ok": False, "error": str(exc)}
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(".tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(output)
    print(json.dumps(report, ensure_ascii=False))
    return 0 if all(value.get("ok") for value in report["paths"].values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
