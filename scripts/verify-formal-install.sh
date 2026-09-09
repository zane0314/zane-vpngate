#!/usr/bin/env bash
set -u

readonly INSTALL_DIR="${AIMILI_INSTALL_DIR:-/opt/aimilivpn}"
readonly ENV_FILE="${AIMILI_ENV_FILE:-/etc/default/aimilivpn}"
readonly AIMILI_PROFILE="${AIMILI_PROFILE:-local-authority}"
failures=0
warnings=0

pass() { printf '[PASS] %s\n' "$*"; }
warn() { printf '[WARN] %s\n' "$*"; warnings=$((warnings + 1)); }
fail() { printf '[FAIL] %s\n' "$*"; failures=$((failures + 1)); }

check_file() {
    if [ -f "$1" ]; then pass "文件存在：$1"; else fail "文件缺失：$1"; fi
}

check_env() {
    key="$1"
    expected="$2"
    if grep -Eq "^${key}=${expected}$" "$ENV_FILE" 2>/dev/null; then
        pass "${key}=${expected}"
    else
        fail "${key} 应为 ${expected}"
    fi
}

if [ -c /dev/net/tun ]; then pass "TUN/TAP 可用"; else fail "TUN/TAP 不可用"; fi
check_file "$INSTALL_DIR/vpngate_manager.py"
check_file "$INSTALL_DIR/node_pool.py"
check_file "$INSTALL_DIR/scripts/full_path_speedtest.py"
check_file "$INSTALL_DIR/vpngate_data/ui_auth.json"
check_file "$ENV_FILE"
check_file /etc/logrotate.d/aimilivpn

check_env STRICT_RESIDENTIAL_ONLY true
check_env TRUSTED_POOL_LIMIT 10
check_env OBSERVATION_POOL_LIMIT 10
check_env TRUST_MIN_SUCCESSES 1
check_env HARD_GATE_TTL_SECONDS 108000
check_env STANDBY_TARGET 10
check_env STANDBY_MAX_AGE_SECONDS 691200
if [ "$AIMILI_PROFILE" = "upstream-consumer" ]; then
    check_env POOL_SOURCE_MODE upstream-consumer
    check_env TEST_MAX_WORKERS 1
    check_env UPSTREAM_SNAPSHOT_MAX_AGE_SECONDS 7200
    check_env UPSTREAM_LOCAL_PROBE_BATCH_SIZE 10
    check_env UPSTREAM_LOCAL_PROBE_INTERVAL_SECONDS 300
    check_env UPSTREAM_LOCAL_PROBE_BYTES 25000000
    check_env UPSTREAM_LOCAL_PROBE_SAMPLES 1
    check_env UPSTREAM_LOCAL_PROBE_RETENTION_SECONDS 172800
else
    check_env POOL_SOURCE_MODE local-authority
    check_env TEST_MAX_WORKERS 2
fi
check_env DAILY_MAINTENANCE_START_HOUR 3
check_env DAILY_MAINTENANCE_END_HOUR 4
check_env STABLE_POOL_PROBE_DAILY_LIMIT 2
check_env STABLE_POOL_PROBE_BYTES 25000000
check_env STABLE_POOL_PROBE_UPLOAD_BYTES 3000000
check_env STABLE_POOL_PROBE_SAMPLES 2
check_env FULL_PROBE_BYTES 10000000
check_env QUALITY_TIE_WINDOW 5
check_env SPEED_SWITCH_GAIN_PERCENT 30
check_env SWITCH_REQUIRED_WINS 2
check_env LOCAL_PROXY_HOST 127.0.0.1
check_env UI_HOST 127.0.0.1

if command -v systemctl >/dev/null 2>&1; then
    if systemctl is-enabled --quiet aimilivpn.service; then pass "systemd 已设为开机启动"; else fail "systemd 未启用"; fi
    if systemctl is-active --quiet aimilivpn.service; then pass "AimiliVPN 服务运行中"; else fail "AimiliVPN 服务未运行"; fi
    timer="aimilivpn-pool-export.timer"
    [ "$AIMILI_PROFILE" = "upstream-consumer" ] && timer="aimilivpn-pool-sync.timer"
    if systemctl is-enabled --quiet "$timer"; then pass "$timer 已启用"; else fail "$timer 未启用"; fi
elif command -v rc-service >/dev/null 2>&1; then
    if rc-service aimilivpn status >/dev/null 2>&1; then pass "AimiliVPN 服务运行中"; else fail "AimiliVPN 服务未运行"; fi
else
    fail "未找到受支持的服务管理器"
fi

auth_file="$INSTALL_DIR/vpngate_data/ui_auth.json"
if [ -f "$auth_file" ]; then
    auth_summary="$(python3 - "$auth_file" <<'PY'
import json, sys
with open(sys.argv[1], encoding="utf-8") as handle:
    cfg = json.load(handle)
required = ("username", "password", "secret_path", "port", "proxy_port")
missing = [key for key in required if not cfg.get(key)]
print("missing=" + ",".join(missing))
print("ui_port=" + str(cfg.get("port", "")))
print("proxy_port=" + str(cfg.get("proxy_port", "")))
PY
)"
    if printf '%s\n' "$auth_summary" | grep -qx 'missing='; then
        pass "管理路径、账号和密码均已生成"
    else
        fail "管理凭据不完整"
    fi
fi

if command -v ss >/dev/null 2>&1; then
    if ss -lnt | grep -Eq '127\.0\.0\.1:7928|\[::1\]:7928'; then
        pass "本地代理监听在回环地址"
    else
        warn "尚未发现回环代理端口；首次选出合格节点前可能出现此状态"
    fi
fi

state_file="$INSTALL_DIR/vpngate_data/state.json"
if [ -f "$state_file" ]; then
    python3 - "$state_file" <<'PY' || failures=$((failures + 1))
import json, sys
with open(sys.argv[1], encoding="utf-8") as handle:
    state = json.load(handle)
print("[INFO] active_node=" + str(state.get("active_openvpn_node_id") or "等待合格节点"))
print("[INFO] strict_residential_only=" + str(state.get("strict_residential_only")))
print("[INFO] trusted_pool=" + str(state.get("trusted_pool_count", 0)))
print("[INFO] observation_pool=" + str(state.get("observation_pool_count", 0)))
PY
else
    warn "状态文件尚未生成；后台可能仍在首次启动"
fi

printf '[SUMMARY] failures=%s warnings=%s\n' "$failures" "$warnings"
if [ "$failures" -ne 0 ]; then
    exit 1
fi
