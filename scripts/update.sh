#!/usr/bin/env bash
# ============================================================================
# OneServe 测试环境一键更新（scripts/update.sh）
#
# 【适用环境】测试环境 ECS：git 工作副本 + docker compose 生产形态栈，
#   即 docker-compose.yml + compose.prod.yml + compose.hbase.yml
#   + compose.kafka.yml 四文件叠加，项目名 oneserve-prod。
#   ★ 生产环境禁止使用本脚本：无备份、无灰度、无回滚步骤，仅面向测试环境。
#   ★ 全程只操作 oneserve-prod 项目资源，不碰 dev 共享栈（oneserve-*）。
#
# 【用法】
#   ./scripts/update.sh              正式更新
#   ./scripts/update.sh --dry-run    演练：只打印将执行的步骤，不做任何变更
#   前置：仓库已配置 origin 并完成首次 clone；.env 已就位（含 PROD_BIND_IP 等）。
#
# 【流程】
#   ① git pull --ff-only：已跟踪文件有本地改动 → 提示并中止（不 merge、不强推）；
#      未跟踪文件仅提示不阻塞（运行期产物不影响更新）
#   ② docker compose build：增量重建有变化的镜像（缓存命中近零开销）
#   ③ docker compose up -d：只重建有差异的容器（滚动替换）
#   ④ 等健康：docker ps 中 oneserve-prod 全部运行容器——有 healthcheck 的必须
#      healthy，无 healthcheck 的 Up 即算就绪（一次性 init 容器跑完即退）；
#      出现 unhealthy 立即失败；超时 300 秒报错退出
#   ⑤ 打印更新摘要：前后 commit、耗时、容器状态表
#
# 【幂等】可重复执行：pull 幂等、build 走缓存、up 只动有差异的容器。
# 【失败】任何一步失败即停，并给出「下一步」提示；修复后直接重跑本脚本。
# ============================================================================
set -euo pipefail
export PATH="$PATH:$HOME/.docker/bin"

PROJECT="oneserve-prod"
COMPOSE_FILES=(docker-compose.yml compose.prod.yml compose.hbase.yml compose.kafka.yml)
HEALTH_TIMEOUT_SECS=300
HEALTH_INTERVAL_SECS=5
PROGRESS_EVERY_SECS=30

cd "$(dirname "$0")/.."

case "${1:-}" in
  "")        DRY_RUN=0 ;;
  --dry-run) DRY_RUN=1 ;;
  *) echo "[update] 未知参数：$1（支持 --dry-run；用法见脚本头注释）" >&2; exit 2 ;;
esac

step() { printf '\n== %s ==\n' "$*"; }
die()  { printf '[update] ✗ %s\n' "$1" >&2; printf '[update] 下一步：%s\n' "$2" >&2; exit 1; }

COMPOSE=(docker compose)
for f in "${COMPOSE_FILES[@]}"; do COMPOSE+=(-f "$f"); done
COMPOSE+=(-p "$PROJECT")
# 人话提示里可整体复制的排查命令前缀
COMPOSE_CMD="docker compose -f docker-compose.yml -f compose.prod.yml -f compose.hbase.yml -f compose.kafka.yml -p $PROJECT"

run_or_print() {
  if [ "$DRY_RUN" -eq 1 ]; then
    printf '[dry-run] 将执行：%s\n' "$*"
  else
    "$@"
  fi
}

fmt_secs() { printf '%d分%02d秒' $(($1 / 60)) $(($1 % 60)); }

START=$SECONDS
echo "栈：${COMPOSE[*]}"
if [ "$DRY_RUN" -eq 1 ]; then
  echo "[dry-run] 演练模式：不做任何变更（不 pull、不 build、不碰容器）"
fi

# ---- ① git pull ------------------------------------------------------------
step "① git pull（--ff-only；脏改动中止，不 merge 不强推）"
BEFORE_COMMIT=$(git rev-parse --short HEAD)
dirty=$(git status --porcelain | grep -v '^??' || true)
untracked=$(git status --porcelain | grep '^??' || true)
if [ "$DRY_RUN" -eq 1 ]; then
  if [ -n "$dirty" ]; then
    echo "[dry-run] 检测到已跟踪文件有本地改动（正式执行将在本步中止）："
    echo "$dirty"
  else
    echo "[dry-run] 工作区（已跟踪文件）干净"
  fi
  printf '[dry-run] 将执行：git pull --ff-only\n'
elif [ -n "$dirty" ]; then
  echo "以下已跟踪文件有未提交改动："
  echo "$dirty"
  die "git pull 前工作区必须干净。" \
      "人工处理改动后重跑：git stash（先暂存）或 git commit（先提交）；本脚本不做 merge、不强推。"
else
  if [ -n "$untracked" ]; then
    echo "（未跟踪文件不阻塞 pull，仅供知悉）"
    echo "$untracked"
  fi
  git pull --ff-only \
    || die "git pull 失败（网络/凭证/历史分叉）。" \
       "检查 origin 可达与凭证；若本地已分叉，git fetch 后用 git log HEAD..origin/main 人工对比决断，处理完重跑本脚本。"
fi
AFTER_COMMIT=$(git rev-parse --short HEAD)
if [ "$BEFORE_COMMIT" = "$AFTER_COMMIT" ]; then
  echo "commit：${BEFORE_COMMIT}（无新提交）"
else
  echo "commit：$BEFORE_COMMIT → $AFTER_COMMIT"
fi

# ---- ② build ---------------------------------------------------------------
step "② 构建镜像（增量；无变化则全部缓存命中）"
run_or_print "${COMPOSE[@]}" build

# ---- ③ up -d ---------------------------------------------------------------
step "③ 滚动替换（up -d；只重建有差异的容器）"
run_or_print "${COMPOSE[@]}" up -d

# ---- ④ 等健康 ---------------------------------------------------------------
step "④ 等待健康（docker ps 全 healthy，超时 $HEALTH_TIMEOUT_SECS 秒）"
if [ "$DRY_RUN" -eq 1 ]; then
  printf '[dry-run] 将执行：每 %ss 轮询 docker ps --filter label=com.docker.compose.project=%s\n' "$HEALTH_INTERVAL_SECS" "$PROJECT"
  echo "[dry-run]   通过条件：全部运行容器 healthy（无 healthcheck 的 Up 即算）；"
  echo "[dry-run]   出现 unhealthy 立即失败；超 ${HEALTH_TIMEOUT_SECS}s 报错退出"
else
  deadline=$((SECONDS + HEALTH_TIMEOUT_SECS))
  last_beat=$SECONDS
  while :; do
    rows=$(docker ps --filter "label=com.docker.compose.project=$PROJECT" --format '{{.Names}}\t{{.Status}}') \
      || die "docker ps 执行失败。" \
         "确认 docker 守护进程正常（systemctl status docker）；修复后重跑本脚本。"
    if [ -z "$rows" ]; then
      die "项目 $PROJECT 下没有运行中的容器。" \
         "$COMPOSE_CMD ps -a 排查为何未起来；处理后重跑本脚本。"
    fi
    total=0; ready=0; bad=""; pending=""
    while IFS=$'\t' read -r name status; do
      [ -z "$name" ] && continue
      total=$((total + 1))
      case "$status" in
        *'(healthy)'*)        ready=$((ready + 1)) ;;
        *'(unhealthy)'*)      bad="$bad $name" ;;
        *'health: starting'*) pending="$pending $name" ;;
        *Up*)                 ready=$((ready + 1)) ;;
        *)                    pending="$pending $name" ;;
      esac
    done <<< "$rows"
    if [ -n "$bad" ]; then
      echo "$rows"
      die "容器不健康：$bad" \
         "$COMPOSE_CMD logs <容器名> 看日志定位；修复后重跑本脚本（幂等）。"
    fi
    if [ "$ready" -eq "$total" ]; then break; fi
    if [ "$SECONDS" -ge "$deadline" ]; then
      echo "$rows"
      die "等待健康超时（>$HEALTH_TIMEOUT_SECS 秒）：就绪 $ready/${total}，未就绪：$pending" \
         "$COMPOSE_CMD ps 与 $COMPOSE_CMD logs <容器名> 定位卡点；修复后重跑本脚本（幂等）。"
    fi
    if [ $((SECONDS - last_beat)) -ge "$PROGRESS_EVERY_SECS" ]; then
      echo "  等待中：就绪 $ready/${total}，待：${pending}（已等 $(fmt_secs $((SECONDS - START)))）"
      last_beat=$SECONDS
    fi
    sleep "$HEALTH_INTERVAL_SECS"
  done
  echo "全部就绪：$ready/${total}（healthy/Up）"
fi

# ---- ⑤ 摘要 -----------------------------------------------------------------
step "⑤ 更新摘要"
echo "commit：$BEFORE_COMMIT → $AFTER_COMMIT"
echo "耗时：$(fmt_secs $((SECONDS - START)))"
if [ "$DRY_RUN" -eq 1 ]; then
  printf '[dry-run] 将执行：%s ps\n' "$COMPOSE_CMD"
  echo "[dry-run] 演练结束：未做任何变更。"
else
  echo "容器状态："
  "${COMPOSE[@]}" ps
  echo
  echo "更新完成。如需下线整栈（保留卷）：$COMPOSE_CMD down"
fi
