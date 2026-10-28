#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""批量查询链路防回潮守卫（P2b 实战首跑 2026-10-15；配套证据
tests/evidence/batch-first-run-20261015.txt）。

背景：batch_worker 599 行真实现但队列零任务从未真跑——根因是 batch.BATCH_SOURCE_SERVICE
写死已删除的 user_tag_query（09-29 全服务下线漏更名），任何提交都 403、队列永空。
单元测试当时全绿＝fixture 自洽掩盖了与真实 services.yaml/auth_matrix.yaml 的脱节。
本守卫三道闸（轻量直测，不跑真实任务——首跑证据在 evidence）：
  ① 端点存在性：batch_api 路由五条齐全（提交/状态/结果/取消/重试）；
  ② 提交体校验逻辑直测：形状/名单/字段三道校验的机读拒绝口径（batch.py 纯逻辑）；
  ③ 载体服务名一致性（本卡核心防回潮）：BATCH_SOURCE_SERVICE 必须真实存在于
     service/services.yaml 注册表、且在 service/auth_matrix.yaml 有租户授权——
     服务再下线/更名而批量常量不跟随时，此处即刻红（语义取舍：批量载体服务
     必须在线；注册表全下线空表合法态下批量链路本就不可用，守卫如实红）。

纯 python 直跑或 pytest 收集均可（同 tests/test_batch_api.py 纪律）。
"""

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "service"))

from app import batch  # noqa: E402
from app import errors  # noqa: E402
from app import kv_executor as kvx  # noqa: E402


# ---------- ① 端点存在性 ----------

def test_batch_endpoints_registered():
    """批量五端点路由齐全：提交/状态/结果交付/取消/重试（P2b 验收口径）。

    宿主机无 fastapi（项目纪律：batch_api 薄胶水另行语法机检，同 test_batch_api）——
    路由断言＝py_compile 可编译＋源码五条路由装饰器文本在场；端点级行为已在
    首跑实战验证（evidence §3/§5：提交 202/状态/下载/分页/取消语义旁证全通）。
    """
    import py_compile
    api = REPO / "service" / "app" / "batch_api.py"
    py_compile.compile(str(api), doraise=True)        # 薄胶水可编译（语法机检）
    src = api.read_text(encoding="utf-8")
    for deco in ('@router.post("/v1/batch/jobs")',
                 '@router.get("/v1/batch/jobs/{job_id}")',
                 '@router.get("/v1/batch/jobs/{job_id}/results")',
                 '@router.delete("/v1/batch/jobs/{job_id}")',
                 '@router.post("/v1/batch/jobs/{job_id}/retry")'):
        assert deco in src, f"批量路由缺失：{deco}"


# ---------- ② 提交体校验逻辑直测（不依赖 FastAPI/redis） ----------

def _payload_err(payload, quotas=None):
    """直跑提交体三道校验（形状→名单→字段），返回 BatchError 或 None（全过）。"""
    try:
        scope, keys, fields = batch._validate_payload(payload)
        if scope == batch.SCOPE_SNAPSHOT:
            keys = None
        else:
            keys = batch._validate_keys(keys, r"^[a-z0-9]{1,128}$",
                                        (quotas or batch.quota_limits())["max_keys_per_job"])
        if fields is not None:
            batch._validate_fields(fields, {"aum_band", "churn_risk_band"})
        else:
            batch._validate_fields(fields, {"aum_band"})   # None→返回全白名单，不抛
        return None
    except batch.BatchError as exc:
        return exc


def test_submit_payload_shape_rejections():
    """形状闸：非对象体/非法 scope/snapshot 携 keys/keys 非字符串数组→机读码。"""
    assert _payload_err("not-a-dict").code == errors.INVALID_JSON
    assert _payload_err({"scope": "diff", "keys": []}).code == errors.INVALID_SCOPE
    assert _payload_err({"scope": "snapshot", "keys": ["ab" * 32]}).code == errors.INVALID_REQUEST
    assert _payload_err({"keys": "ab" * 32}).code == errors.INVALID_REQUEST
    assert _payload_err({"keys": [123]}).code == errors.INVALID_REQUEST


def test_submit_keys_and_fields_rejections():
    """名单闸＋字段闸：空/超上限/非法键/字段越权→机读码；合法直通不产生异常。"""
    quotas = {"max_keys_per_job": 10}
    assert _payload_err({"keys": []}, quotas).code == errors.EMPTY_KEYS
    assert _payload_err({"keys": ["ab" * 32] * 11}, quotas).code == errors.TOO_MANY_KEYS
    assert _payload_err({"keys": ["has space"]}).code == errors.INVALID_KEY
    assert _payload_err({"keys": ["ab" * 32], "fields": ["usr_id"]}).code \
        == errors.FIELDS_NOT_GRANTED
    assert _payload_err({"keys": ["ab" * 32], "fields": ["aum_band"]}) is None
    assert _payload_err({"keys": ["AB" * 32]}) is None   # 归一化小写后合法


# ---------- ③ 载体服务名一致性（本卡核心防回潮） ----------

def test_batch_source_service_exists_in_real_registry_and_matrix():
    """BATCH_SOURCE_SERVICE 必须在真实 services.yaml 注册、真实 auth_matrix.yaml 有授权。

    2026-10-15 首跑断链根因守卫：批量常量指向已删除/未授权服务时，单测 fixture
    自洽照样全绿而实战永 403——本测试直接读生产真源（无 redis 依赖），常量脱节
    即刻红，杜绝「纸面实现、队列永空」回潮。
    """
    services = kvx.load_services()                    # 真源注册表（空表 fail-loud）
    matrix = kvx.load_auth_matrix()                   # 真源授权矩阵
    sid = batch.BATCH_SOURCE_SERVICE
    assert sid in services, \
        f"批量载体服务 {sid!r} 不在真实注册表（09-29 式下线后漏更名→队列永空回潮）"
    svc = services[sid]
    assert svc.get("archetype") == "kv_lookup", svc.get("archetype")
    kv = svc["backend"]["kv"]
    # worker 依赖的三件 kv 模板齐全（alias 指针/版本元信息/数据键）
    for field in ("alias_key", "version_meta_key", "data_key"):
        assert kv.get(field), f"{sid} 缺 backend.kv.{field}"
    assert svc["params"][0].get("pattern"), f"{sid} 缺键 pattern（提交端名单校验依赖）"
    granted = {g["service"] for row in matrix for g in row.get("grants", [])}
    assert sid in granted, \
        f"批量载体服务 {sid!r} 无任何租户授权（auth_matrix.yaml）→ 实战提交必 403 回潮"


def test_batch_source_service_matches_point_query_channel():
    """批量载体与正式点查通道同源（main.py /v1/tags/query 指向同一服务）。

    口径一致性守卫：批量投影白名单/键空间与点查同源是 AC-9 的前提；两通道
    指向不同服务＝口径漂移（本卡修复前 user_tag_query/v1-svc-* 分裂即实例）。
    """
    main_py = (REPO / "service" / "app" / "main.py").read_text(encoding="utf-8")
    sid = batch.BATCH_SOURCE_SERVICE
    assert sid in main_py, \
        f"正式点查通道未指向批量载体服务 {sid!r}（/v1/tags/query 口径漂移）"


# ---------- ④ 首跑证据在场 ----------

