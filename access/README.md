# OneServe 标签 API 接入：手机号 SM3 口径说明（task-011 · 官方参考实现）

> 为什么有这份文档：各合作方此前各自实现哈希，口径稍一漂移，查询就**静默返回空值**
> （SPEC v1 §4.2 K1 决策）。接入方请**照抄本文口径**，并在联调前用 `precheck.py` 对答案。

## 一、规范化规则表（先规范化，再哈希；顺序固定）

| # | 规则 | 输入示例 | 规范化后 |
|---|------|---------|---------|
| 1 | 去空格（半角空格与全角空格） | `138 0013 8000` | `13800138000` |
| 2 | 去连字符（半角 `-` 与全角 `－`） | `138-0013-8000` | `13800138000` |
| 3 | 去 `+86` 前缀（**仅带加号的**；不带加号的 `86` 开头**不去**，宁可在预检暴露也不静默猜） | `+8613800138000` | `13800138000` |
| 4 | 字母小写（手机号正常不含字母，手滑兜底） | `+86A`（示例） | `+86a` |

流程：**规范化 → UTF-8 编码 → SM3 → 64 位小写十六进制字符串**。
示例：`+86 138-0013-8000` ≡ `13800138000`，二者哈希必须相同。

## 二、Python（全码，抄走即用）

```python
from gmssl import func, sm3   # pip3 install gmssl

def normalize(raw: str) -> str:
    s = raw.replace(" ", "").replace("\u3000", "")          # 1) 去空格
    s = s.replace("-", "").replace("\uff0d", "")            # 2) 去连字符
    if s.startswith("+86"):                                  # 3) 去 +86 前缀
        s = s[3:]
    return s.lower()                                         # 4) 字母小写

def phone_sm3(raw: str) -> str:
    return sm3.sm3_hash(func.bytes_to_list(normalize(raw).encode("utf-8")))
```

自检锚点：`phone_sm3("abc")`（即对字符串 `abc` 直接哈希）应等于
`66c7f0f462eeedd9d1f2d46bdc10e4e24167c4875cf2f7a2297da02b8f4ba8e0`（国标 GM/T 0004-2012 向量）。

## 三、一行命令自测

```bash
python3 access/sm3_ref.py "+86 138-0013-8000"
# 输出应与 python3 access/sm3_ref.py 13800138000 完全一致
```

## 四、联调预检（上线/联调前必做）

1. 出题方（OneServe 侧）：
   ```bash
   python3 access/precheck.py --generate 10 --out answers.csv --seed 42
   # stdout 打印题目（手机号清单），answers.csv 由出题方保管、不给应答方
   ```
2. 应答方（合作方）：对每道题按本文口径算 SM3，回填 CSV（`phone,hash` 两列，
   phone 照抄题目原样，勿改写），存成 `results.csv`。
3. 出题方校验：
   ```bash
   python3 access/precheck.py --verify results.csv --answers answers.csv
   # 逐条打印 一致/不一致 清单＋汇总；全绿退出 0，任一 不一致/缺失/多余 退出 1
   ```

题目故意混有 `+86`/空格/连字符样式，专门钓规范化口径漂移；跑不过请回第二节逐条对照。

## 五、其他语言

- **curl**：哈希在调用方本地算好随请求携带，curl 只负责带参请求，无需特殊处理。
- **Java**：一行说明——用 BouncyCastle 的 `SM3Digest`（或 hutool `SmUtil.sm3`）对规范化后的 UTF-8 字节取摘要转小写 hex。

## 六、常见翻车对照

| 症状 | 原因 |
|-----|------|
| 预检「不一致」但哈希看着像 | 哈希大写了；SM3 输出须小写 hex |
| 裸 `86` 开头的题不一致 | 不带加号的 `86` 前缀不去，按原样参与哈希 |
| 全部不一致 | 编码不是 UTF-8，或哈希了未经规范化的原文 |
| 大量「缺失」 | 回填 phone 列被 Excel 改写（科学计数法等）；请照抄题目原样并以 CSV 文本方式编辑 |

---

题目手机号均为**随机伪造**（1[3-9]+9 位随机数字，非真实数据）；除 `--out`/回填文件外不落盘。
本工具不占端口、不依赖 Docker；依赖仅 `gmssl`（Python 3.9+）。
