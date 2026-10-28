# 服务层镜像：Python 3.13（task-002 定版）——镜像官方源（开源批 B2）
FROM library/python:3.13-slim

WORKDIR /app

# A窗 2026-10-19（压测页容器内触发使能）：run_ladder.sh 探针/预检依赖 curl。
# 仅本机/演示栈形态；生产镜像如需瘦身可移除（压测回宿主 CLI 触发口径）。
# 2026-10-21 前置卡（对抗复核 P0-1 根治）：postgresql-client＝meta_writer db 模式
# 容器内直连通道（META_DB_HOST 五键注入即 psql -h 直连 meta 库；基镜像 trixie
# 默认装 PG17 客户端，与 metadata-db postgres:17 同大版本）。
# apt/pip 均官方默认源（开源批 B2；国内网络受限时可在构建期自行配置镜像加速）
RUN apt-get update && apt-get install -y --no-install-recommends curl postgresql-client \
    && rm -rf /var/lib/apt/lists/*

# pip 官方默认源
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY services.yaml upstreams.yaml auth_matrix.yaml ./
COPY registry/ ./registry/
COPY static/ ./static/

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
