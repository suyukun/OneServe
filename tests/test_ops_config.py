#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""运营写操作接真套件（P1-10/11 · 前台重造 SPEC §5 P1-10/11 行，D2 写侧）。

覆盖（机器化部分；compose 端到端 curl 证据归验证脚本/回执）：
  真源目录树：GET 视图由字段字典 CSV×services.yaml 生成（六域真实列数、
    297 蓝本口径、示例键不预造＝留空＋可粘贴口径（P0-1：假用户演示键已下线）），
    mock schema-domains 不再参与；
  services.yaml 写区隔离：发布/下线只动文件尾 console_services 写区——运行时
    注册区（含全部注释）逐字节保留，load_services 真加载器零漂移才落盘；
  版本语义：同 route＝新版本条目（V1→V2…，±N 摘要），下线追加版本栏记录；
  校验拒绝：坏路由/空字段/发布态缺示例值/未知状态等，文件零变更；
  upstreams.yaml：缺文件＝空表、草稿/发布/下线状态机、同 route 更新、校验拒绝；
  原子写：tmp+rename，不留 *.tmp 残件；
  HTTP 编排：admin_api 六端点在位且全部过管理令牌闸（语法机检，宿主无 fastapi）；
  前端接线：两页 live 分支（apiFetch+X-Admin-Token+真源 trace 占位文案），
    demo 降级路径保持本卡之前行为；portal/admin 构建产物同步（无陈旧拷贝）。

被测对象 service/app/ops_config.py 不依赖 FastAPI/redis（分层纪律）。
"""
import py_compile
import re
import shutil
import subprocess
import pytest
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "service"))

from app import ops_config as oc            # noqa: E402
from app.kv_executor import load_services   # noqa: E402
from synth_services import real_registry_state  # noqa: E402  现实态助手（tests/ 由 pytest 注入 sys.path）

NOW = datetime(2026, 9, 23, 8, 0, tzinfo=timezone.utc)
FIELDS_1 = [{"name": "pf_chnl_sts_41", "sample": "APP", "desc": "主触达渠道"},
            {"name": "if_acct_sts_01", "sample": "正常", "desc": "账户状态"}]


def _tmp_services() -> Path:
    """发布管线基板：真 services.yaml 头注释＋一个最小运行时服务条目。

    全下线终态下真文件是 services: []（真加载器 fail-loud），零漂移断言需要
    非空运行时注册表——用合成条目充当基板（与线上状态解耦，形状＝真实条目）。
    """
    d = Path(tempfile.mkdtemp())
    p = d / "services.yaml"
    real = (REPO / "service" / "services.yaml").read_text(encoding="utf-8")
    head = real.split("services:")[0] + "services:" + chr(10)
    seed = """  - service_id: seed_kv_query            # 测试基板合成条目（非线上服务）
    archetype: kv_lookup
    backend:
      kv:
        namespace: tags:seed
        alias_key: tags:seed:latest
        data_key: 'tags:seed:{version}:{sm3}'
        version_meta_key: 'sync:ver:seed:{version}'
        storage_key: as_received
        entity: seed
    params:
      - name: key
        type: string(1..128)
        key_type: seed_key
        pattern: '^[A-Za-z0-9_.-]{1,128}$'
    response:
      projection: auth_matrix
      meta: [data_as_of, live_version]
    guard:
      timeout_ms: 3000
      max_items_per_call: 1
    audit:
      enabled: true
      log_params: hash_only
      log_response_digest: sha256_of_desensitized_body
"""
    p.write_text(head + seed, encoding="utf-8")
    return p


def _svc_fields(names, sample="x"):
    return [{"name": n, "sample": sample, "desc": ""} for n in names]


# ---------- 真源目录树 ----------

def test_tree_generated_from_real_sources():
    """目录树＝字段字典 CSV 真源分组（六族真实列数，合计 296 出参＋键列＝297）。"""
    view = oc.build_console_view()
    counts = {d["id"]: len(d["fields"]) for d in view["domains"]}
    assert counts == {"base": 34, "behavior": 242, "marketing": 10,
                      "channel": 6, "risk": 2, "misc": 2}
    assert view["datasource"]["columnCount"] == 297
    _reg, _reg_err = real_registry_state()
    assert not _reg_err   # 2026-09-30 现实态口径：fail-loud 加载语义照守
    # 2026-09-30 现实态口径：注册服务数动态对真源注册表（str 键＝去重服务数；空/非空均合法）
    assert view["datasource"]["registeredServices"] == len([k for k in _reg if isinstance(k, str)])
    assert "usr_id" not in [f["name"] for d in view["domains"] for f in d["fields"]]
    some = view["domains"][0]["fields"][0]
    assert some["sample"] == "" and some["type"] and some["desc"]   # 示例值留白＝运营者填（门禁口径）
    # P0-1：示例键不再预造（历史演示键＝假用户 SM3，真库无此行）；真源视图只给空＋可粘贴口径
    assert view["key"]["sampleKey"] == "" and view["key"]["sampleUsrId"] == ""
    assert view["key"]["readonly"] is False
    assert "粘贴" in view["key"]["note"]
    assert "DEMO-USR-0001" not in str(view) and \
        "291eded30fd68c4cf423fe14bb7d53e5c076de2494d7f5fa2a21abf2649de96f" not in str(view)
    # 2026-10-20 C1：发布台服务列表＝console 写区条目＋upstreams.yaml 已发布代理
    # （kind=proxy、fields=[]）。写区条目顺序/内容不变，代理作为功能扩展追加在后；
    # 代理只取 published（草稿/下线不对外，不能进签发页）。
    console_ids = [e["id"] for e in oc.load_console_services()]
    assert [s["id"] for s in view["services"]][:len(console_ids)] == console_ids   # 写区条目如实展示真源（空/非空均合法，不预置演示数）
    proxy_ids = [p["id"] for p in oc.load_upstreams() if p.get("status") == "published"]
    assert [s["id"] for s in view["services"]] == console_ids + proxy_ids
    for s in view["services"]:
        if s["id"] in proxy_ids:
            assert s["kind"] == "proxy" and s["fields"] == []


# ---------- services.yaml 写区隔离＋版本语义 ----------

def test_publish_writezone_isolation_and_zero_drift():
    """发布只写文件尾写区：运行时注册区逐字节保留＋真加载器注册表零漂移。"""
    p = _tmp_services()
    orig = p.read_text(encoding="utf-8")
    reg_before = {k: v for k, v in load_services(p).items()}
    entry, before = oc.publish_service(p, name="渠道偏好画像点查",
                                       route="/v1/svc/channel-preference",
                                       fields=FIELDS_1, operator="admin@oneserve", now=NOW)
    text = p.read_text(encoding="utf-8")
    assert text.split(oc.CONSOLE_MARKER)[0] == orig            # 运行时区（含注释）逐字节不动
    assert oc.CONSOLE_MARKER in text and "console_services:" in text
    assert load_services(p) == reg_before                      # 真加载器：注册表零漂移
    assert entry["id"] == "v1-svc-channel-preference"
    assert entry["status"] == "published" and entry["fields"] == ["pf_chnl_sts_41", "if_acct_sts_01"]
    assert entry["gitShortHash"] is None and before is None    # git 留痕占位：不伪造 hash
    # 卡4：无 version/versions 键；发布史在 trace.history（NC-4b 留痕位）
    assert "version" not in entry and "versions" not in entry
    stored = oc.load_console_services(p)[0]
    assert stored["trace"]["history"][0]["delta"] == "+2"


def test_publish_overwrite_and_delta():
    """卡4 AC-2a：同 route 重复发布＝覆盖更新（仍单条目），trace.history 带 ±N 摘要。"""
    p = _tmp_services()
    oc.publish_service(p, name="渠道偏好", route="/v1/svc/chn", fields=FIELDS_1, now=NOW)
    e2, before = oc.publish_service(
        p, name="渠道偏好", route="/v1/svc/chn",
        fields=_svc_fields(["pf_chnl_sts_41", "pf_chnl_sts_43", "ac_active_sts_01"]), now=NOW)
    assert before is not None and before["fields"] == ["pf_chnl_sts_41", "if_acct_sts_01"]
    assert "version" not in e2 and "versions" not in e2       # 服务修订号已删
    stored = oc.load_console_services(p)[0]
    hist = stored["trace"]["history"]
    assert len(hist) == 2 and hist[-1]["delta"] == "+2 -1"   # 覆盖前旧值留痕（AC-2b）
    entries = oc.load_console_services(p)
    assert len(entries) == 1                                    # 同 id 覆盖，不新增行


def test_publish_preserves_card_meta_fields():
    """重发重建条目保持卡面元数据（2026-10-15，N5「重建丢 desc」边界同款收口）。

    场景＝真库现状：desc/update_frequency/owner 手编登记在写区（发布向导不收集
    这三项）；同 id 重发重建条目后三项须原样继承——console 写区与运行时区两处。
    """
    p = _tmp_services()
    oc.publish_service(p, name="资产标签点查", route="/v1/svc/asset",
                       fields=_svc_fields(["pf_chnl_sts_41"]), now=NOW)
    # 手编登记卡面元数据（N5 desc 手编同场景），走真写器落盘
    entries = oc.load_console_services(p)
    entries[0].update({"desc": "按查询键返回资产标签。", "update_frequency": "每日（T+1）",
                       "owner": "平台运营组"})
    head, _ = oc._split_marker(p.read_text(encoding="utf-8"))
    oc._save_services_yaml(p, head, entries, [])
    # 同 route 同名重发＝同 id 覆盖更新，条目整体重建——三项不得丢
    oc.publish_service(p, name="资产标签点查", route="/v1/svc/asset",
                       fields=_svc_fields(["pf_chnl_sts_41", "if_acct_sts_01"]), now=NOW)
    stored = oc.load_console_services(p)[0]
    assert stored["desc"] == "按查询键返回资产标签。"
    assert stored["update_frequency"] == "每日（T+1）" and stored["owner"] == "平台运营组"

    # hbase 通道（现行发布通道）：写区＋运行时区两处保持——运行时条目＝console 条目
    # 拷贝，继承须发生在拷贝之前（否则运行时区丢三项）
    names = [n for n in oc.load_field_dict(table="usr_basic_info") if n != "usr_id"][:2]
    hb = {"table": "usr_basic_info", "cf": "cf", "key_type": "phone_sm3"}
    f = [{"name": n, "sample": "x", "desc": ""} for n in names]
    oc.publish_service(p, name="基本信息查询", route="/v1/svc/biz-info",
                       fields=f, datasource="hbase", hbase=hb, now=NOW)
    entries = oc.load_console_services(p)
    biz = next(e for e in entries if e["id"] == "v1-svc-biz-info")
    biz.update({"desc": "按查询键返回用户基本信息。", "update_frequency": "每日（T+1）",
                "owner": "平台运营组"})
    head, _ = oc._split_marker(p.read_text(encoding="utf-8"))
    oc._save_services_yaml(p, head, entries, [])
    oc.publish_service(p, name="基本信息查询", route="/v1/svc/biz-info",
                       fields=f, datasource="hbase", hbase=hb, now=NOW)
    biz2 = next(e for e in oc.load_console_services(p) if e["id"] == "v1-svc-biz-info")
    assert biz2["desc"] == "按查询键返回用户基本信息。"
    assert biz2["update_frequency"] == "每日（T+1）" and biz2["owner"] == "平台运营组"
    rt = load_services(p)["v1-svc-biz-info"]                  # 运行时区条目同步继承
    assert rt.get("update_frequency") == "每日（T+1）" and rt.get("owner") == "平台运营组"


def test_carry_card_meta_semantics():
    """_carry_card_meta：继承前条目非空值、strip 归一、新条目自带值优先。"""
    prev = {"desc": " 按查询键返回资产标签。 ", "update_frequency": "每日（T+1）",
            "owner": "平台运营组"}
    entry = {"name": "n"}
    oc._carry_card_meta(entry, prev)
    assert entry["desc"] == "按查询键返回资产标签。"          # 继承＝strip 后非空值
    assert entry["update_frequency"] == "每日（T+1）" and entry["owner"] == "平台运营组"
    own = {"desc": "向导新值", "update_frequency": "每周"}
    oc._carry_card_meta(own, prev)                            # 自带值优先，不被旧值覆盖
    assert own["desc"] == "向导新值" and own["update_frequency"] == "每周"
    assert own["owner"] == "平台运营组"                       # 缺项仍从前条目补
    fresh = {"name": "n2"}
    oc._carry_card_meta(fresh, None)                          # 无前条目＝零继承不报错
    assert "desc" not in fresh


def test_publish_validation_rejects_and_keeps_file():
    """校验拒绝：坏路由/空字段/发布态缺示例值/未知状态——文件零变更。"""
    p = _tmp_services()
    before_text = p.read_text(encoding="utf-8")
    cases = [
        (dict(name="", route="/v1/svc/xxx", fields=_svc_fields(["a_field"])), "invalid_name"),
        (dict(name="n", route="/v1/SVC/X", fields=_svc_fields(["a_field"])), "invalid_route"),
        (dict(name="n", route="/v1/svc/xxx", fields=[]), "empty_fields"),
        (dict(name="n", route="/v1/svc/xxx", fields=[{"name": "pf_chnl_sts_41", "sample": "", "desc": ""}]),
         "sample_required"),
        (dict(name="n", route="/v1/svc/xxx", fields=_svc_fields(["BAD"])), "invalid_field"),
        (dict(name="n", route="/v1/svc/xxx", fields=_svc_fields(["a_field"]), status="offline"),
         "invalid_status"),
    ]
    for kwargs, code in cases:
        try:
            oc.publish_service(p, now=NOW, **kwargs)
            raise AssertionError(f"应拒绝 {kwargs}（期望 {code}）")
        except oc.OpsConfigError as exc:
            assert exc.code == code, (kwargs, exc.code)
    assert p.read_text(encoding="utf-8") == before_text        # 拒绝＝零变更
    # 草稿允许空示例值（门禁只卡发布态）
    e, _ = oc.publish_service(p, name="草稿", route="/v1/svc/dft",
                              fields=[{"name": "pf_chnl_sts_41", "sample": "", "desc": ""}],
                              status="draft", now=NOW)
    assert e["status"] == "draft" and e["publishedAt"] is None


def test_offline_state_machine():
    """下线：published→offline 留 trace.history 记录；重复下线/下线草稿/未知 id 拒绝。"""
    p = _tmp_services()
    oc.publish_service(p, name="n", route="/v1/svc/xxx", fields=_svc_fields(["pf_chnl_sts_41"]), now=NOW)
    oc.publish_service(p, name="n", route="/v1/svc/dft",
                       fields=[{"name": "pf_chnl_sts_41", "sample": "", "desc": ""}],
                       status="draft", now=NOW)
    entry, before = oc.offline_service(p, "v1-svc-xxx", operator="admin@oneserve", now=NOW)
    assert entry["status"] == "offline" and before["status"] == "published"
    stored = next(r for r in oc.load_console_services(p) if r["id"] == "v1-svc-xxx")
    hist = stored["trace"]["history"]
    assert hist[-1]["status"] == "offline" and hist[-1]["delta"] == "±0"   # AC-2c：下线留痕不走 versions
    assert "versions" not in stored                            # 无版本栏可追加
    for sid, code in (("v1-svc-xxx", "already_offline"), ("v1-svc-dft", "draft_not_offline"),
                      ("v1-svc-none", "service_not_found")):
        try:
            oc.offline_service(p, sid, now=NOW)
            raise AssertionError(f"应拒绝 {sid}")
        except oc.OpsConfigError as exc:
            assert exc.code == code


# ---------- upstreams.yaml ----------

def test_upstreams_lifecycle():
    """缺文件＝空表；发布→草稿（publishedAt 空）→同 route 更新→下线状态机。"""
    d = Path(tempfile.mkdtemp())
    p = d / "upstreams.yaml"
    assert oc.load_upstreams(p) == []
    e1, _ = oc.save_upstream(p, service_name="合作方信息查询", route="/proxy/partner/record-status",
                             upstream_url="http://mock-partner:9100/record", reuse_upstream=True,
                             upstream_name="mock-partner", status="published", now=NOW)
    assert e1["id"] == "proxy-partner-record-status" and e1["status"] == "published"
    assert e1["upstreamName"] == "mock-partner" and e1["publishedAt"]
    e2, _ = oc.save_upstream(p, service_name="合作方目录查询", route="/proxy/partner/policy-list",
                             upstream_url="http://mock-partner:9100/policy", status="draft", now=NOW)
    assert e2["status"] == "draft" and e2["publishedAt"] is None
    assert e2["upstreamName"] == "proxy-partner-policy-list-up"   # 未命名可复用→slug 兜底
    e2b, before = oc.save_upstream(p, service_name="合作方目录查询", route="/proxy/partner/policy-list",
                                   upstream_url="http://mock-partner:9100/policy",
                                   status="published", now=NOW)
    assert before["status"] == "draft" and e2b["status"] == "published"
    assert len(oc.load_upstreams(p)) == 2                       # 同 route 更新不新增
    e1o, _ = oc.offline_upstream(p, e1["id"], now=NOW)
    assert e1o["status"] == "offline"
    try:
        oc.offline_upstream(p, e2b["id"], now=NOW)
        oc.offline_upstream(p, e2b["id"], now=NOW)              # 第二次＝已下线
        raise AssertionError("应拒绝重复下线")
    except oc.OpsConfigError as exc:
        assert exc.code == "already_offline"
    header = p.read_text(encoding="utf-8").splitlines()[1]
    assert "upstreams.yaml" in header                            # 权威源头注释随写保留


def test_delete_upstream_draft_only_and_proxy_namespace():
    """删除草稿（2026-10-20 A12）：仅 draft 可删；已发布须先下线（not_draft 防误删线上）。

    proxy_service_map 只收 published——草稿/下线不对外，不能进签发页。
    """
    d = Path(tempfile.mkdtemp())
    p = d / "upstreams.yaml"
    oc.save_upstream(p, service_name="草稿条目", route="/proxy/a-draft",
                     upstream_url="http://x:9100/a", status="draft", now=NOW)
    pub, _ = oc.save_upstream(p, service_name="已发布条目", route="/proxy/a-pub",
                              upstream_url="http://x:9100/b", status="published", now=NOW)
    assert set(oc.proxy_service_map(p)) == {pub["id"]}, "签发命名空间只应含已发布代理"
    removed, _ = oc.delete_upstream_draft(p, "proxy-a-draft", now=NOW)
    assert removed["id"] == "proxy-a-draft"
    assert [e["id"] for e in oc.load_upstreams(p)] == [pub["id"]]
    for pid, code in ((pub["id"], "invalid_status"), ("proxy-no-such", "proxy_not_found")):
        try:
            oc.delete_upstream_draft(p, pid, now=NOW)
            raise AssertionError(f"应拒绝删除 {pid}")
        except oc.OpsConfigError as exc:
            assert exc.code == code


def test_upstreams_validation():
    """校验拒绝：坏路由/坏 URL/非正超时/负重试/坏可复用名——文件零变更。"""
    d = Path(tempfile.mkdtemp())
    p = d / "upstreams.yaml"
    cases = [
        (dict(service_name="", route="/proxy/a-b"), "invalid_name"),
        (dict(service_name="n", route="/Proxy/A"), "invalid_route"),
        (dict(service_name="n", route="/proxy/a", method="DELETE"), "invalid_method"),
        (dict(service_name="n", route="/proxy/a", upstream_url="ftp://x"), "invalid_upstream_url"),
        (dict(service_name="n", route="/proxy/a", upstream_url="http://x:9100", timeout_sec=0),
         "invalid_timeout"),
        (dict(service_name="n", route="/proxy/a", upstream_url="http://x:9100", retry_count=-1),
         "invalid_retry"),
        (dict(service_name="n", route="/proxy/a", upstream_url="http://x:9100",
              reuse_upstream=True, upstream_name="Bad Name"), "invalid_upstream_name"),
    ]
    for kwargs, code in cases:
        kwargs.setdefault("upstream_url", "http://mock-partner:9100/record")
        try:
            oc.save_upstream(p, now=NOW, **kwargs)
            raise AssertionError(f"应拒绝 {kwargs}（期望 {code}）")
        except oc.OpsConfigError as exc:
            assert exc.code == code, (kwargs, exc.code)
    assert not p.exists()                                       # 全拒绝＝未建文件


def test_atomic_write_no_tmp_residue():
    """原子写（tmp+rename）：写后目录无 *.tmp 残件。"""
    for p in (_tmp_services(), (Path(tempfile.mkdtemp()) / "upstreams.yaml")):
        if p.name == "services.yaml":
            oc.publish_service(p, name="n", route="/v1/svc/xxx", fields=_svc_fields(["pf_chnl_sts_41"]), now=NOW)
        else:
            oc.save_upstream(p, service_name="n", route="/proxy/a",
                             upstream_url="http://mock-partner:9100/record", now=NOW)
        assert not list(p.parent.glob("*.tmp"))


# ---------- HTTP 编排（语法机检：宿主无 fastapi，同 test_appkeys 口径） ----------

def test_admin_api_endpoints_wired():
    """六端点在位，且**不再残留令牌闸**（2026-09-23 机制整体拆除）。

    原测试名 ..._wired_and_token_gated，断言「每端点各过一个 _token_ok 闸」。
    管理令牌机制按老板决定彻底拆除（本机/内网阶段授权边界＝网络层可达；认证归 M3
    接入公司内网时统一做），故该断言随机制下线，改为反向的防回潮断言。
    """
    src = (REPO / "service" / "app" / "admin_api.py").read_text(encoding="utf-8")
    for route in ('"/v1/admin/publish"', '"/v1/admin/publish/{sid}/offline"',
                  '"/v1/admin/upstreams"', '"/v1/admin/upstreams/{pid}/offline"'):
        assert route in src, f"缺端点 {route}"
    # 防回潮：令牌机制已整体拆除，admin_api 不应再出现任何 _token_ok。
    assert src.count("_token_ok") == 0, "admin_api 不应残留令牌闸（机制已整体拆除）"
    py_compile.compile(str(REPO / "service" / "app" / "admin_api.py"), doraise=True)
    py_compile.compile(str(REPO / "service" / "app" / "ops_config.py"), doraise=True)


def test_compose_mount_enables_atomic_write():
    """部署前提：service 目录 rw 挂载（tmp+rename 原子写生效）；镜像补 upstreams.yaml。"""
    compose = (REPO / "docker-compose.yml").read_text(encoding="utf-8")
    assert "- ./service:/app" in compose
    assert "./service/services.yaml:/app/services.yaml:ro" not in compose   # 单文件 ro 挂载已退役
    assert "COPY services.yaml upstreams.yaml auth_matrix.yaml ./" in \
        (REPO / "service" / "Dockerfile").read_text(encoding="utf-8")


# ---------- 前端接线（静态机检；动态断言在 selftest-* 浏览器套件） ----------

def test_frontend_live_wiring():
    """两页 live 分支：apiFetch 真源取数、真源 trace 占位文案、demo 降级保留。

    2026-09-23：原断言「apiFetch 带 X-Admin-Token」。管理令牌机制已按老板决定整体
    拆除（授权边界＝网络层可达；认证归 M3），去掉令牌头断言，apiFetch 真源取数照守。
    """
    pub = (REPO / "admin" / "publish.js").read_text(encoding="utf-8")
    ups = (REPO / "admin" / "upstreams.js").read_text(encoding="utf-8")
    adapter = (REPO / "admin" / "publish-hbase-adapter.js").read_text(encoding="utf-8")
    # 发布台重写（SPEC v1）：端点字面量唯一出处＝适配层（test_publish_hbase 同口径）；
    # 留痕条/演示分支随两段式发布一并删除（维护者 2026-09-29 批准，一步发布）。
    assert "consoleView: '/v1/admin/publish'" in adapter, "适配层缺发布台真源端点"
    assert "apiFetch" in pub, "publish.js 缺真源取数"
    assert "'/v1/admin/publish'" not in pub, "publish.js 不得端写真源端点（归适配层）"
    for dead in ("git 留痕占位", "已提交 git·", "trace-bar", "留痕条"):
        assert dead not in pub, f"publish.js 残留留痕叙事（SPEC §四删）：{dead}"
    # 卡3：表源换 assets（流程②；discovery 归装载页/运维），服务列表/取数口径不变
    assert "loadAssets" in pub and "loadServices" in pub, "publish.js 缺真源加载方法"
    # 上游页口径不变（未重写）
    assert "'/v1/admin/upstreams'" in ups, "upstreams.js 缺真源端点"
    assert "apiFetch" in ups and "finishDemo" in ups and "loadLive" in ups
    assert "mock/upstreams.json" in ups, "上游演示降级仍在"
    # 发布台挡板：只在驱动脚本（m25_selftest_browser.mjs）伺服，页面 js 代码零 mock 读取
    # （头注释里的挡板数据路径说明属文档，不计——剥注释后断言）
    pub_code = re.sub(r"/\*.*?\*/", "", pub, flags=re.S)
    assert "mock/" not in pub_code, "publish.js 代码不得读取 mock（诚信线：不回落示例数据）"
    assert (REPO / "admin" / "mock" / "publish-wizard.json").is_file(), "缺发布台挡板数据"


def test_portal_artifacts_rebuilt():
    """构建产物同步：portal/admin 拷贝含新端点/向导组件（陈旧则先重建，防漏同步）。"""
    out = REPO / "portal" / "admin"
    if "publishWizard" not in (out / "publish.js").read_text(encoding="utf-8"):
        subprocess.run([sys.executable, str(REPO / "scripts" / "build_portal.py")],
                       check=True, capture_output=True)
    assert "publishWizard" in (out / "publish.js").read_text(encoding="utf-8"), "产物缺向导组件"
    assert "consoleView: '/v1/admin/publish'" in (out / "publish-hbase-adapter.js").read_text(encoding="utf-8"), \
        "产物适配层缺真源端点"
    assert "/v1/admin/upstreams" in (out / "upstreams.js").read_text(encoding="utf-8")


# ---------- P1-10 差量：生效范围收敛（先测试/直接上生产 → env×instance 派生） ----------

def test_derive_env_instance_modes():
    """derive_env_instance：先测试→测试T1×网关实例B；直接上生产→本机M0×网关实例A；
    空/未知值从严回落生产（不猜测试环境）。"""
    assert oc.derive_env_instance("先测试") == ("测试T1", "网关实例B")
    assert oc.derive_env_instance("直接上生产") == ("本机M0", "网关实例A")
    assert oc.derive_env_instance("  先测试  ") == ("测试T1", "网关实例B")   # 去空白容错
    for bad in ("", None, "今天上线", "测试T1"):
        assert oc.derive_env_instance(bad) == ("本机M0", "网关实例A"), bad


def test_publish_release_mode_derives_scope():
    """发布台：release_mode 派生落盘 scope（先测试/直接上生产/未知回落），无显式 env 时生效。"""
    p = _tmp_services()
    e_test, _ = oc.publish_service(p, name="n", route="/v1/svc/chn",
                                   fields=_svc_fields(["pf_chnl_sts_41"]),
                                   release_mode="先测试", now=NOW)
    assert e_test["env"] == "测试T1" and e_test["instance"] == "网关实例B"
    stored = oc.load_console_services(p)[0]
    assert stored["scope"] == {"env": "测试T1", "instance": "网关实例B"}

    e_prod, _ = oc.publish_service(p, name="n", route="/v1/svc/chn",
                                   fields=_svc_fields(["pf_chnl_sts_41"]),
                                   release_mode="直接上生产", now=NOW)
    assert (e_prod["env"], e_prod["instance"]) == ("本机M0", "网关实例A")

    e_unknown, _ = oc.publish_service(p, name="n", route="/v1/svc/chn",
                                      fields=_svc_fields(["pf_chnl_sts_41"]),
                                      release_mode="天外飞仙", now=NOW)
    assert (e_unknown["env"], e_unknown["instance"]) == ("本机M0", "网关实例A")
    e_default, _ = oc.publish_service(p, name="n", route="/v1/svc/chn",
                                      fields=_svc_fields(["pf_chnl_sts_41"]), now=NOW)
    assert (e_default["env"], e_default["instance"]) == ("本机M0", "网关实例A")   # 缺省＝直上


def test_publish_explicit_scope_wins_backward_compat():
    """向后兼容：显式 env/instance（存量调用方/手编旧值）优先于 release_mode 派生。"""
    p = _tmp_services()
    # 双显式：即便 release_mode=直接上生产，也以手选二元组为准（存量语义不变）
    e, _ = oc.publish_service(p, name="n", route="/v1/svc/chn", fields=_svc_fields(["pf_chnl_sts_41"]),
                              env="测试T1", instance="网关实例B", release_mode="直接上生产", now=NOW)
    assert (e["env"], e["instance"]) == ("测试T1", "网关实例B")
    # 半显式：显式 instance 保留，env 按 release_mode 派生
    e2, _ = oc.publish_service(p, name="n", route="/v1/svc/chn", fields=_svc_fields(["pf_chnl_sts_41"]),
                               instance="网关实例B", release_mode="先测试", now=NOW)
    assert (e2["env"], e2["instance"]) == ("测试T1", "网关实例B")


def test_stored_legacy_scope_reads_back():
    """存量 yaml 条目显式 env/instance 原样读出（_svc_view 兼容旧值，不回改写区）。"""
    p = _tmp_services()
    oc.publish_service(p, name="n", route="/v1/svc/legacy", fields=_svc_fields(["pf_chnl_sts_41"]),
                       env="测试T1", instance="网关实例B", now=NOW)
    view = oc.build_console_view(services_path=p)
    row = [s for s in view["services"] if s["id"] == "v1-svc-legacy"][0]
    assert row["env"] == "测试T1" and row["instance"] == "网关实例B"


def test_save_upstream_release_mode_derives_scope():
    """上游代理：save_upstream 同口径接受 release_mode 并派生 scope。"""
    p = Path(tempfile.mkdtemp()) / "upstreams.yaml"
    e, _ = oc.save_upstream(p, service_name="n", route="/proxy/a",
                            upstream_url="http://mock-partner:9100/x",
                            release_mode="先测试", now=NOW)
    assert (e["env"], e["instance"]) == ("测试T1", "网关实例B")
    assert oc.load_upstreams(p)[0]["scope"] == {"env": "测试T1", "instance": "网关实例B"}
    e2, _ = oc.save_upstream(p, service_name="n", route="/proxy/a",
                             upstream_url="http://mock-partner:9100/x",
                             env="本机M0", instance="网关实例A",
                             release_mode="先测试", now=NOW)
    assert (e2["env"], e2["instance"]) == ("本机M0", "网关实例A")   # 显式优先


def test_admin_api_reads_release_mode():
    """HTTP 编排：admin_api 两写端点读 releaseMode 并传入 ops_config（链路不再断）。"""
    src = (REPO / "service" / "app" / "admin_api.py").read_text(encoding="utf-8")
    # 两个写端点各一处：前端 releaseMode 字段不再被静默丢弃
    assert src.count('release_mode=str(p.get("releaseMode") or "")') == 2, \
        "publish/upstreams 两写端点须均读 releaseMode"


def test_describe_scope_read_side():
    """读侧人话口径：两选映射命中回「先测试/直接上生产」；存量旧值如实回 env × instance。"""
    assert oc.describe_scope({"env": "测试T1", "instance": "网关实例B"}) == "先测试"
    assert oc.describe_scope({"env": "本机M0", "instance": "网关实例A"}) == "直接上生产"
    assert oc.describe_scope({"env": "自建机房", "instance": "网关实例Z"}) == "自建机房 × 网关实例Z"
    assert oc.describe_scope(None) == "本机M0 × 网关实例A"


def test_views_expose_release_mode():
    """读视图（列表/写区）带 releaseMode 人话栏，供前端列表直显（改动最小、不破旧 env/instance）。"""
    p = _tmp_services()
    oc.publish_service(p, name="n", route="/v1/svc/chn", fields=_svc_fields(["pf_chnl_sts_41"]),
                       env="测试T1", instance="网关实例B", now=NOW)
    row = oc.build_console_view(services_path=p)["services"][0]
    assert row["releaseMode"] == "先测试" and row["env"] == "测试T1"
    up = Path(tempfile.mkdtemp()) / "up.yaml"
    px, _ = oc.save_upstream(up, service_name="n", route="/proxy/a",
                             upstream_url="http://mock-partner:9100/x", release_mode="先测试", now=NOW)
    assert px["releaseMode"] == "先测试"


def test_field_domains_expose_cn_name():
    """字段双列数据源（P1-10 差量）：field_domains 每字段带非空 cnName（字典真源）。"""
    domains = oc.field_domains()
    assert domains, "字段字典缺失"
    names = {}
    for d in domains:
        for f in d["fields"]:
            assert f.get("cnName"), f"字段 {f['name']} 缺 cnName"
            names[f["name"]] = f["cnName"]
    assert names["pf_chnl_sts_41"] == "渠道偏好字段001"
# ---------- R2 回环防线（发布链路 × 漂移判定口径闭环） ----------

def test_publish_records_field_space_and_rejects_unknown_wide_field():
    """发布记录 field_space；宽表域引用字典外字段（语义占位符）→ 拒绝发布、文件零变更。"""
    p = _tmp_services()
    before_text = p.read_text(encoding="utf-8")
    # 宽表域 + 真列 → 放行，条目记 field_space=wide297
    e, _ = oc.publish_service(p, name="宽表服务", route="/v1/svc/wide-ok",
                              fields=_svc_fields(["pf_chnl_sts_41"]), now=NOW)
    assert e["fieldSpace"] == "wide297"
    assert oc.load_console_services(p)[0]["field_space"] == "wide297"
    # 宽表域 + 语义占位符 tags → 拒绝（假阳性后门关闭）
    with pytest.raises(oc.OpsConfigError) as ei:
        oc.publish_service(p, name="占位服务", route="/v1/svc/backdoor",
                           fields=_svc_fields(["tags"]), now=NOW)
    assert ei.value.code == "field_not_in_dict" and "tags" in ei.value.message
    assert "宽表" in ei.value.message                      # 人话给出订正方向
    # 声明新闻域后同一字段放行（语义占位符合法）
    e2, _ = oc.publish_service(p, name="资讯服务", route="/v1/svc/news-ok",
                               fields=_svc_fields(["tags"]), field_space="news", now=NOW)
    assert e2["fieldSpace"] == "news"
    # 非法 field_space → 拒绝
    with pytest.raises(oc.OpsConfigError) as ei2:
        oc.publish_service(p, name="坏空间", route="/v1/svc/bad-space",
                           fields=_svc_fields(["pf_chnl_sts_41"]), field_space="typo", now=NOW)
    assert ei2.value.code == "invalid_field_space"


def test_publish_wide_field_gate_dict_injection():
    """dict_cols 注入口径：None→自读真源（真列外拦截）；显式列集→受控放行；
    空列集（模拟 CSV 不可读）→ 跳过校验（不因解析不出字典而误挡发布）。"""
    p = _tmp_services()
    # None：自读真 CSV（可读）→ 非真列仍拦截
    with pytest.raises(oc.OpsConfigError) as ei:
        oc.publish_service(p, name="n", route="/v1/svc/xxx",
                           fields=_svc_fields(["not_a_real_col"]), dict_cols=None, now=NOW)
    assert ei.value.code == "field_not_in_dict"
    # 显式传入宽松列集＝受控放行（调用方可注入，便于离线/演练）
    e2, _ = oc.publish_service(p, name="n2", route="/v1/svc/yyy",
                               fields=_svc_fields(["not_a_real_col"]),
                               dict_cols={"not_a_real_col"}, now=NOW)
    assert e2["id"] == "v1-svc-yyy"


def test_publish_to_drift_loopback_no_false_quarantine():
    """回环：经发布链路落定的条目喂给漂移对账——宽表域真列 ok、资讯域占位符 not_applicable，
    无一被误隔离（发布口径与漂移判定口径闭环）。"""
    import importlib.util
    sys.path.insert(0, str(REPO / "sync"))
    spec = importlib.util.spec_from_file_location("reconcile_drift", REPO / "scripts" / "reconcile_drift.py")
    recon = importlib.util.module_from_spec(spec); spec.loader.exec_module(recon)
    from app import schema_drift as sd

    p = _tmp_services()
    oc.publish_service(p, name="宽表", route="/v1/svc/wide",
                       fields=_svc_fields(["pf_chnl_sts_41"]), now=NOW)
    oc.publish_service(p, name="资讯", route="/v1/svc/news",
                       fields=_svc_fields(["tags"]), field_space="news", now=NOW)
    services = [{"service_id": "v1-svc-wide", "field_space": "wide297"},
                {"service_id": "v1-svc-news", "field_space": "news"}]
    matrix = [{"grants": [{"service": "v1-svc-wide", "fields": ["pf_chnl_sts_41"]},
                         {"service": "v1-svc-news", "fields": ["tags"]}]}]
    refs = sd.referenced_fields(services, matrix)
    spaces = sd.field_space_by_service(services)
    class _R:
        def __init__(self): self.kv = {}
        def get(self, k): return self.kv.get(k)
        def set(self, k, v): self.kv[k] = v
    r = _R()
    report = recon.check_schema_drift(r, refs, recon.load_dict_columns(), spaces)
    assert report["v1-svc-wide"]["state"] == sd.STATE_OK
    assert report["v1-svc-news"]["state"] == sd.STATE_NOT_APPLICABLE
    assert not any(s["state"] == sd.STATE_QUARANTINED for s in report.values()), report


def test_admin_publish_endpoint_forwards_field_space():
    """HTTP 编排：publish 端点把 fieldSpace 透传给 publish_service（AST 契约，宿主无 fastapi）。"""
    import ast as _ast
    src = (REPO / "service" / "app" / "admin_api.py").read_text(encoding="utf-8")
    tree = _ast.parse(src)
    fn = next(n for n in _ast.walk(tree)
              if isinstance(n, _ast.AsyncFunctionDef) and n.name == "publish_service_ep")
    body = _ast.unparse(fn)
    assert "field_space" in body and "fieldSpace" in body, "发布端点未透传字段空间"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"[ok] {fn.__name__}")
        except Exception as exc:   # noqa: BLE001 —— 直跑模式聚合报错（pytest 模式各自独立）
            failed += 1
            print(f"[FAIL] {fn.__name__}: {type(exc).__name__}: {exc}")
    sys.exit(1 if failed else 0)
