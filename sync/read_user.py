# -*- coding: utf-8 -*-
"""读取方 CLI（演练用）：经 meta 指针读一个用户的线上标签。

口径＝线上点查：GET tags:user:latest → 版本 → GET 数据键；返回带 data_as_of，
无该用户/空标签返回 found=false（对外即 null，SPEC §4.3 时点诚实）。

用法：
  python3.13 sync/read_user.py <上游供数键>
"""
import json
import sys

from common import read_user, redis_conn


def main():
    if len(sys.argv) != 2:
        print("用法: python3.13 sync/read_user.py <键>", file=sys.stderr)
        return 2
    sm3 = sys.argv[1].strip().lower()
    r = redis_conn()
    print(json.dumps(read_user(r, sm3), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
