#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""授权登记配置面 CLI（task-008 · SPEC §5② · ADR-0003；1b-8 起薄壳同调统一写服务）。

唯一权威矩阵：service/auth_matrix.yaml（task-003 建 M0 静态版，task-008
归一为本管理面的读写对象；config/ 下的临时副本已删除，双矩阵不再并存）。

【1b-8 写径抽层（设计稿 v1 §5-1 无悔步）】矩阵编解码、结构校验（validate_matrix）、
凭证生成（appkey/secret/摘要）全量下沉 service/app/grants_admin.py——授权管理页
端点与本 CLI 同调同一服务层、同一校验函数（三入口同门禁的第一步）。本文件只留
命令行编排（参数解析/输出文案/consumer 片段）；1b-7 meta_writer 合入时持久层
切换发生在 grants_admin.save_matrix，本 CLI 零改动。

格式＝003 基底（顶层 matrix[]：tenant/appkey/oa_ticket/grants[]→
service/fields——service/app/kv_executor.py 直接消费）＋管理面可选扩展
字段（租户级 display_name/secret_hash/status/tenant_type；grant 级
endpoint/oa_ticket/kind/status/effective_from/effective_to）。
扩展字段对服务读取方透明（tenant_grant 只认基底字段）。

职责边界：审批在工单系统完成，平台只把工单批的结果「登记」进矩阵并
发放调用凭证；缺 OA 单号一律拒绝登记（ADR-0003 的同步风险对策）。
沙箱租户（tenant_type=sandbox）豁免：平台演示模板，单号留空（1b-8 起）。

子命令：
  register  登记一条授权（租户×服务×字段×OA 单号）。租户首次登记时
            发放 appkey/secret：secret 仅在 stdout 显示一次，矩阵文件
            只存 sha256 摘要；同时输出 APISIX consumer 初始化命令片段。
            重复登记（同租户×同服务）拒绝；已有租户复用其 appkey。
  validate  校验矩阵文件是否符合规范（结构/必填/格式/唯一性）。

编解码：pyyaml（003 已引入；行内注释 safe_load 可解析），写回用
safe_dump ＋固定头注释模板——行内注释不保留，规则说明统一收进头注释。

安全纪律（AGENTS.md 安全红线）：secret 明文不进代码，防注入；secret 明文
不落盘、不落日志，仅在 register 的 stdout 显示一次；写盘前断言明文不出现在
序列化结果中（断言在 grants_admin.save_matrix）。

用法示例：
  python3 scripts/auth_admin.py register \
      --tenant tenant-foo --display-name "甲合作方" \
      --service user_tag_query --endpoint /v1/tags/query \
      --fields phone_sm3,age_band --oa-ticket OA-2026-000123
  python3 scripts/auth_admin.py validate
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MATRIX = REPO_ROOT / "service" / "auth_matrix.yaml"

# 统一写服务（授权管理页端点同调此层；宿主机 CLI 须把 service/ 加入 import 路径）
sys.path.insert(0, str(REPO_ROOT / "service"))
from app.grants_admin import (   # noqa: E402
    APPKEY_RE, ENDPOINT_RE, MatrixFormatError, OA_TICKET_RE, SECRET_HASH_RE,
    SECRET_RE, SERVICE_ID_RE, TENANT_ID_RE, _new_matrix_doc, _utc_now_iso,
    dump_matrix, gen_appkey, gen_secret, load_matrix_file, save_matrix,
    secret_digest, validate_matrix,
)


# ----------------------------------------------------------------------
# 子命令：register / validate（命令行编排；校验与写盘全在统一写服务）
# ----------------------------------------------------------------------

def _consumer_snippet(tenant_id: str, appkey: str) -> str:
    """APISIX consumer 初始化命令片段（key-auth；与 configs/apisix/init_routes.py
    同风格：同 Admin 地址、同 X-API-KEY、同 consumer 结构的幂等 PUT）。
    Admin API 9180 仅 compose 内网可达，故片段借 service 容器执行，本脚本不直连。"""
    return (
        "---- APISIX consumer 初始化片段（复制到仓库根执行；幂等可重复）----\n"
        "docker compose exec -T service python - <<'PYEOF'\n"
        "import json, os, urllib.request\n"
        "admin_key = os.environ.get('APISIX_ADMIN_KEY', '***REMOVED***')\n"
        f"body = json.dumps({{'username': '{tenant_id}', "
        f"'plugins': {{'key-auth': {{'key': '{appkey}'}}}}}}).encode()\n"
        f"req = urllib.request.Request('http://oneserve-apisix:9180/apisix/admin/consumers/{tenant_id}', "
        "data=body, method='PUT',\n"
        "                     headers={'X-API-KEY': admin_key, 'Content-Type': 'application/json'})\n"
        "print('PUT consumer ->', urllib.request.urlopen(req).status)\n"
        "PYEOF\n"
        "（执行后即时生效；APISIX_ADMIN_KEY 以实际环境为准）"
    )


def cmd_register(args) -> int:
    # —— ADR-0003 对策：缺 OA 单号一律拒绝，矩阵文件保持不动 ——
    if not args.oa_ticket or not args.oa_ticket.strip():
        print("错误：--oa-ticket 必填。审批在工单系统完成，平台只做授权登记；"
              "缺 OA 单号拒绝登记（ADR-0003）。", file=sys.stderr)
        return 2
    oa_ticket = args.oa_ticket.strip()
    if not OA_TICKET_RE.match(oa_ticket):
        print(f"错误：--oa-ticket 格式非法：{oa_ticket!r}（4~64 位字母数字-_）", file=sys.stderr)
        return 2
    if not TENANT_ID_RE.match(args.tenant):
        print(f"错误：--tenant 格式非法：{args.tenant!r}（小写字母/数字/_-，2~64 位）", file=sys.stderr)
        return 2
    if not SERVICE_ID_RE.match(args.service):
        print(f"错误：--service 格式非法：{args.service!r}", file=sys.stderr)
        return 2
    if args.endpoint is not None and not ENDPOINT_RE.match(args.endpoint):
        print(f"错误：--endpoint 格式非法：{args.endpoint!r}（须以 / 开头）", file=sys.stderr)
        return 2
    fields = [f.strip() for f in args.fields.split(",") if f.strip()]
    if not fields:
        print("错误：--fields 必须给出至少一个字段白名单项（逗号分隔）", file=sys.stderr)
        return 2
    if len(fields) != len(set(fields)):
        print("错误：--fields 含重复字段", file=sys.stderr)
        return 2

    operator = args.operator or os.environ.get("USER", "auth_admin")

    if args.matrix.is_file():
        try:
            data = load_matrix_file(args.matrix)
        except MatrixFormatError as exc:
            print(f"错误：矩阵文件无法解析（{args.matrix}）：{exc}", file=sys.stderr)
            return 4
    else:
        data = _new_matrix_doc(operator)
    errors, warnings = validate_matrix(data)
    if errors:
        print(f"错误：矩阵底稿不合法（{args.matrix}），拒绝改写：", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 4

    matrix = data.setdefault("matrix", [])
    row = next((r for r in matrix if r.get("tenant") == args.tenant), None)
    if row is None:
        row: dict = {"tenant": args.tenant}
        if args.display_name:
            row["display_name"] = args.display_name
        row["appkey"] = None        # 占位，发凭证后回填（保持键序可读）
        row["oa_ticket"] = oa_ticket
        row["grants"] = []
        matrix.append(row)
        is_new_tenant = True
    else:
        is_new_tenant = False
        if args.display_name and row.get("display_name") is not None \
                and args.display_name != row.get("display_name"):
            print(f"提示：display_name 保持既有值 {row.get('display_name')!r}（登记不变更租户名）",
                  file=sys.stderr)
        dup = next((g for g in row.get("grants", []) if g.get("service") == args.service), None)
        if dup is not None:
            print(f"错误：{args.tenant} × {args.service} 已登记（OA 单号 "
                  f"{dup.get('oa_ticket') or row.get('oa_ticket')}），重复登记拒绝；"
                  "如需变更请走吊销/重登流程。", file=sys.stderr)
            return 3

    # 凭证：租户已有 appkey 则复用；否则新发（secret 仅显示一次）
    secret_plain = None
    if row.get("appkey"):
        appkey = str(row["appkey"])
        print("凭证：复用租户现有 appkey（不重复发放）")
    else:
        all_keys = {str(r.get("appkey")) for r in matrix if r.get("appkey")}
        appkey = gen_appkey(all_keys)
        secret_plain = gen_secret()
        row["appkey"] = appkey
        row["secret_hash"] = secret_digest(secret_plain)
        row.setdefault("status", "active")

    grant: dict = {"service": args.service}
    if args.endpoint:
        grant["endpoint"] = args.endpoint
    grant["oa_ticket"] = oa_ticket          # 单次授权溯源（租户级为首次发证/授权单号）
    grant["fields"] = fields
    row.setdefault("grants", []).append(grant)
    data["meta"] = {"updated": _utc_now_iso(), "updated_by": operator}

    save_matrix(args.matrix, data, secret_plain=secret_plain)

    # —— 输出：secret 明文仅出现在这里一次 ——
    print(f"登记成功：租户={args.tenant} 服务={args.service} OA单号={oa_ticket}"
          + ("（新租户）" if is_new_tenant else ""))
    print(f"字段白名单：{', '.join(fields)}")
    print(f"矩阵已更新：{args.matrix}")
    print(f"APPKEY={appkey}")
    if secret_plain is not None:
        print(f"SECRET={secret_plain}")
        print("（SECRET 仅此一次显示，请立即交付调用方并妥善保管；矩阵中只存 sha256 摘要）")
    print(_consumer_snippet(args.tenant, appkey))
    return 0


def cmd_validate(args) -> int:
    try:
        data = load_matrix_file(args.matrix)
    except MatrixFormatError as exc:
        print(f"校验失败：{exc}", file=sys.stderr)
        return 1
    errors, warnings = validate_matrix(data)
    matrix = data.get("matrix", []) or []
    n_tenants = len(matrix)
    n_grants = sum(len(r.get("grants", [])) for r in matrix if isinstance(r, dict))
    print(f"矩阵：{args.matrix}（租户 {n_tenants} 个 / 授权 {n_grants} 条）")
    for w in warnings:
        print(f"  ⚠ {w}")
    if errors:
        print(f"校验未过（{len(errors)} 项）：")
        for e in errors:
            print(f"  - {e}")
        return 1
    print("校验通过：符合授权矩阵规范（SPEC §5② · ADR-0003；基底兼容 kv_executor）")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="OneServe 授权登记配置面（OA 单号必填，ADR-0003）")
    sub = parser.add_subparsers(dest="command", required=True)

    p_reg = sub.add_parser("register", help="登记一条授权（租户×服务×字段×OA 单号）")
    p_reg.add_argument("--tenant", required=True, help="租户 ID（小写字母/数字/短横线）")
    p_reg.add_argument("--display-name", default=None, help="租户显示名（新租户建议提供）")
    p_reg.add_argument("--service", required=True, help="服务 ID（如 user_tag_query）")
    p_reg.add_argument("--endpoint", default=None, help="服务端点（可选，如 /v1/tags/query，供计量对账映射）")
    p_reg.add_argument("--fields", required=True, help="字段白名单，逗号分隔")
    p_reg.add_argument("--oa-ticket", default=None, help="OA 审批单号（必填，缺省拒绝——ADR-0003）")
    p_reg.add_argument("--matrix", type=Path, default=DEFAULT_MATRIX, help=f"矩阵文件路径（默认 {DEFAULT_MATRIX}）")
    p_reg.add_argument("--operator", default=None, help="登记操作人（默认取 USER 环境变量）")
    p_reg.set_defaults(func=cmd_register)

    p_val = sub.add_parser("validate", help="校验矩阵文件规范")
    p_val.add_argument("--matrix", type=Path, default=DEFAULT_MATRIX, help=f"矩阵文件路径（默认 {DEFAULT_MATRIX}）")
    p_val.set_defaults(func=cmd_validate)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
