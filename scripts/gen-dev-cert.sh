#!/usr/bin/env bash
# 自签开发证书生成（蓝图 1a 批 HTTPS 预留 · 2026-10-20）。
# 输出 certs/dev/{fullchain,privkey}.pem（certs/ 已在 .gitignore，私钥永不入仓）。
# 用途：portal 容器 443 预留块（configs/nginx/portal-nginx.conf）与本地 prod 形态
# （compose.prod.yml 20443 回环口）验证 TLS 握手；真域名部署换 CA 签发证书（1b 批）。
# 幂等：已存在且未过期则跳过；FORCE=1 强制重签。
set -euo pipefail
cd "$(dirname "$0")/.."
OUT=certs/dev
mkdir -p "$OUT"
if [ "${FORCE:-0}" != "1" ] && [ -s "$OUT/fullchain.pem" ] && [ -s "$OUT/privkey.pem" ]; then
  end=$(openssl x509 -in "$OUT/fullchain.pem" -noout -enddate 2>/dev/null | cut -d= -f2- || true)
  if [ -n "$end" ] && openssl x509 -checkend 86400 -noout -in "$OUT/fullchain.pem" >/dev/null 2>&1; then
    echo "[gen-dev-cert] 证书已存在且 24h 内不过期（end=${end}），跳过（FORCE=1 重签）"
    exit 0
  fi
fi
openssl req -x509 -newkey rsa:2048 -sha256 -days 825 -nodes \
  -keyout "$OUT/privkey.pem" -out "$OUT/fullchain.pem" \
  -subj "/CN=localhost" \
  -addext "subjectAltName=DNS:localhost,IP:127.0.0.1"
chmod 600 "$OUT/privkey.pem"
echo "[gen-dev-cert] 已生成 $OUT/fullchain.pem + $OUT/privkey.pem（自签，仅本机 dev/验证用）"
