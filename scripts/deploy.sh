#!/usr/bin/env bash
# OneServe 生产部署脚本（目标 Linux 服务器（示例主机 10.0.0.1），Linux ARM64，root 执行）
# 用法：scripts/deploy.sh [--dry-run] [--services "service batch-worker"] [--skip-pull] [--no-smoke]
# 形态：docker-compose 三文件叠加（docker-compose.yml＝dev 基座 + compose.prod.yml＝prod 覆盖 +
#       deploy/compose.runtime-fixes.yml＝ECS 运行期兼容补丁），项目名 oneserve-prod。
# 步骤：①体检 → ②同步 compose 文件（绝不碰 .env）→ ③拉镜像 → ④up -d → ⑤冒烟；
#       任一步失败打印肇事容器最近 10 行日志＋人话处置建议（含手工回滚）后退出非 0。
#
# ── 为何必须 docker-compose v5.6.0 ＋ DOCKER_API_VERSION=1.39（2026-10-23 ECS 五病根之一）──
# 服务器两套 compose 并存：/usr/local/bin=v5.6.0、/usr/bin=v2.6.0。
#   - v2.6.0 不认 compose.prod.yml 的 volumes/ports !override 标签，叠加时会把 dev 基座的
#     源码 bind 挂载错拼进 prod 容器（prod「源码进镜像零 bind」被破坏且极难察觉）——只准用 v5.6。
#   - v5.6 客户端比 docker 18.09 守护进程新，不带 DOCKER_API_VERSION=1.39 会按自身新版本
#     协商 API 而被守护进程拒连——必须 export 后再调用。
# ── 为何有 runtime-fixes（五病根之二/之四）──
# docker 18.09 默认 seccomp profile 误杀新 glibc 镜像的线程创建（can't start new thread），
#   service/batch-worker 两容器需 seccomp=unconfined（原第三容器 casdoor-init 已随
#   无登录模式 ADR-0029 退役）；apisix 镜像
#   conf 目录属主 uid 1001 而默认用户 636 无权读，需 user: "1001:0"。docker 升级后按
#   deploy/compose.runtime-fixes.yml 头注的解除条件回收。
set -euo pipefail

PROJ_NAME=oneserve-prod
DEPLOY_DIR=/root/OneServe
DC_BIN=/usr/local/bin/docker-compose          # 只认 v5.6.0（头注；v2.6.0 会坏 !override）
PROBE_IMAGE="${ONESERVE_PROBE_IMAGE:-hello-world}"   # 开源批 B2：镜像官方源，探针改拉公共小镜像
# META_APP_PASSWORD 暂不查：元数据库（compose.metadata.yml 叠加层）尚未部署；
# 接入元数据库当天把该键加回本清单（ADR-0021 DR runbook §6 前置步会用到）
# 无登录模式（ADR-0029）：登录链凭证键（OIDC/CASDOOR/BOOTSTRAP）已全部移除。
REQUIRED_ENV_KEYS=(
  PROD_PUBLIC_ORIGIN
  APISIX_ADMIN_KEY
)
COMPOSE_FILES=(-f docker-compose.yml -f compose.prod.yml -f compose.runtime-fixes.yml)

DRY_RUN=0; SKIP_PULL=0; SMOKE=1; SERVICES=()

log()  { echo "[deploy] $*"; }
warn() { echo "[deploy][警告] $*" >&2; }
die()  { echo "[deploy][失败] $*" >&2; exit 1; }

usage() {
  cat <<'USAGE'
用法：deploy.sh [--dry-run] [--services "service batch-worker"] [--skip-pull] [--no-smoke]
  --dry-run    只打印将执行的动作（步骤①体检全只读，仍真实执行）
  --services   只部署指定服务（空格分隔的 compose 服务名）
  --skip-pull  跳过镜像拉取
  --no-smoke   跳过冒烟验证
USAGE
  exit 0
}

while (($#)); do
  case "$1" in
    --dry-run)   DRY_RUN=1 ;;
    --skip-pull) SKIP_PULL=1 ;;
    --no-smoke)  SMOKE=0 ;;
    --services)  shift; read -r -a SERVICES <<<"${1:?--services 需要服务名列表}" ;;
    -h|--help)   usage ;;
    *)           die "未知参数：$1（--help 看用法）" ;;
  esac
  shift
done

[ "$(id -u)" -eq 0 ] || die "本脚本须以 root 运行（要写 $DEPLOY_DIR 并操作 docker）"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# 扫描 oneserve-prod 容器健康态：不健康/异常退出者记入 bad_containers
# （原一次性容器 casdoor-init 的 Exited (0) 豁免已随无登录模式 ADR-0029 退役移除）。
scan_bad_containers() {
  bad_containers=()
  local name status total=0
  while IFS=$'\t' read -r name status; do
    [ -n "$name" ] || continue
    total=$((total + 1))
    if [[ "$status" != *"(healthy)"* ]]; then
      bad_containers+=("$name [$status]")
    fi
  done < <(docker ps -a --filter "label=com.docker.compose.project=$PROJ_NAME" \
             --format '{{.Names}}\t{{.Status}}')
  if [ "$total" -eq 0 ]; then
    bad_containers+=("（未发现 $PROJ_NAME 容器——栈未起来）")
  fi
}

# 最多等 120 秒全容器健康（起栈后 healthcheck 有 start_period，立刻查会误报 starting）。
wait_healthy() {
  local i
  for i in $(seq 1 12); do
    scan_bad_containers
    if [ "${#bad_containers[@]}" -eq 0 ]; then return 0; fi
    sleep 10
  done
  return 1
}

# 部署/冒烟失败出口：打印肇事容器日志＋人话处置建议（含手工回滚）。
fail_deploy() {
  echo "[deploy][失败] $*" >&2
  scan_bad_containers
  echo "[deploy] 异常容器：${bad_containers[*]}" >&2
  echo "[deploy] ── 肇事容器最近 10 行日志 ──" >&2
  local c name
  for c in "${bad_containers[@]}"; do
    case "$c" in （*) continue ;; esac   # 占位条目（栈未起来）无容器可查日志
    name="${c%% \[*}"
    echo "---- $name ----" >&2
    docker logs --tail 10 "$name" 2>&1 || true
  done
  cat >&2 <<'ADVICE'
[deploy] 处置建议（按序）：
 1) 先看上方日志定位报错。常见病因：
    - can't start new thread → seccomp 补丁没生效：确认 /root/OneServe/compose.runtime-fixes.yml
      已同步、up 命令带了第三个 -f；
    - 起栈即 Exited (1) → 多半 .env 缺键或值非法（步骤①核过，复核是否有手改痕迹）；
    - pull/up 报 manifest/架构错 → 镜像源认证/网络问题或镜像非 linux/arm64（ECS 是 ARM）。
 2) 单容器细看：docker logs oneserve-prod-<名>；进容器：docker exec -it oneserve-prod-<名> sh。
 3) 手工回滚（本脚本不自动回滚）：
    a. 把 /root/OneServe 下 docker-compose.yml、compose.prod.yml、compose.runtime-fixes.yml
       恢复为上一个可用版本（git checkout <旧commit> -- 这三个文件，或从开发机仓库拷旧版）；
    b. 镜像回退：优先用镜像仓库上一个提交短哈希 tag；没有则 docker images | grep oneserve
       找本机旧版镜像改 tag；
    c. 重跑部署：cd /root/OneServe && DOCKER_API_VERSION=1.39 /usr/local/bin/docker-compose
       -f docker-compose.yml -f compose.prod.yml -f compose.runtime-fixes.yml -p oneserve-prod up -d
ADVICE
  exit 1
}

# ═══ 步骤① 体检（全只读） ═══
log "步骤① 体检（docker/compose 版本、/data 磁盘、.env 键、镜像源连通）"

srv_ver="$(docker version --format '{{.Server.Version}}' 2>&1)" \
  || die "docker version 查询失败（守护进程未起？）：$srv_ver"
log "docker 守护进程版本：$srv_ver"
case "$srv_ver" in
  18.09*)
    warn "docker $srv_ver：默认 seccomp 会误杀新 glibc 镜像线程创建（本部署靠 runtime-fixes
补住）、API 停在 1.39（compose 必须带 DOCKER_API_VERSION）。升级到 ≥20.10 后按
deploy/compose.runtime-fixes.yml 头注回收补丁。"
    ;;
esac

if [ ! -x "$DC_BIN" ]; then
  if [ -x /usr/bin/docker-compose ]; then
    die "未找到 $DC_BIN（v5.6.0）。/usr/bin/docker-compose 是 v2.6.0——它不认 compose.prod.yml
的 !override 标签，会把 dev 源码挂载错拼进 prod 容器，禁止使用。请先恢复
/usr/local/bin/docker-compose v5.6.0 再重跑。"
  fi
  die "未找到 $DC_BIN（v5.6.0），也未找到任何 docker-compose——请先安装。"
fi
export DOCKER_API_VERSION=1.39   # v5.6 客户端 → docker 18.09 守护进程（API 1.39）的必备协商钉
dc_ver="$($DC_BIN --version 2>&1)" || die "docker-compose --version 执行失败：$dc_ver"
echo "$dc_ver" | grep -Eq 'version +v?[0-9]+\.[0-9]+' || die "docker-compose 版本输出异常：$dc_ver"
log "docker-compose：$dc_ver"

[ -d /data ] || die "/data 不存在（生产数据盘未挂？）"
avail_pct="$(df -P /data | awk 'NR==2 {t=$3+$4; if (t==0) print 100; else printf "%.1f", $4*100/t}')"
[ "${avail_pct%%.*}" -ge 15 ] || die "/data 可用空间 ${avail_pct}%（阈值 15%）——先清理旧镜像/日志再部署"
log "/data 可用：${avail_pct}%（阈值 15%）"

ENV_FILE="$DEPLOY_DIR/.env"
[ -f "$ENV_FILE" ] || die "缺 $ENV_FILE（.env 永不从仓库同步，须在服务器常驻）"
missing_keys=()
for k in "${REQUIRED_ENV_KEYS[@]}"; do
  grep -Eq "^${k}=.+" "$ENV_FILE" || missing_keys+=("$k")
done
[ "${#missing_keys[@]}" -eq 0 ] || die ".env 缺键或值为空：${missing_keys[*]}（对照仓内 .env.example 补齐）"
log ".env 必含键 ${#REQUIRED_ENV_KEYS[@]} 项齐全"

# 镜像源连通性：pull --quiet 探针（18.09 的 manifest inspect 需开 experimental，弃用）。
# 401 类＝私有仓认证问题（如配置了镜像仓库前缀），人话报错让运维处理后重跑
# ——按红线本脚本不自动登录。
if ! pull_out="$(docker pull "$PROBE_IMAGE" 2>&1)"; then
  case "$pull_out" in
    *nauthorized*|*"authentication required"*|*denied*|*"401"*)
      die "镜像源认证失败（$PROBE_IMAGE 被拒）。若使用私有镜像仓库，请运维在本机执行
  docker login <你的镜像仓库地址>
后重跑本脚本。按红线本脚本不做自动登录。" ;;
    *)
      die "镜像源探针未通过（非 401 类错误）。原始输出：
$pull_out" ;;
  esac
fi
log "镜像源连通（docker pull $PROBE_IMAGE 通过）"

# ═══ 步骤② 同步 compose 文件（绝不碰 .env） ═══
log "步骤② 同步 compose 文件（仓库 → $DEPLOY_DIR；.env 不在同步清单、永不覆盖）"
[ -d "$DEPLOY_DIR" ] || die "$DEPLOY_DIR 不存在（.env 应已就位于此）"
for f in docker-compose.yml compose.prod.yml deploy/compose.runtime-fixes.yml; do
  src="$REPO_DIR/$f"
  dst="$DEPLOY_DIR/$(basename "$f")"
  [ -f "$src" ] || die "仓库缺 $src——deploy.sh 须在 OneServe 仓库检出内运行"
  if [ "$DRY_RUN" -eq 1 ]; then
    log "[dry-run] cp $src → $dst"
  elif [ "$src" = "$dst" ]; then
    log "源即目标，跳过：$dst"
  elif cmp -s "$src" "$dst" ; then
    log "无变化，跳过：$dst"
  else
    cp "$src" "$dst"
    log "已更新：$dst"
  fi
done

# ═══ 步骤③ 拉取镜像 ═══
cd "$DEPLOY_DIR"   # compose 从当前目录读三文件与 .env
if [ "$SKIP_PULL" -eq 1 ]; then
  log "步骤③ 拉取镜像：--skip-pull 跳过"
else
  pull_args=("${COMPOSE_FILES[@]}" pull)
  if [ "${#SERVICES[@]}" -gt 0 ]; then pull_args+=("${SERVICES[@]}"); fi
  if [ "$DRY_RUN" -eq 1 ]; then
    log "[dry-run] $DC_BIN ${pull_args[*]}"
  else
    log "步骤③ 拉取镜像"
    "$DC_BIN" "${pull_args[@]}" || fail_deploy "镜像拉取失败（查 SWR 令牌/网络/镜像架构——ECS 为 ARM 须 linux/arm64）"
  fi
fi

# ═══ 步骤④ 起栈（三文件叠加） ═══
up_args=("${COMPOSE_FILES[@]}" -p "$PROJ_NAME" up -d)
if [ "${#SERVICES[@]}" -gt 0 ]; then up_args+=("${SERVICES[@]}"); fi
if [ "$DRY_RUN" -eq 1 ]; then
  log "[dry-run] cd $DEPLOY_DIR && DOCKER_API_VERSION=1.39 $DC_BIN ${up_args[*]}"
else
  log "步骤④ 起栈（项目 $PROJ_NAME，三文件叠加）"
  "$DC_BIN" "${up_args[@]}" || fail_deploy "up 失败"
fi

# ═══ 步骤⑤ 冒烟 ═══
if [ "$SMOKE" -eq 0 ]; then
  log "步骤⑤ 冒烟：--no-smoke 跳过"
elif [ "$DRY_RUN" -eq 1 ]; then
  log "[dry-run] 冒烟将检查：curl 127.0.0.1:20000＝200；curl -k https://127.0.0.1:20443＝200；"
  log "[dry-run] 容器内 healthz＝200；oneserve-prod 全容器 healthy"
else
  log "步骤⑤ 冒烟"
  code="$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:20000 || echo 000)"
  [ "$code" = "200" ] || fail_deploy "门户 http://127.0.0.1:20000 期望 200，实际 $code"
  code="$(curl -sk -o /dev/null -w '%{http_code}' https://127.0.0.1:20443 || echo 000)"
  [ "$code" = "200" ] || fail_deploy "门户 https://127.0.0.1:20443 期望 200，实际 $code（自签证书 -k 已忽略）"
  code="$(docker exec oneserve-prod-service curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8000/healthz || echo 000)"
  [ "$code" = "200" ] || fail_deploy "service 容器内 healthz 期望 200，实际 $code"
  if ! wait_healthy; then
    fail_deploy "容器未达健康（等满 120 秒）"
  fi
  log "冒烟全绿：门户 20000/20443＝200、容器内 healthz＝200、$PROJ_NAME 全容器 healthy"
fi

log "完成 ✅"
