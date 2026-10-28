#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""合成服务/授权 fixture（2026-09-29 全服务下线后的测试基建）。

线上 service/services.yaml 已按 维护者 决定清空（services: []），机制类测试不再有
真实服务条目可引用。本模块以 git HEAD(5455288) 的注册条目为底本提供合成 fixture：
  - 形状与真实条目逐字段一致（user_tag_query / news_tag_query / partner_proxy 原样）；
  - hbase 型条目改指业务表 default:usr_basic_info（demo 表已删，字典
    sync/schema_usr_basic_info.csv 共 12 列）；
  - 仅存在于测试内存态/临时文件，绝不写回 service/services.yaml 或 auth_matrix.yaml。
现实态断言（空注册表/矩阵无孤儿授权/路由不渲染）由各套件自己的 reality 用例守卫。
"""
import sys
import tempfile
from pathlib import Path

import yaml

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / 'service'))

from app import kv_executor as _kvx  # noqa: E402

_SERVICES_YAML = """services:
- service_id: user_tag_query
  archetype: kv_lookup
  field_space: wide297
  backend:
    kv:
      namespace: tags:user
      alias_key: tags:user:latest
      data_key: tags:user:{version}:{sm3}
      version_meta_key: sync:ver:{version}
      storage_key: as_received
      entity: user
  params:
  - name: key
    type: string(1..128)
    key_type: phone_sm3
    pattern: ^[A-Za-z0-9_.-]{1,128}$
  response:
    projection: auth_matrix
    desensitize:
      if_acct_fct_24:
        type: band_mask
        bands:
        - max: 300000
          out: A
        - max: 600000
          out: B
        - out: C
        unknown_out: U
      if_bse_fct_04:
        type: band_mask
        bands:
        - max: 100000
          out: A
        - max: 500000
          out: B
        - out: C
        unknown_out: U
    meta:
    - data_as_of
    - live_version
  guard:
    timeout_ms: 3000
    max_items_per_call: 1
  audit:
    enabled: true
    log_params: hash_only
    log_response_digest: sha256_of_desensitized_body
# 2026-10-09：main.py 正式/演示通道改指现役 v1-svc-usr-asset-tag（user_tag_query 已删），
# 端点机制测试需要注册表与矩阵有同名条目（形状照 service/services.yaml 真源）。
- service_id: v1-svc-usr-asset-tag
  archetype: kv_lookup
  # field_space 注：真源 services.yaml 该条目标 wide297 系 297 字典时代历史标记；
  # 现役 18 字段属新表级 schema，对旧 297 字典跑漂移必隔离（B1 表结构登记的结构债）。
  # fixture 用独立空间＝如实不参与 297 漂移域，防「零误隔离」防线被历史标记误伤。
  field_space: usr_asset_tag
  backend:
    kv:
      namespace: tags:usr_asset_tag
      alias_key: tags:usr_asset_tag:latest
      data_key: tags:usr_asset_tag:{version}:{sm3}
      version_meta_key: sync:ver:usr_asset_tag:{version}
      storage_key: as_received
      entity: user
  params:
  - name: key
    type: string(64)
    key_type: phone_sm3
    pattern: ^[A-Za-z0-9_.-]{1,128}$
  response:
    projection: auth_matrix
    # 2026-10-15：批量链路改指本服务后，worker 机制测试（投影×脱敏×钉版本）需要
    # desensitize 规则形状——照 user_tag_query 旧条目 band_mask 同形配置到白名单内
    # 字段 aum_band（合成 fixture 机制测试用；真源该服务当前值即档位、未配脱敏）。
    desensitize:
      aum_band:
        type: band_mask
        bands:
        - max: 300000
          out: A
        - max: 600000
          out: B
        - out: C
        unknown_out: U
    meta:
    - data_as_of
    - live_version
  guard:
    timeout_ms: 3000
    max_items_per_call: 1
  audit:
    enabled: true
    log_params: hash_only
    log_response_digest: sha256_of_desensitized_body
- service_id: news_tag_query
  archetype: kv_lookup
  field_space: news
  backend:
    kv:
      namespace: tags:news
      alias_key: tags:news:latest
      data_key: tags:news:{version}:{sm3}
      version_meta_key: sync:ver:news:{version}
      storage_key: as_received
      entity: news
  params:
  - name: key
    type: string(4..64)
    key_type: news_id
    pattern: ^[a-z0-9]{4,64}$
  response:
    projection: auth_matrix
    meta:
    - data_as_of
    - live_version
  guard:
    timeout_ms: 3000
    max_items_per_call: 1
  audit:
    enabled: true
    log_params: hash_only
    log_response_digest: sha256_of_desensitized_body
- service_id: partner_proxy
  archetype: external_proxy
  field_space: proxy
  backend:
    proxy:
      upstream_url: http://oneserve-mock-partner:9100
      timeout_ms: 2000
      retry: 1
  retention:
    enabled: true
    dir: /data/retention/partner
  params:
  - name: key
    type: string(1..128)
    key_type: partner_query_key
  guard:
    timeout_ms: 3000
  audit:
    enabled: true
    log_params: hash_only
    log_response_digest: sha256_of_body
- service_id: usr_basic_info_query
  archetype: kv_lookup
  field_space: usr_basic_info
  backend:
    source: hbase
    hbase:
      table: default:usr_basic_info
      cf: cf
      key_type: phone_sm3
  params:
  - name: key
    type: string(1..128)
    key_type: phone_sm3
    pattern: ^[A-Za-z0-9_.-]{1,128}$
  response:
    projection: auth_matrix
    desensitize:
      risk_level:
        type: band_mask
        bands:
        - max: 3
          out: R12
        - out: R345
        unknown_out: U
    meta: []
  guard:
    timeout_ms: 3000
    max_items_per_call: 1
  audit:
    enabled: true
    log_params: hash_only
    log_response_digest: sha256_of_desensitized_body
"""

_MATRIX = [{'tenant': 'demo_bank', 'appkey': 'oneserve-dev-appkey-001', 'oa_ticket': 'OA-PENDING-TASK008', 'grants': [{'service': 'user_tag_query', 'endpoint': '/v1/tags/query', 'fields': ['ac_active_sts_01', 'ac_active_sts_02', 'ac_bussi_sts_01', 'ac_login_fct_01', 'ac_login_sts_02', 'ac_point_sts_05', 'ac_vist_sts_01', 'mk_touch_sts_02', 'if_acct_fct_24', 'if_bse_fct_04']}, {'service': 'v1-svc-usr-asset-tag', 'endpoint': '/v1/tags/query', 'fields': ['aum_band', 'dep_bal_band', 'wealth_hold_flag', 'fund_hold_flag', 'ins_hold_flag', 'loan_flag', 'mortgage_flag', 'credit_limit_band', 'overdue_cnt_band', 'active_m3_flag', 'dormant_m12_flag', 'login_cnt_band', 'pay_cnt_band', 'salary_in_flag', 'cross_sell_cnt_band', 'churn_risk_band', 'ltv_band', 'mk_response_band']}, {'service': 'news_tag_query', 'endpoint': '/v1/news/query', 'fields': ['tags']}, {'service': 'partner_proxy', 'endpoint': '/v1/proxy/partner', 'fields': ['passthrough']}, {'service': 'usr_basic_info_query', 'endpoint': '/v1/hbase/query', 'fields': ['gender_cd', 'age_band', 'city_code', 'cust_level', 'risk_level', 'vip_flag']}]}]


def load_synth_services() -> dict:
    """合成注册表（service_id 单键，同 kvx.load_services 口径；临时文件承载）。"""
    path = Path(tempfile.mkdtemp()) / 'services.yaml'
    path.write_text(_SERVICES_YAML, encoding='utf-8')
    return _kvx.load_services(path)


def synth_services_dict() -> dict:
    """扁平 dict 视图（service_id -> 条目；单键注册表下两视图同构）。"""
    reg = load_synth_services()
    return {k: v for k, v in reg.items() if isinstance(k, str)}


def synth_matrix() -> list:
    """合成授权矩阵（demo_bank：user/news/partner/usr_basic_info_query 四授权）。"""
    return yaml.safe_load(yaml.safe_dump(_MATRIX, allow_unicode=True))


def real_registry_state():
    """现实态：全下线终态下 load_services 对空表 fail-loud（RuntimeError）。

    返回 (registry, load_error)：正常上线态 error=None；空表终态 registry={} 且
    error 为异常文本（服务层拒绝空启动是产品红线，不是测试障碍）。
    """
    try:
        return _kvx.load_services(), None
    except RuntimeError as exc:
        return {}, str(exc)


def real_matrix_state():
    """现实态：空矩阵同样 fail-loud（RuntimeError）。返回 (matrix, error)。"""
    try:
        return _kvx.load_auth_matrix(), None
    except RuntimeError as exc:
        return [], str(exc)


def synth_services_path() -> Path:
    """合成注册表文件路径（gen_openapi/loader 等需要文件真源的消费方）。"""
    path = Path(tempfile.mkdtemp()) / 'services.yaml'
    path.write_text(_SERVICES_YAML, encoding='utf-8')
    return path


def synth_matrix_path() -> Path:
    """合成授权矩阵文件路径（load_auth_endpoints/load_auth_fields 等文件消费方）。"""
    path = Path(tempfile.mkdtemp()) / 'auth_matrix.yaml'
    path.write_text(yaml.safe_dump({'matrix': _MATRIX}, allow_unicode=True), encoding='utf-8')
    return path
