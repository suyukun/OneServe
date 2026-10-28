#!/usr/bin/env bash
# OneServe 离线 Docker 安装脚本（aarch64 · 无外网 ECS 自装）
#
# 用法（在解包目录内）：
#   sudo bash install.sh
#
# 做什么：校验包内 sha256 → 停已有 docker/containerd（若有）→ 静态二进制装
# /usr/local/bin → compose 插件装 /usr/local/lib/docker/cli-plugins →
# 装 systemd 单元 → enable --now → 把当前用户加进 docker 组 → 打印自检。
# 幂等：可重复执行，重跑即覆盖升级；不动 /var/lib/docker 镜像与数据。
# 面向 Linux systemd 发行版（ECS 目标机执行；勿在本机 Mac 运行）。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

log() { printf '\n== %s ==\n' "$*"; }
die() { printf '错误：%s\n' "$*" >&2; exit 1; }

BIN_DIR="/usr/local/bin"
PLUGIN_DIR="/usr/local/lib/docker/cli-plugins"
UNIT_DIR="/etc/systemd/system"

# ---- 前置检查 ----
[ "$(id -u)" -eq 0 ] || die "请用 sudo 运行：sudo bash install.sh"
[ "$(uname -m)" = "aarch64" ] || die "本机架构 $(uname -m) ≠ aarch64，此包不适用（x86_64 找 Rose 另打）"
command -v systemctl >/dev/null 2>&1 || die "未找到 systemctl（本脚本面向 systemd 发行版）"

DOCKER_TGZ="$(ls "$SCRIPT_DIR"/docker-*.tgz 2>/dev/null | head -1 || true)"
[ -n "$DOCKER_TGZ" ] || die "当前目录缺 docker-*.tgz（解包完整目录后再跑）"
COMPOSE_BIN="$SCRIPT_DIR/docker-compose-linux-aarch64"
[ -f "$COMPOSE_BIN" ] || die "当前目录缺 docker-compose-linux-aarch64"
[ -f "$SCRIPT_DIR/docker.service" ] || die "当前目录缺 docker.service"

log "0/7 校验包内文件 sha256"
sha256sum -c --status sha256sums.txt || die "sha256 校验失败：包传输损坏，重新拷贝整个目录"

log "1/7 停已有 docker（若有；只停服务，不删镜像与数据）"
if systemctl list-unit-files docker.service >/dev/null 2>&1; then
  systemctl stop docker.socket docker.service 2>/dev/null || true
  systemctl disable docker.socket docker.service 2>/dev/null || true
fi
# 手工（非 systemd）起的 dockerd：温和终止并等待退出
pkill -x dockerd 2>/dev/null || true
for _ in 1 2 3 4 5 6 7 8 9 10; do
  pgrep -x dockerd >/dev/null 2>&1 || break
  sleep 1
done
# 发行版自带 containerd.service 若在跑：停掉（其 socket 与 dockerd 自带的 containerd
# 冲突，独占 /run/containerd/containerd.sock）。只停不 disable；若他负载依赖请先评估。
if systemctl is-active --quiet containerd.service 2>/dev/null; then
  systemctl stop containerd.service 2>/dev/null || true
  echo "注：已停用系统自带 containerd.service（与本安装的 dockerd 冲突；仅停用未禁用）"
fi

log "2/7 安装静态二进制到 $BIN_DIR"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
tar -xzf "$DOCKER_TGZ" -C "$TMP"
[ -d "$TMP/docker" ] || die "静态包结构异常（解包后缺 docker/ 目录）"
mkdir -p "$BIN_DIR"
install -m 0755 "$TMP"/docker/* "$BIN_DIR"/

log "3/7 安装 compose 插件到 $PLUGIN_DIR"
mkdir -p "$PLUGIN_DIR"
install -m 0755 "$COMPOSE_BIN" "$PLUGIN_DIR/docker-compose"

log "4/7 确保 docker 组存在"
getent group docker >/dev/null || groupadd docker

log "5/7 安装 systemd 单元并 daemon-reload"
install -m 0644 docker.service docker.socket "$UNIT_DIR"/
systemctl daemon-reload

log "6/7 启动并设开机自启"
systemctl enable --now docker.socket docker.service

log "7/7 把当前用户加入 docker 组"
if [ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != "root" ]; then
  usermod -aG docker "$SUDO_USER"
  echo "已把 $SUDO_USER 加入 docker 组（重新登录生效；急用可 newgrp docker）"
fi

# ---- 自检 ----
DOCKER_VER="$(basename "$DOCKER_TGZ" .tgz)"
DOCKER_VER="${DOCKER_VER#docker-}"
COMPOSE_VER="$("$PLUGIN_DIR/docker-compose" version --short 2>/dev/null || echo 未知)"
echo
echo "== 自检（安装即验） =="
"$BIN_DIR/docker" --version
"$BIN_DIR/docker" compose version || echo "警告：docker 未识别 compose 插件，见 README 故障表"
printf 'docker.service: %s\n' "$(systemctl is-active docker.service)"
echo
echo "== 你自己再验两条（建议重新登录后） =="
echo "  docker --version          # 预期：Docker version $DOCKER_VER"
echo "  docker compose version    # 预期：Docker Compose version $COMPOSE_VER"
echo
echo "完成。接下来按《部署引导单-维护者专属_v0》§3 往下走。"
