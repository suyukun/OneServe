# -*- coding: utf-8 -*-
"""开发占位管理令牌的可见性纯逻辑（管理令牌可见卡 · 2026-09-23）。

2026-10-14：管理令牌闸已第三次反转拆除（维护者 拍板，见 docs/adr/0010）——_admin_gate
恒放行、compose/.env.example 注入行注释停用；本文件保留为令牌值单一事实来源，
登录页时代或恢复闸期复用（恢复形态见 commit bcf34f8）。恒放行期以下可见性端点
（/v1/portal/admin-token-hint）仍在伺服但前端 shim 不再拉取。

背景：管理令牌（X-Admin-Token）此前只写在 .env.example 与代码缺省值里，使用者
「不知道它是什么」。维护者 决策：不改令牌值，而是让它在开发页面上可见。

安全底线（本模块是唯一裁决点）：
  · 只有「开发占位态」（ADMIN_API_TOKEN 未显式配置，或显式等于内置默认值）才下发真值；
  · 一旦部署方把 ADMIN_API_TOKEN 显式设为非默认值，本模块不下发任何令牌片段
    （连掩码都不给），前端只显示「已自定义、不在页面显示」——即「只在开发占位态可见」。

分层纪律：本模块零依赖（不 import fastapi），供 service/app/admin_api.py 与
service/app/portal_api.py 共用（单一事实来源）；宿主无 fastapi 也可直跑单测
（同 service/app/monitor.py 口径）。
"""
from __future__ import annotations

import os

# 内置默认值＝开发占位值；admin_api 的 os.environ 缺省引用本常量（单一事实来源，防两处漂移）。
DEV_DEFAULT_TOKEN = "admin321"

# 环境变量名（与 admin_api/portal_api 共用；.env 可覆盖）。
ADMIN_ENV_KEY = "ADMIN_API_TOKEN"

PLACEHOLDER_NOTE = (
    "开发占位令牌（本机开发/演示专用）：生产环境将改为严格认证方式，"
    "届时本提示消失且不再下发令牌值"
)
CUSTOM_NOTE = (
    "当前管理令牌已由部署方自定义（非默认开发占位值）：按安全口径不在页面显示，"
    "请查阅部署配置或运维手册"
)


def runtime_admin_token() -> str:
    """运行期实际生效的管理令牌（环境变量优先，缺省＝开发占位默认值）。"""
    return os.environ.get(ADMIN_ENV_KEY, DEV_DEFAULT_TOKEN)


def is_placeholder(admin_token: str) -> bool:
    """是否处于开发占位态：令牌等于内置默认值（即部署方未显式改值）。"""
    return admin_token == DEV_DEFAULT_TOKEN


def dev_token_view(admin_token: str) -> dict:
    """开发令牌提示视图（端点出参口径的单一事实来源）。

    占位态→下发真值＋「开发占位」标注；非占位态→不下发任何令牌片段（不泄漏真值）。
    """
    if is_placeholder(admin_token):
        return {"devOnly": True, "placeholder": True, "token": admin_token,
                "note": PLACEHOLDER_NOTE}
    return {"devOnly": True, "placeholder": False, "token": None,
            "note": CUSTOM_NOTE}
