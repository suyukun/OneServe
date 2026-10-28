#!/usr/bin/env bash
# OneServe 离线交付打包脚本（国企弱运维环境：产出单个 tar 包 + sha256 + manifest）。
#
# 用法（仓库根或任意目录均可）：
#   bash scripts/offline_pack.sh                 # 只打主编排 docker-compose.yml
#   COMPOSE_FILES="docker-compose.yml compose.sync.yml" bash scripts/offline_pack.sh
#
# 产物（dist/ 下）：
#   oneserve-offline-<YYYYMMDD>-<git短哈希>.tar        离线包
#   oneserve-offline-<YYYYMMDD>-<git短哈希>.tar.sha256 校验文件（sha256sum -c 通用格式）
#
# tar 内结构（解压后得到 oneserve-offline-<日期>-<哈希>/ 一个目录）：
#   images/all.tar            全部镜像（docker load -i images/all.tar 一次导入）
#   compose/                  编排文件副本 + compose bind 挂载文件（原相对布局内置，
#                             如 configs/ portal/ sync/ service/services.yaml——包内 up
#                             直接可用，修 v1 缺口：挂载文件缺失导致干净机起栈 unhealthy）
#   env/.env.example          环境变量模板副本（真实 .env 不入包，密钥不进包）
#   scripts/                  offline_pack.sh + offline_restore.sh（包内自带恢复脚本）
#   docs/运维手册-一页纸_v0.md 运维手册（若仓库里存在）
#   manifest.md               镜像清单（名称/tag/digest/大小）+ SBOM 或降级说明 + 恢复步骤
#   sbom/*.json               （可选）syft 生成的 SBOM；本机无 syft 时缺省并在 manifest 说明
#
# 兼容 macOS 自带 bash 3.2（不用关联数组 / mapfile / ${var,,} 等 bash4 语法）。
# 服务与镜像一律「读 compose 实际列表」（docker compose config --images），不硬编码；
# 挂载文件清单同样从 docker compose config 实际解析（bind 卷 source），不硬编码；
# APISIX 改 standalone 去掉 etcd 后本脚本无需修改。
# 依赖：docker + compose 插件 + python3（解析 compose config JSON；仅打包机需要）。
set -euo pipefail
export PATH="$PATH:$HOME/.docker/bin"

# ---------- 定位仓库根 ----------
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
REPO_ROOT=$(cd "$SCRIPT_DIR/.." && pwd)
cd "$REPO_ROOT"

if ! command -v docker >/dev/null 2>&1; then
  echo "错误：找不到 docker 命令（先启动 Docker 引擎再打包）" >&2
  exit 1
fi

# ---------- 要打包的 compose 文件（空格分隔，可经环境变量覆盖） ----------
if [ -z "${COMPOSE_FILES:-}" ]; then
  COMPOSE_FILES="docker-compose.yml"
fi
for f in $COMPOSE_FILES; do
  if [ ! -f "$f" ]; then
    echo "错误：compose 文件不存在：$f" >&2
    exit 1
  fi
done

# ---------- 从 compose 读实际镜像列表（去重、保序；bash3 用字符串判重） ----------
IMAGES=""
for f in $COMPOSE_FILES; do
  dir=$(dirname "$f"); base=$(basename "$f")
  list=$(cd "$dir" && docker compose -f "$base" config --images 2>/dev/null) || {
    echo "错误：docker compose -f $f config --images 失败（compose 无效？需先 build 的服务请先构建）" >&2
    exit 1
  }
  for img in $list; do
    case " $IMAGES " in
      *" $img "*) : ;;                       # 已收录，跳过
      *) IMAGES="$IMAGES $img" ;;
    esac
  done
done
IMAGES=$(echo "$IMAGES" | tr " " "\n" | sed "/^$/d")
IMG_COUNT=$(echo "$IMAGES" | grep -c . || true)
if [ "$IMG_COUNT" = "0" ]; then
  echo "错误：compose 未解析出任何镜像" >&2
  exit 1
fi

# ---------- 镜像必须已在本机（缺失即明确报错，绝不静默跳过） ----------
MISSING=""
while IFS= read -r img; do
  [ -n "$img" ] || continue
  docker image inspect "$img" >/dev/null 2>&1 || MISSING="$MISSING $img"
done <<EOF_MISSING
$IMAGES
EOF_MISSING
if [ -n "$MISSING" ]; then
  # compose 里带 build: 的服务自动补构建一次；仍缺则明确报错退出（不静默跳过）
  BUILDABLE=""
  for f in $COMPOSE_FILES; do
    dir=$(dirname "$f"); base=$(basename "$f")
    bl=$(cd "$dir" && docker compose -f "$base" config --services 2>/dev/null) || bl=""
    for svc in $bl; do
      if (cd "$dir" && docker compose -f "$base" build "$svc" >/dev/null 2>&1); then :
      else echo "警告：compose $f 的 $svc 构建失败或无 build 段" >&2; fi
    done
  done
  MISSING=""
  while IFS= read -r img; do
    [ -n "$img" ] || continue
    docker image inspect "$img" >/dev/null 2>&1 || MISSING="$MISSING $img"
  done <<EOF_MISSING2
$IMAGES
EOF_MISSING2
  if [ -n "$MISSING" ]; then
    echo "错误：以下镜像本机不存在且自动构建未补齐，先手动处理再打包：" >&2
    for m in $MISSING; do echo "  - $m" >&2; done
    echo "提示：可执行 docker compose build 或 bash scripts/up.sh 后重试" >&2
    exit 1
  fi
  echo "已自动构建缺失的本地镜像" >&2
fi

# ---------- 版本标识与暂存目录 ----------
DATE=$(date +%Y%m%d)
GIT_SHORT=$(git rev-parse --short HEAD 2>/dev/null || echo "nogit")
PKG_NAME="oneserve-offline-${DATE}-${GIT_SHORT}"
STAGE="$REPO_ROOT/dist/${PKG_NAME}"
mkdir -p "$REPO_ROOT/dist"
rm -rf "$STAGE"
mkdir -p "$STAGE/images" "$STAGE/compose" "$STAGE/env" "$STAGE/scripts" "$STAGE/docs" "$STAGE/sbom"

echo "== 打包对象：${IMG_COUNT} 个镜像 =="
echo "$IMAGES" | sed "s/^/  - /"

# ---------- 批量 save 为一个 all.tar（任一失败立即退出） ----------
SAVE_LIST=""
while IFS= read -r img; do
  [ -n "$img" ] || continue
  SAVE_LIST="$SAVE_LIST $img"
done <<EOF_SAVE
$IMAGES
EOF_SAVE
# shellcheck disable=SC2086
if ! docker save $SAVE_LIST -o "$STAGE/images/all.tar"; then
  echo "错误：docker save 失败" >&2
  rm -rf "$STAGE"
  exit 1
fi

# ---------- 复制 compose / .env.example / 脚本 / 手册 ----------
for f in $COMPOSE_FILES; do cp "$f" "$STAGE/compose/$(basename "$f")"; done

# ---------- compose bind 挂载文件随包内置（v2：修 v1 干净机起栈 unhealthy 的缺口） ----------
# docker compose 解析 bind 挂载 source 的基准＝compose 文件所在目录；包内 compose 位于
# oneserve-offline-*/compose/，故挂载文件必须按「原相对路径」放进 compose/ 下，目标机 up 才能找到。
# 清单从 docker compose config 实际解析（type=bind 的 source），不硬编码；目录先拷、子路径自动去重。
TAB=$(printf "\t")
MOUNT_PAIRS=""                      # 每行：<挂载源绝对路径><TAB><相对 compose 目录路径>
for f in $COMPOSE_FILES; do
  fdir=$(cd "$(dirname "$f")" && pwd); fbase=$(basename "$f")
  mnts=$(cd "$fdir" && docker compose -f "$fbase" config --format json 2>/dev/null | python3 -c '
import json, sys
cfg = json.load(sys.stdin)
srcs = set()
for svc in (cfg.get("services") or {}).values():
    for v in (svc.get("volumes") or []):
        if isinstance(v, dict) and v.get("type") == "bind" and v.get("source"):
            srcs.add(v["source"])
print("\n".join(sorted(srcs)))
') || { echo "错误：解析 $f 的 bind 挂载清单失败（python3 缺失或 compose 无效）" >&2; exit 1; }
  while IFS= read -r src; do
    [ -n "$src" ] || continue
    if [ ! -e "$src" ]; then
      echo "错误：compose 挂载源不存在：$src（先补齐仓库文件再打包）" >&2
      exit 1
    fi
    case "$src" in
      "$fdir"/*) rel=${src#"$fdir"/} ;;
      *) echo "错误：挂载源在 compose 目录之外，无法按包内相对布局内置：$src" >&2; exit 1 ;;
    esac
    MOUNT_PAIRS="$MOUNT_PAIRS$src$TAB$rel
"
  done <<EOF_MNT
$mnts
EOF_MNT
done
# 去重（同一挂载源被多个 compose 文件引用只留一份）＋按相对路径排序（父目录必先于其子路径）
MOUNT_UNIQUE=$(printf "%s" "$MOUNT_PAIRS" | sed "/^$/d" | sort -u | sort -t"$TAB" -k2,2)
MOUNT_COPIED=""
while IFS="$TAB" read -r src rel; do
  [ -n "$rel" ] || continue
  skip=""
  for done_rel in $MOUNT_COPIED; do
    case "$rel" in "$done_rel"/*) skip=1 ;; esac
  done
  [ -n "$skip" ] && continue
  mkdir -p "$STAGE/compose/$(dirname "$rel")"
  cp -R "$src" "$STAGE/compose/$rel"
  echo "已内置挂载：compose/$rel"
  MOUNT_COPIED="$MOUNT_COPIED $rel"
done <<EOF_MPAIRS
$MOUNT_UNIQUE
EOF_MPAIRS
# OS/Python 缓存杂物不入包
find "$STAGE/compose" \( -name .DS_Store -o -name __pycache__ -o -name "*.pyc" \) -exec rm -rf {} + 2>/dev/null || true

if [ -f .env.example ]; then cp .env.example "$STAGE/env/.env.example"; else
  echo "警告：仓库无 .env.example（manifest 中会标注）" >&2
fi
cp "$SCRIPT_DIR/offline_pack.sh" "$STAGE/scripts/"
if [ -f "$SCRIPT_DIR/offline_restore.sh" ]; then
  cp "$SCRIPT_DIR/offline_restore.sh" "$STAGE/scripts/"
else
  echo "错误：scripts/offline_restore.sh 不存在（须与打包脚本同在）" >&2
  rm -rf "$STAGE"; exit 1
fi
if [ -f "$SCRIPT_DIR/ecs_seed.sh" ]; then        # v3：演示数据灌数脚本随包（ECS 起栈后一键灌数）
  cp "$SCRIPT_DIR/ecs_seed.sh" "$STAGE/scripts/"
else
  echo "错误：scripts/ecs_seed.sh 不存在（v3 起随包内置，须与打包脚本同在）" >&2
  rm -rf "$STAGE"; exit 1
fi
if [ -f "$REPO_ROOT/docs/plans/运维手册-一页纸_v0.md" ]; then
  cp "$REPO_ROOT/docs/plans/运维手册-一页纸_v0.md" "$STAGE/docs/"
fi

# ---------- manifest.md：镜像清单（名称/tag/digest/大小） ----------
MANIFEST="$STAGE/manifest.md"
BT=$(printf "\140")           # BT＝一个反引号（printf 八进制 140＝ASCII 96；bash3 通用）
BQ="$BT$BT$BT"            # markdown 代码围栏＝三个反引号
{
  echo "# OneServe 离线交付包 · ${PKG_NAME}"
  echo
  echo "- 打包时间：$(date "+%Y-%m-%d %H:%M:%S %z")"
  echo "- 源仓库：$REPO_ROOT @ git ${GIT_SHORT}"
  echo "- compose 文件：$COMPOSE_FILES"
  echo "- 打包模式：预构建交付（目标机不构建、不联网拉镜像）"
  echo
  echo "## 镜像清单"
  echo
  echo "| 镜像（name:tag） | 架构 | digest | 大小(MB) |"
  echo "|---|---|---|---|"
} > "$MANIFEST"
while IFS= read -r img; do
  [ -n "$img" ] || continue
  name=${img%:*}; tag=${img##*:}
  digests=$(docker image inspect --format "{{json .RepoDigests}}" "$img" 2>/dev/null || echo "[]")
  size_bytes=$(docker image inspect --format "{{.Size}}" "$img")
  size_mb=$(( size_bytes / 1000 / 1000 ))
  dg=$(echo "$digests" | sed -e 's/.*@//' -e 's/".*//')
  if [ -z "$dg" ] || [ "$dg" = "$digests" ]; then dg="(本地构建，无上游 digest)"; fi
  arch=$(docker image inspect -f "{{.Architecture}}" "$img")
  echo "| ${name}:${tag} | ${arch} | ${BT}${dg}${BT} | ${size_mb} |" >> "$MANIFEST"
done <<EOF_MANIFEST_IMG
$IMAGES
EOF_MANIFEST_IMG

# ---------- manifest：compose 挂载文件清单（v2 新增，随包内置） ----------
{
  echo
  echo "## Compose 挂载文件（已随包内置）"
  echo
  echo "包内 compose 在 compose/ 目录，bind 挂载相对该目录解析；以下文件已按仓库内原相对布局内置，"
  echo "目标机无需再从仓库拷贝（v1 包缺这些文件，干净机起栈 apisix/service/portal 必 unhealthy）。"
  echo
  echo "| 包内路径 | 类型 | 大小 | 文件数 |"
  echo "|---|---|---|---|"
} >> "$MANIFEST"
if [ -z "$MOUNT_COPIED" ]; then
  echo "| （本包 compose 无 bind 挂载） | - | - | - |" >> "$MANIFEST"
else
  for rel in $MOUNT_COPIED; do
    p="$STAGE/compose/$rel"
    if [ -d "$p" ]; then
      msize=$(du -sh "$p" | cut -f1); nfiles=$(find "$p" -type f | wc -l | tr -d " "); mtype="目录"
    else
      msize=$(ls -lh "$p" | awk "{print \$5}"); nfiles=1; mtype="文件"
    fi
    echo "| compose/$rel | ${mtype} | ${msize} | ${nfiles} |" >> "$MANIFEST"
  done
fi

# ---------- manifest：数据种子与灌数（v3 新增） ----------
{
  echo
  echo "## 数据种子与灌数（v3 新增）"
  echo
  echo "演示数据不静态内置，由包内 scripts/ecs_seed.sh 在目标机 compose 网内现场生成＋灌入"
  echo "（全程 docker compose exec，ECS 无外网/无仓库副本可跑；seed=42 确定性，与彩排链同参数同数据）："
  echo
  echo "- 用户标签 KV：2000 户 × 296 列（gen_mock_source → loader → reconcile 对账三查 → switch 切指针）；"
  echo "- 资讯标签 KV：1000 条（gen_news_data 直灌＋切指针）；"
  echo "- 公告种子：list portal:announcements；调用量 KPI 不灌种子——service/app/stats.py"
  echo "  挂审计链路实时计数（hash portal:stats:live＋日滚动键，网关即拒的 401 也计），"
  echo "  无调用如实显示 0（诚信线 2026-09-22：数字必须真）；"
  echo "- 门户数据面：服务镜像内置 /v1/portal/* 只读端点（service/app/portal_api.py），门户 nginx"
  echo "  （configs/nginx/portal-nginx.conf，已按 v2 挂载机制随包内置）同源反代 /v1/portal/ 与 /demo——"
  echo "  目录页／试用台／新鲜度＝种子真值，首页/运行视图 KPI＝实时真计数（无调用显示 0）。"
  echo
  echo "起栈顺序固定（缺一步都有空页/404）："
  echo
  echo "$BQ"
  echo "bash scripts/offline_restore.sh .                                   # 1. 校验＋导入镜像（本节上文）"
  echo "cd compose && docker compose -f docker-compose.yml --env-file ../.env up -d   # 2. 起栈，等六容器 healthy"
  echo "docker compose -f docker-compose.yml exec -T service python /init/init_routes.py   # 3. 网关路由初始化（7×201）"
  echo "cd .. && bash scripts/ecs_seed.sh                                  # 4. 一键灌数＋端到端自检（尾行打印演示手机号）"
  echo "$BQ"
  echo
  echo "ecs_seed.sh 幂等可重跑；down -v 清卷后需重跑第 4 步。"
} >> "$MANIFEST"

# ---------- SBOM：有 syft 则生成，无则降级为 docker images 快照 + 说明 ----------
if command -v syft >/dev/null 2>&1; then
  {
    echo
    echo "## SBOM（syft 生成）"
    echo
    echo "每镜像一份 JSON 在 sbom/ 目录："
  } >> "$MANIFEST"
  while IFS= read -r img; do
    [ -n "$img" ] || continue
    safe=$(echo "$img" | tr "/:" "__")
    if syft "docker:$img" -o json > "$STAGE/sbom/${safe}.json" 2>/dev/null; then
      echo "- sbom/${safe}.json ← $img" >> "$MANIFEST"
    else
      rm -f "$STAGE/sbom/${safe}.json"
      echo "- (生成失败) $img" >> "$MANIFEST"
    fi
  done <<EOF_SBOM
$IMAGES
EOF_SBOM
else
  {
    echo
    echo "## SBOM（降级说明）"
    echo
    echo "打包机未安装 syft，按交付约定降级为下方 docker images 快照清单（不强制安装）。"
    echo "如需完整 SBOM：在打包机安装 syft（brew install syft）后重跑本脚本即自动附带。"
    echo
    echo "$BQ"
    docker images
    echo "$BQ"
  } >> "$MANIFEST"
fi

# ---------- manifest 收尾：包结构 + 目标机恢复步骤 ----------
{
  echo
  echo "## 包内结构"
  echo
  echo "$BQ"
  echo "images/all.tar        全部镜像（单次 docker load 导入）"
  echo "compose/              编排文件副本 + bind 挂载文件（原相对布局内置，见「Compose 挂载文件」表）"
  echo "env/.env.example      环境变量模板（真实 .env 不入包）"
  echo "scripts/              offline_pack.sh / offline_restore.sh / ecs_seed.sh（v3 演示数据灌数）"
  echo "docs/                 运维手册-一页纸_v0.md（若打包时存在）"
  echo "sbom/                 syft SBOM（无 syft 时为空目录，见上方降级说明）"
  echo "manifest.md           本文件"
  echo "$BQ"
  echo
  echo "## 目标机恢复（顺序固定四步，详见「数据种子与灌数」）"
  echo
  echo "$BQ"
  echo "sha256sum -c <包名>.tar.sha256         # 1. 校验完整性＋解包"
  echo "tar xf <包名>.tar"
  echo "cd oneserve-offline-<日期>-<哈希>"
  echo "bash scripts/offline_restore.sh .      # 2. 一键恢复（load + 打印起栈命令）"
  echo "# 起栈后补两步（restore 尾段 3/3 亦打印）："
  echo "cd compose && docker compose -f docker-compose.yml --env-file ../.env up -d   # 3. 起栈（六容器 healthy 后）"
  echo "docker compose -f docker-compose.yml exec -T service python /init/init_routes.py   #    网关路由初始化（7×201）"
  echo "cd .. && bash scripts/ecs_seed.sh      # 4. 演示数据一键灌入（门户 KPI/试用台全真数据）"
  echo "$BQ"
  echo
  echo "或手动：docker load -i images/all.tar 后，进 compose/ 目录，"
  echo "cp ../env/.env.example ../.env（按需修改），再 docker compose -f docker-compose.yml up -d。"
} >> "$MANIFEST"

# ---------- 打 tar + sha256 ----------
cd "$REPO_ROOT/dist"
rm -f "${PKG_NAME}.tar" "${PKG_NAME}.tar.sha256"
tar cf "${PKG_NAME}.tar" "${PKG_NAME}"
rm -rf "$STAGE"

if command -v sha256sum >/dev/null 2>&1; then
  SHACMD="sha256sum"
elif command -v shasum >/dev/null 2>&1; then
  SHACMD="shasum -a 256"
else
  echo "错误：本机无 sha256sum / shasum，无法生成校验文件" >&2
  exit 1
fi
$SHACMD "${PKG_NAME}.tar" > "${PKG_NAME}.tar.sha256"

TAR_SIZE_HUMAN=$(ls -lh "${PKG_NAME}.tar" | awk "{print \$5}")
echo
echo "== 打包完成 =="
echo "包：      dist/${PKG_NAME}.tar (${TAR_SIZE_HUMAN})"
echo "校验：    dist/${PKG_NAME}.tar.sha256"
echo "manifest：包内 manifest.md（镜像清单 + SBOM/降级说明）"
echo "下一步：  scp dist/${PKG_NAME}.tar* 到目标机，按 docs/plans/运维手册-一页纸_v0.md 操作"
