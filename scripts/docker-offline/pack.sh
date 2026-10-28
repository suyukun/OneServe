#!/usr/bin/env bash
# OneServe 离线 Docker 安装包组包脚本（本机 Mac 执行；只下载/组包/校验，禁止本机安装）
#
# 用法：
#   bash scripts/docker-offline/pack.sh                        # 自动探测最新稳定版（dist/build 已有则复用）
#   DOCKER_VER=29.8.1 COMPOSE_VER=v5.5.1 bash scripts/docker-offline/pack.sh
#
# 产物（dist/ 不入库）：
#   dist/docker-offline-aarch64-<docker版>.tar
#   dist/docker-offline-aarch64-<docker版>.tar.sha256
#
# 包内布局：tar 顶层目录 docker-offline-aarch64-<ver>/，含
#   docker-<ver>.tgz（官方 aarch64 静态包）/ docker-compose-linux-aarch64 /
#   docker.service / docker.socket / install.sh / README.md / sha256sums.txt
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DIST="$HERE/../../dist"
BUILD="$DIST/build"
STAGE="$BUILD/stage"
mkdir -p "$BUILD"

if command -v sha256sum >/dev/null 2>&1; then SHA_CMD="sha256sum"; else SHA_CMD="shasum -a 256"; fi

# ---- 1. 定版本（默认探测最新稳定版） ----
if [ -z "${DOCKER_VER:-}" ]; then
  DOCKER_VER="$(curl -fsSL "https://download.docker.com/linux/static/stable/aarch64/" \
    | grep -oE 'href="docker-[0-9][^"]*\.tgz"' | sed 's/href="//;s/"//' \
    | sort -uV | tail -1 | sed 's/^docker-//;s/\.tgz$//')"
  [ -n "$DOCKER_VER" ] || { echo "错误：探测 Docker 最新版失败，可手动 DOCKER_VER=x.y.z 重跑" >&2; exit 1; }
fi
if [ -z "${COMPOSE_VER:-}" ]; then
  COMPOSE_VER="$(curl -fsSL "https://api.github.com/repos/docker/compose/releases/latest" \
    | grep -oE '"tag_name": *"[^"]+"' | head -1 | sed 's/.*"\([^"]*\)"$/\1/')"
  [ -n "$COMPOSE_VER" ] || { echo "错误：探测 compose 最新版失败，可手动 COMPOSE_VER=vX.Y.Z 重跑" >&2; exit 1; }
fi
echo "版本：docker $DOCKER_VER / compose $COMPOSE_VER"

# ---- 2. 下载（已有则复用；来源如实记录） ----
DOCKER_URL="https://download.docker.com/linux/static/stable/aarch64/docker-$DOCKER_VER.tgz"
COMPOSE_URL="https://github.com/docker/compose/releases/download/$COMPOSE_VER/docker-compose-linux-aarch64"
[ -s "$BUILD/docker-$DOCKER_VER.tgz" ] || { echo "下载 $DOCKER_URL"; curl -fL --retry 2 -o "$BUILD/docker-$DOCKER_VER.tgz" "$DOCKER_URL"; }
[ -s "$BUILD/docker-compose-linux-aarch64" ] || { echo "下载 $COMPOSE_URL"; curl -fL --retry 2 -o "$BUILD/docker-compose-linux-aarch64" "$COMPOSE_URL"; }

# ---- 3. 架构核验（本机只做核验，不安装） ----
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
tar -xzf "$BUILD/docker-$DOCKER_VER.tgz" -C "$TMP" docker/dockerd
file "$TMP/docker/dockerd" | grep -q "aarch64\|ARM aarch64" || { echo "错误：dockerd 非 aarch64" >&2; exit 1; }
file "$BUILD/docker-compose-linux-aarch64" | grep -q "aarch64\|ARM aarch64" || { echo "错误：compose 非 aarch64" >&2; exit 1; }
echo "架构核验：dockerd / compose 均 aarch64 ✓"

# ---- 4. 组包 ----
PKG="docker-offline-aarch64-$DOCKER_VER"
rm -rf "$STAGE"; mkdir -p "$STAGE/$PKG"
cp "$BUILD/docker-$DOCKER_VER.tgz" "$STAGE/$PKG/"
cp "$BUILD/docker-compose-linux-aarch64" "$STAGE/$PKG/"
cp "$HERE/docker.service" "$HERE/docker.socket" "$HERE/install.sh" "$HERE/README.md" "$STAGE/$PKG/"
(cd "$STAGE/$PKG" && $SHA_CMD docker-*.tgz docker-compose-linux-aarch64 docker.service docker.socket install.sh README.md > sha256sums.txt)
mkdir -p "$DIST"
COPYFILE_DISABLE=1 tar -cf "$DIST/$PKG.tar" -C "$STAGE" "$PKG"
(cd "$DIST" && $SHA_CMD "$PKG.tar" > "$PKG.tar.sha256")

# ---- 5. 产物报告 ----
echo
echo "== 产物 =="
ls -lh "$DIST/$PKG.tar" "$DIST/$PKG.tar.sha256"
echo "-- 包内容清单 --"
tar -tf "$DIST/$PKG.tar"
echo "-- 整包 sha256 --"
cat "$DIST/$PKG.tar.sha256"
