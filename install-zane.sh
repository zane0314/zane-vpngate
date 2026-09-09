#!/usr/bin/env bash
set -Eeuo pipefail

readonly AIMILI_REPO="${AIMILI_REPO:-https://github.com/OWNER/REPOSITORY.git}"
readonly AIMILI_BRANCH="${AIMILI_BRANCH:-main}"
readonly AIMILI_INSTALL_DIR="${AIMILI_INSTALL_DIR:-/opt/aimilivpn}"
readonly AIMILI_ENV_FILE="${AIMILI_ENV_FILE:-/etc/default/aimilivpn}"
readonly AIMILI_LOCAL_SOURCE_DIR="${AIMILI_LOCAL_SOURCE_DIR:-}"
readonly AIMILI_PROFILE="${AIMILI_PROFILE:-local-authority}"
readonly SCRIPT_URL="${SCRIPT_URL:-https://raw.githubusercontent.com/OWNER/REPOSITORY/${AIMILI_BRANCH}/install-zane.sh}"
export AIMILI_PROFILE

info() { printf '[AimiliVPN] %s\n' "$*"; }
die() { printf '[AimiliVPN] ERROR: %s\n' "$*" >&2; exit 1; }

if [ "$(id -u)" -ne 0 ]; then
    die "请使用 root 运行。"
fi

if [ ! -c /dev/net/tun ]; then
    die "未检测到 /dev/net/tun 字符设备；请先在 VPS 面板启用 TUN/TAP。"
fi

if ! command -v git >/dev/null 2>&1 || ! command -v python3 >/dev/null 2>&1; then
    [ -f /etc/os-release ] || die "无法识别操作系统，不能自动安装 git/python3。"
    # shellcheck disable=SC1091
    . /etc/os-release
    case "${ID:-}" in
        debian|ubuntu)
            export DEBIAN_FRONTEND=noninteractive
            apt-get update -q
            apt-get install -y git python3 ca-certificates
            ;;
        alpine)
            apk add --no-cache git python3 ca-certificates bash
            ;;
        centos|rhel|rocky|almalinux|fedora|ol|amzn)
            if command -v dnf >/dev/null 2>&1; then
                dnf install -y git python3 ca-certificates
            else
                yum install -y git python3 ca-certificates
            fi
            ;;
        *)
            die "不支持自动引导依赖的系统：${ID:-unknown}。"
            ;;
    esac
fi

command -v git >/dev/null 2>&1 || die "git 安装失败。"
command -v python3 >/dev/null 2>&1 || die "python3 安装失败。"

if [ -n "$AIMILI_LOCAL_SOURCE_DIR" ]; then
    [ -f "$AIMILI_LOCAL_SOURCE_DIR/vpngate_manager.py" ] || die "本地安装包缺少 vpngate_manager.py。"
    [ -f "$AIMILI_LOCAL_SOURCE_DIR/config/aimilivpn.env" ] || die "本地安装包缺少正式配置模板。"
    [ ! -d "$AIMILI_LOCAL_SOURCE_DIR/.git" ] || die "本地源目录不能包含 .git；请使用正式发布归档。"
    if [ -e "$AIMILI_INSTALL_DIR" ] && [ ! -d "$AIMILI_INSTALL_DIR/.git" ] && [ ! -f "$AIMILI_INSTALL_DIR/.aimilivpn-managed" ]; then
        die "$AIMILI_INSTALL_DIR 已存在且不是受管安装，安装器不会覆盖。"
    fi
    if [ -d "$AIMILI_INSTALL_DIR/.git" ]; then
        current_origin="$(git -C "$AIMILI_INSTALL_DIR" remote get-url origin 2>/dev/null || true)"
        case "$current_origin" in
            "$AIMILI_REPO"|"${AIMILI_REPO#https://}"|"${AIMILI_REPO#https://github.com/}") ;;
            *) die "$AIMILI_INSTALL_DIR 来自其他 Git 仓库，安装器不会覆盖。" ;;
        esac
    fi
    info "从本地正式发布包部署；保留目标机的 vpngate_data。"
    mkdir -p "$AIMILI_INSTALL_DIR"
    cp -a "$AIMILI_LOCAL_SOURCE_DIR/." "$AIMILI_INSTALL_DIR/"
    touch "$AIMILI_INSTALL_DIR/.local_dev" "$AIMILI_INSTALL_DIR/.aimilivpn-managed"
elif [ -e "$AIMILI_INSTALL_DIR" ] && [ ! -d "$AIMILI_INSTALL_DIR/.git" ]; then
    die "$AIMILI_INSTALL_DIR 已存在但不是 Git 仓库。为避免覆盖未知数据，安装已停止。"
elif [ -d "$AIMILI_INSTALL_DIR/.git" ]; then
    current_origin="$(git -C "$AIMILI_INSTALL_DIR" remote get-url origin 2>/dev/null || true)"
    case "$current_origin" in
        "$AIMILI_REPO"|"${AIMILI_REPO#https://}"|"${AIMILI_REPO#https://github.com/}") ;;
        *) die "$AIMILI_INSTALL_DIR 的 origin 与 AIMILI_REPO 不一致；请人工确认迁移，安装器不会覆盖。" ;;
    esac
    info "更新现有正式安装到 ${AIMILI_BRANCH}；运行数据和管理凭据将保留。"
    git -C "$AIMILI_INSTALL_DIR" fetch origin "$AIMILI_BRANCH"
    git -C "$AIMILI_INSTALL_DIR" checkout -B "$AIMILI_BRANCH" "origin/$AIMILI_BRANCH"
else
    info "克隆 ${AIMILI_REPO} (${AIMILI_BRANCH})。"
    git clone --branch "$AIMILI_BRANCH" --single-branch "$AIMILI_REPO" "$AIMILI_INSTALL_DIR"
fi

case "$AIMILI_PROFILE" in
    local-authority) profile="$AIMILI_INSTALL_DIR/config/aimilivpn.env" ;;
    upstream-consumer) profile="$AIMILI_INSTALL_DIR/config/aimilivpn-upstream.env" ;;
    *) die "未知 AIMILI_PROFILE：$AIMILI_PROFILE" ;;
esac
[ -f "$profile" ] || die "正式配置模板不存在：$profile"

if [ -f "$AIMILI_ENV_FILE" ]; then
    backup="${AIMILI_ENV_FILE}.backup.$(date +%Y%m%d-%H%M%S)"
    cp "$AIMILI_ENV_FILE" "$backup"
    info "已备份原环境配置到 $backup。"
fi
install -m 0600 "$profile" "$AIMILI_ENV_FILE"
install -m 0644 "$AIMILI_INSTALL_DIR/systemd/aimilivpn.logrotate" /etc/logrotate.d/aimilivpn

info "调用项目原生安装管线。"
repo_path="${AIMILI_REPO#https://github.com/}"
repo_path="${repo_path#git@github.com:}"
repo_path="${repo_path%.git}"
repo_owner="${repo_path%%/*}"
repo_name="${repo_path#*/}"
bash "$AIMILI_INSTALL_DIR/install.sh" "$repo_owner" "$repo_name"

if command -v systemctl >/dev/null 2>&1; then
    install -m 0644 "$AIMILI_INSTALL_DIR/systemd/aimilivpn-pool-export.service" /etc/systemd/system/
    install -m 0644 "$AIMILI_INSTALL_DIR/systemd/aimilivpn-pool-export.timer" /etc/systemd/system/
    install -m 0644 "$AIMILI_INSTALL_DIR/systemd/aimilivpn-pool-sync.service" /etc/systemd/system/
    install -m 0644 "$AIMILI_INSTALL_DIR/systemd/aimilivpn-pool-sync.timer" /etc/systemd/system/
    systemctl daemon-reload
    if [ "$AIMILI_PROFILE" = "local-authority" ]; then
        getent group aimili-sync >/dev/null 2>&1 || groupadd --system aimili-sync
        if ! id -u aimili-sync >/dev/null 2>&1; then
            useradd --system --gid aimili-sync --home-dir /var/lib/aimili-sync \
                --create-home --shell /bin/bash aimili-sync
        fi
        install -d -o aimili-sync -g aimili-sync -m 0700 /var/lib/aimili-sync/.ssh
        install -d -o root -g aimili-sync -m 0750 /var/lib/aimilivpn-export
        systemctl disable --now aimilivpn-pool-sync.timer >/dev/null 2>&1 || true
        systemctl enable --now aimilivpn-pool-export.timer
    else
        install -d -m 0700 /etc/aimilivpn "$AIMILI_INSTALL_DIR/vpngate_data"
        systemctl disable --now aimilivpn-pool-export.timer >/dev/null 2>&1 || true
        systemctl enable --now aimilivpn-pool-sync.timer
    fi
fi

info "执行安装后验收。"
bash "$AIMILI_INSTALL_DIR/scripts/verify-formal-install.sh"

info "正式安装完成。以后可用相同命令升级："
printf 'bash <(curl -fsSL %s)\n' "$SCRIPT_URL"
