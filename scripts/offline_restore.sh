#!/usr/bin/env bash
# OneServe 离线包目标机恢复脚本：校验 sha256 → docker load → 提示 compose up。
#
# 用法（在解包目录内执行，或传解包目录参数）：
#   bash scripts/offline_restore.sh            # 已在 oneserve-offline-xxx/ 目录内
#   bash scripts/offline_restore.sh <解包目录>
#
# 前提：交付时 tar、.tar.sha256 与本解包目录三者同处一个目录（校验用）。
# 兼容 macOS 自带 bash 3.2 与 Linux bash；不依赖 git。
set -euo pipefail
export PATH="$PATH:$HOME/.docker/bin"

PKG_DIR="${1:-.}"
cd "$PKG_DIR" || { echo "错误：解包目录不存在：$PKG_DIR" >&2; exit 1; }
PKG_DIR=$(pwd)

if ! command -v docker >/dev/null 2>&1; then
  echo "错误：找不到 docker 命令（目标机需先安装 Docker + Docker Compose 插件）" >&2
  exit 1
fi

if [ ! -f images/all.tar ]; then
  echo "错误：当前目录不是有效解包目录（缺 images/all.tar）：$PKG_DIR" >&2
  echo "正确姿势：sha256sum -c 包名.tar.sha256 && tar xf 包名.tar && cd 包名目录 && bash scripts/offline_restore.sh ." >&2
  exit 1
fi

# ---------- 1. 校验整包 sha256（找到即强制校验，失败禁止继续） ----------
BASE=$(basename "$PKG_DIR")          # 形如 oneserve-offline-YYYYMMDD-哈希
SHA_FILE=""
for cand in "../${BASE}.tar.sha256" "./${BASE}.tar.sha256"; do
  if [ -f "$cand" ]; then SHA_FILE="$cand"; break; fi
done
if [ -z "$SHA_FILE" ]; then
  for m in ../*.tar.sha256; do      # 兜底：上级目录任意 .tar.sha256
    if [ -f "$m" ]; then SHA_FILE="$m"; break; fi
  done
fi

if command -v sha256sum >/dev/null 2>&1; then
  CHECKCMD="sha256sum"
elif command -v shasum >/dev/null 2>&1; then
  CHECKCMD="shasum -a 256"
else
  CHECKCMD=""
fi

if [ -n "$SHA_FILE" ] && [ -f "${SHA_FILE%.sha256}" ]; then
  if [ -z "$CHECKCMD" ]; then
    echo "错误：本机无 sha256sum / shasum，无法完成完整性校验（安全要求，不放行）" >&2
    exit 1
  fi
  echo "== 1/3 校验整包 sha256（${SHA_FILE}；大文件约需 1-2 分钟）=="
  ( cd "$(dirname "$SHA_FILE")" && $CHECKCMD -c "$(basename "$SHA_FILE")" ) || {
    echo "错误：sha256 校验失败！包传输损坏或被篡改，禁止继续，请重新拷贝 tar 包" >&2
    exit 1
  }
elif [ -n "$SHA_FILE" ]; then
  echo "警告：找到 $SHA_FILE 但对应 tar 本体不在旁边（应 tar+sha256 一起放），跳过整包校验" >&2
else
  echo "警告：未找到 <包名>.tar.sha256，跳过完整性校验（生产交付不允许省略此步）" >&2
fi

# ---------- 2. docker load ----------
echo "== 2/3 导入镜像（docker load -i images/all.tar，视磁盘速度约数分钟）=="
if ! docker load -i images/all.tar; then
  echo "错误：docker load 失败（检查磁盘空间 df -h 与 docker 服务状态 systemctl status docker）" >&2
  exit 1
fi

# ---------- 3. 准备 .env 并给出启动命令（不自动 up：首次部署先让运维确认配置） ----------
COMPOSE_FILE="$(ls compose/docker-compose.yml 2>/dev/null | head -1 || true)"
if [ -z "$COMPOSE_FILE" ]; then
  COMPOSE_FILE="$(ls compose/*.yml 2>/dev/null | head -1 || true)"
fi
echo
echo "== 3/3 镜像导入完成，准备启动 =="
if [ ! -f "$PKG_DIR/.env" ]; then
  if [ -f env/.env.example ]; then
    cp env/.env.example "$PKG_DIR/.env"
    echo "已从 env/.env.example 生成 $PKG_DIR/.env（如需改 AppKey 等请先编辑该文件）"
  else
    echo "警告：包内无 env/.env.example，请手动准备 .env" >&2
  fi
fi
if [ -n "$COMPOSE_FILE" ]; then
  echo
  echo "复制执行以下四条命令即可起全套（顺序固定；v3 起第 4 步灌演示数据）："
  echo
  echo "  cd $PKG_DIR/compose"
  echo "  docker compose -f $(basename "$COMPOSE_FILE") --env-file $PKG_DIR/.env up -d   # ① 起栈（等六容器 healthy）"
  echo "  docker compose -f $(basename "$COMPOSE_FILE") exec -T service python /init/init_routes.py   # ② 网关路由初始化（7×201）"
  echo "  cd $PKG_DIR && bash scripts/ecs_seed.sh    # ③ 灌演示数据（首页 KPI/目录/试用台/运行视图真数据＋自检）"
  echo
  echo "验证：curl -i http://localhost:9080/v1/ping                （401＝网关正常）"
  echo "带 key：见 docs/运维手册-一页纸_v0.md（从 .env 读 APISIX_APPKEY）"
else
  echo "警告：compose/ 下未找到编排文件，请人工确认包完整性" >&2
fi
