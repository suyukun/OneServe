#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""pytest 收集期守门（CI fail-loud）：核心依赖缺席＝collection error，绝不静默 skip。

为什么需要（requirements-dev.txt 头注的事故复盘）：多个套件在依赖缺席时走
`_HAS_JWT=False` → print+return / pytest.skip 降级，全量照样「绿」——缺包不会响亮
失败，只会把角色闸/JWT 断言悄悄吞掉。CI 里这种假绿比红灯更贵（本地绿、线上炸）。

做法：本文件模块顶层**无条件** import 跨套件关键依赖；任一缺席即在收集阶段报
`ERROR collecting tests/conftest.py`，整轮 pytest 非零退出，不会被任何 skip 兜住：
  · redis  —— service/app/main.py 顶层裸 import redis.asyncio（缺它 11 个文件收集中断）
  · yaml   —— services.yaml / auth_matrix.yaml 解析（tests+service 共 20 个文件顶层裸 import）
（bcrypt/jwt 曾为登录套件 fail-loud 守卫；登录模块已随 v0.1.1 清理删除，ADR-0029 §7。）

边界：只做 import 断言，不注入 fixture、不改任何测试行为、不接管 skip 逻辑。
依赖清单版本＝requirements-dev.txt（固化版，Python 3.13 实测基线）。
"""
from __future__ import annotations

import redis  # noqa: F401   service/app 顶层 redis.asyncio
import yaml  # noqa: F401    services.yaml / auth_matrix.yaml 解析
