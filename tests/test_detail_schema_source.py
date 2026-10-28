#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""N2 降级小活：详情页字段说明 ⇆ 装载产物 同源断言（一张测试卡）。

背景（字段释义收权批之后）：
  - 字段释义真源＝sync:schema 装载产物（GET /v1/admin/load/schema）；
  - 服务详情页（portal/src/detail.html＋assets/detail.js＋构建产物
    portal/catalog/v1-svc-*.html）字段表数据源＝构建期 gen_openapi 生成的
    portal/openapi/details/<sid>.json。
本卡把「详情页字段说明与装载产物同源」固化成机检，防两方将来各自换源漂移：

  静态断言（离线，必跑）
    1. detail.js 取数字段的数据源＝../openapi/details/<sid>.json（且是唯一
       fetch 源）；构建产物 catalog/<sid>.html 壳引用同一 detail.js；
    2. gen_openapi.py 生成 details 时字段中文说明的来源链＝表级字典
       sync/schema_*.csv 经 sync/schema_meta.dict_rows 唯一实现——与装载产物
       sync:schema 的 cnName 富化（schema_meta.build_fields）同源，不另造字典；
       并离线实证 load_cn_desc ≡ dict_rows('usr_asset_tag') 的 cnName（全字段）。

  一致性活断言（skip 保护）
    3. 现役 usr_asset_tag（tags:usr_asset_tag:latest 指针，经
       GET /v1/admin/load/assets 读 live 标）版本下，
       GET /v1/admin/load/schema?table=usr_asset_tag&version=<现役> 的同名字段
       cnName ≡ details JSON 字段表（response.auth_fields——详情页出参字段表的
       表字段行）中文名。服务不可达/无现役版 → pytest.skip（不假绿）；
       两源真不一致 → fail 并如实列差异（是否改数据归新卡裁决，本卡不动数据）。

  口径说明：details 的 params/found_shape 等平台信封行的 cn 是平台常量
  （服务标识/查询键…），不属表字段域；同源断言锚 response.auth_fields
  （其 cn 才走 cn_desc→dict_rows 链）。装载产物多于授权白名单的字段
  （投影白名单 ⊆ 表 schema）是预期形态，不判差异、只在差异报告里列明；
  两端「待补」占位文案不同（gen 侧「—（中文名待补…）」vs 装载侧「待补」）
  按如实差异上报，不私设等价豁免。

运行：python3.13 -m pytest tests/test_detail_schema_source.py -q
（静态用例离线可跑；活断言在服务未运行时自动 skip。）
"""
from __future__ import annotations

import inspect
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
for _p in (str(REPO / "scripts"), str(REPO / "sync")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import gen_openapi as gen   # noqa: E402  （gen_openapi 自带把 sync/ 插入 sys.path）
import schema_meta as sm    # noqa: E402

SID = "v1-svc-usr-asset-tag"          # 现役 usr_asset_tag 正式通道服务（services.yaml 登记）
TABLE = "usr_asset_tag"
BASE_URL = "http://localhost:8000"
DETAILS_JSON = REPO / "portal" / "openapi" / "details" / (SID + ".json")
CATALOG_HTML = REPO / "portal" / "catalog" / (SID + ".html")


def _detail_js() -> Path:
    """detail.js 定位：源码区（portal/src）优先，回落构建分发区（portal/assets）。"""
    for cand in (REPO / "portal" / "src" / "detail.js",
                 REPO / "portal" / "assets" / "detail.js"):
        if cand.is_file():
            return cand
    pytest.fail("detail.js 不存在（portal/src 与 portal/assets 均无）——详情页脚本被移动？")


# ── 断言 1（静态）：detail.js 取数路径＝构建期 details sidecar ──────────────

def test_detail_js_sole_data_source_is_details_sidecar():
    """详情页取数字段的数据源必须是 ../openapi/details/<sid>.json，且别无分源。"""
    src = _detail_js().read_text(encoding="utf-8")
    hits = [i for i in range(len(src)) if src.startswith("fetch(", i)]
    assert hits, "detail.js 无任何 fetch——详情页数据源被移走？"
    for i in hits:
        args = src[i:i + 120].split(")")[0]
        assert "../openapi/details/" in args, (
            "detail.js 出现 details sidecar 之外的取数源：fetch(" + args + ")")
    needle = "fetch('../openapi/details/' + sid + '.json'"
    assert needle in src, (
        "取数路径必须是 ../openapi/details/<sid>.json（sid＝URL 解析出的 service_id）")


def test_built_catalog_shell_binds_same_detail_js():
    """构建产物详情壳引用同一 detail.js——壳与取数逻辑不脱钩。"""
    assert CATALOG_HTML.is_file(), (
        "构建产物 " + CATALOG_HTML.name + " 缺失（build_portal 未跑或产物被清）")
    html = CATALOG_HTML.read_text(encoding="utf-8")
    assert "assets/detail.js" in html, "构建产物详情壳未引用 assets/detail.js"


# ── 断言 2（静态）：gen_openapi 字段中文说明来源链＝表级字典唯一实现 ────────

def test_gen_openapi_cn_chain_reads_schema_meta_dict_rows():
    """details 字段中文名来源链＝schema_meta.dict_rows（与装载产物 cnName 同源），不另造字典。"""
    src = inspect.getsource(gen.load_cn_desc)
    assert "schema_meta" in src, "load_cn_desc 未 import schema_meta——中文名读取绕开唯一实现"
    assert "dict_rows(" in src, "load_cn_desc 未调 schema_meta.dict_rows"
    assert 'glob("schema_*.csv")' in src, "中文名来源不再是表级字典 sync/schema_*.csv 族"
    assert "usr_label_wide_297" in src, "297 宽表缓删 CSV 未显式排除（卡2 换源口径回潮）"
    rows = inspect.getsource(gen._auth_fields_rows)
    assert "cn_desc.get(name)" in rows, "auth_fields 中文名不再取自 cn_desc（另造来源）"
    spec_src = inspect.getsource(gen.build_spec)
    assert '"auth_fields": rows' in spec_src, "详情出参表未原样落 auth_fields 行（cn 链断裂）"
    side = inspect.getsource(gen.write_sidecars)
    assert '"details"' in side and '"{sid}.json"' in side, (
        "details sidecar 落盘形态变更（details/<sid>.json）")
    # 同源的另一端：装载产物 sync:schema 的 cnName 富化同经 dict_rows（唯一字典读取实现）
    bf = inspect.getsource(sm.build_fields)
    assert "dict_rows(" in bf and "cnName" in bf, (
        "装载侧 cnName 富化不再走 schema_meta.dict_rows——两端同源被破坏")


def test_cn_desc_equals_table_dict_for_usr_asset_tag():
    """行为级补证：load_cn_desc 非另造字典——usr_asset_tag 全字段与表级 CSV cnName 逐字相等。"""
    d = sm.dict_rows(TABLE)
    assert d, ("sync/schema_" + TABLE + ".csv 缺失或为空——表级字典真源被移除（本卡前提失效，须重审）")
    named = {n: r["cnName"] for n, r in d.items() if r.get("cnName")}
    assert named, "表级字典有行但 cnName 全空"
    cn = gen.load_cn_desc()
    drift = {n: {"gen_cn_desc": cn.get(n), "dict_csv": c}
             for n, c in named.items() if cn.get(n) != c}
    assert not drift, (
        "gen_openapi 中文名与表级字典不一致（另造/错位实证）："
        + json.dumps(drift, ensure_ascii=False))


# ── 断言 3（活断言）：现役装载产物 ⇆ details 同名字段中文名一致（skip 保护） ──

def _get_json(url: str, timeout: float = 3.0):
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _live_version_of(table: str) -> str:
    """经 /v1/admin/load/assets 读 live 指针标的现役版本（GET 读端点，读路径直通）。"""
    try:
        assets = _get_json(BASE_URL + "/v1/admin/load/assets")
    except urllib.error.HTTPError as exc:
        pytest.skip("装载资产读端点被闸/不可用（HTTP " + str(exc.code) + "）——环境口径，如实跳过")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        pytest.skip("OneServe 服务不可达（" + BASE_URL + "）：" + str(exc) + " —— 活断言如实跳过，不假绿")
    rows = {t.get("table"): t for t in assets.get("tables", [])}
    if table not in rows:
        pytest.skip("服务在线但无 " + table + " 装载记录（未装载或已清空间）——无从对现役版断言")
    live = [v["version"] for v in rows[table].get("versions", []) if v.get("live")]
    if not live:
        pytest.skip(table + " 无现役版（live 指针缺失/悬空）——无从断言")
    return live[0]


def test_live_load_schema_cnnames_match_details_artifact():
    """现役 usr_asset_tag：GET /v1/admin/load/schema 同名字段 cnName ≡ details 字段中文名。"""
    assert DETAILS_JSON.is_file(), (
        "details sidecar 缺失：" + str(DETAILS_JSON) + "（gen_openapi 不再产出该服务？）")
    art = json.loads(DETAILS_JSON.read_text(encoding="utf-8"))
    det_cn = {r["name"]: r["cn"] for r in art["response"]["auth_fields"]}
    assert det_cn, "details auth_fields 为空——服务无授权白名单字段？"

    version = _live_version_of(TABLE)
    try:
        sch = _get_json(BASE_URL + "/v1/admin/load/schema?table=" + TABLE + "&version=" + version)
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403, 503):
            pytest.skip("管理读端点被闸/暂不可用（HTTP " + str(exc.code) + "）——环境口径，如实跳过")
        pytest.fail(
            "现役 " + TABLE + "@" + version + " 装载产物 schema 读取失败（HTTP "
            + str(exc.code) + "）——现役版无 schema 副产品＝两源断链，如实报（处置归新卡）")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        pytest.skip("OneServe 服务不可达（" + BASE_URL + "）：" + str(exc) + " —— 活断言如实跳过，不假绿")
    assert sch.get("ok") is True, "load/schema 应答异常：" + json.dumps(sch, ensure_ascii=False)

    live_cn = {f["name"]: (f.get("cnName") or "") for f in sch.get("fields", [])}
    assert live_cn, "现役 " + TABLE + "@" + version + " schema 字段为空"
    common = sorted(set(det_cn) & set(live_cn))
    assert common, (
        "两源字段名零交集：details=" + json.dumps(sorted(det_cn), ensure_ascii=False)
        + " vs 装载产物=" + json.dumps(sorted(live_cn), ensure_ascii=False)
        + "——投影白名单与表 schema 完全错位")

    diff = {n: {"details": det_cn[n], "load_schema": live_cn[n]}
            for n in common if det_cn[n] != live_cn[n]}
    assert not diff, (
        "详情页与装载产物同名字段中文名不一致（现役 " + TABLE + "@" + version + "）——"
        "如实差异，改哪端归新卡裁决：" + json.dumps(diff, ensure_ascii=False)
        + "；仅装载产物有（白名单外，预期形态不判差异）："
        + json.dumps(sorted(set(live_cn) - set(det_cn)), ensure_ascii=False))
    # 卡面口径的字面等式：同名字段中文名集合相等
    assert {det_cn[n] for n in common} == {live_cn[n] for n in common}
