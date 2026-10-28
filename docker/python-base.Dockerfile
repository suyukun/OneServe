# OneServe 预装基础镜像（2026-10-22）：python 官方 slim＋curl＋postgresql-client
# 背景：CI 构建容器内存配额过小，apt 解压 alloc 失败（lzma Cannot allocate memory）
# ——故 apt 步骤移至本地构建此基础镜像，云端 Dockerfile 只做轻量步骤。
# 开源批 B2：底座改官方源（原私仓前缀退场）；产物供根 Dockerfile.prod 引用。
FROM python:3.13-slim

RUN apt-get update && apt-get install -y --no-install-recommends curl postgresql-client \
    && apt-get upgrade -y \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir --progress-bar off --upgrade pip setuptools
