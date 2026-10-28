---
file: DESIGN.md
version: v1.1
date: 2026-10-15
status: canonical（v1 对抗复核修订版·五组修订已落实）
scope: portal/src/*.html（11 页）· admin/*.html（13 页，含 3 selftest）· portal/admin/*.html（13 页产物镜像）· 全部新增 UI
authority: portal/assets/tokens.css   # CSS 为权威源，本文件是人读＋门禁镜像
machine_readable: [§1 Foundations, §2 Components, §4 Accessibility]
enforcement: scripts/design_gate.mjs(10 项断言) + stylelint(增量) + BackstopJS(admin域·待装浏览器) + pangu
---

# 0. Meta

- 权威源优先级：`tokens.css > DESIGN.md > 口头约定`。冲突以 CSS 为准，并同 PR 修本文件。
- 同 PR 铁律：改 token 的同一 PR 必改 ① tokens.css ② 本文件对应条目 ③ 门禁规则与基线；缺一即 review 打回。
- **token 值变更＝强制全量重摄视觉基线事件**：改 token 定义值（非仅增删）→ 同 PR 必须重摄 BackstopJS 全量基线＋人工过图确认。防静默回归。（v1.1 新增·复核 P0-2）
- 变更流程：新值先落 tokens.css 并在本文件登记，再使用；未登记的值视同违规。
- 已批项：D1 滚动模型（§3.1）、D2 响应式（§3.2）、D3 CJK 折行（§4）、D4 间距双轨制（§1）。v1 待批标记已去除。

# 1. Foundations（tokens · 机器可读）

```yaml
colors:            # 值序 [深色, 浅色]；深色为现行主口径；浅色口径值以 tokens.css :root 为准
  brand:           # brand-primary 仅主按钮/进度当前态/关键数字，≤2 处/页
    brand-primary: ["#E85B6B", "#C8102E"]
    brand-on: ["#14161a", "#ffffff"]      # 主按钮字色（品牌伴生值）
  status:          # 只作语义不作装饰；灯旁必配文字
    status-ok:    ["#3fb965", "#009423"]   # 在线/成功/已发布
    status-error: ["#ef6b6b", "#CB272D"]   # 异常/失败/危险
    status-warn:  ["#e2a03f", "#D4760A"]   # 警告/降级/敏感
    status-info:  ["#6ea8fe", "#2563eb"]   # 进行中/链接/中性通知
  neutral:         # 组件只用语义别名，禁直引 --neutral-*
    text-strong:  ["#e8ebf1", "#262626"]   # 标题/大数字
    text-body:    ["#bcc4d0", "#434343"]   # 正文
    text-muted:   ["#828d9c", "#595959"]   # 辅助/来源
    border-strong: ["#2e3c50", "#d9d9d9"]  # 卡片/输入外框
    border-light: ["#253040", "#f0f0f0"]   # 内部分隔/表格行线
    bg-page:      ["#12151c", "#fafafa"]
    bg-card:      ["#1a2029", "#ffffff"]
    bg-subtle:    ["#202836", "#f5f5f5"]   # 侧导航/内嵌底
    bg-inset:     ["#263244", "#f0f0f0"]   # 骨架屏/代码块；--bg-input 为其别名
  restricted:      # 受控附属，禁扩展用途
    chart-1..7: 仅数据可视化（深底 chart-1=#7b93ec）
    warn-ink: ["#4a3305", "#6b4a00"]       # 黄底徽标字
  forbidden: [text-faint]                   # 废止；存量改 --text-muted（存量 23 用·入迁移清单）
typography:        # 封闭 scale；行高无单位；字重仅 400/600
  font-sans: "-apple-system,BlinkMacSystemFont,'PingFang SC','Microsoft YaHei','Segoe UI',sans-serif"
  font-mono: "ui-monospace,SFMono-Regular,Menlo,Consolas,monospace"
  fs-caption: {px: 12, lh: 1.5, use: 角标/来源/表头/徽标}
  fs-body:    {px: 13, lh: 1.6, use: 正文/表格/表单}
  fs-title:   {px: 16, lh: 1.5, use: 卡片题/按钮}
  fs-heading: {px: 20, lh: 1.4, use: 页面题 H1}
  fs-display: {px: 32, lh: 1.2, use: KPI/完成态数字}
  legacy:
    fs-small: "=fs-caption(12px)"          # 存量 92 用·入迁移清单→fs-caption
    fs-tiny: "11px·废止→fs-caption"        # 存量 92 用(--fs-tiny)·入迁移清单
    fs-kpi: "36px·废止→fs-display"         # 存量 5 用·入迁移清单
spacing:           # 【v1.1 D4 双轨制】不重定义现有 --space-*；新增 --os-space-* 为新代码唯一合法 scale
  os-space-1: 4   # 组件内微观（图标与文字/徽标内距）
  os-space-2: 8   # 组件内边距（按钮/输入）
  os-space-3: 12  # 组件紧邻（表单行/表格单元）
  os-space-4: 16  # 组件常规（卡内区块/列表行）
  os-space-5: 24  # 区块间（卡间/表单分组）
  os-space-6: 32  # 大区块间（页头与内容）
  os-space-7: 48  # 页面级留白（完成页/空态）
  legacy:          # 旧 --space-1/2/3=8/16/24 ＋ --space-4..7=16/24/32/48（共 378 引用·入存量迁移清单·不改值防静默回归）
    space-1: "8px·71 引用"
    space-2: "16px·83 引用"
    space-3: "24px·70 引用"
    space-4: "16px·96 引用"
    space-5: "24px·38 引用"
    space-6: "32px·18 引用"
    space-7: "48px·2 引用"
radius:  {radius-sm: 4, radius: 8, radius-full: 999}   # 圆形一律 full，禁 50%
border:  一律 1px；内部分隔 border-light，外框 border-strong；无 2px+
shadow:  仅 shadow-1（深色 0 2px 8px rgba(0,0,0,.4)）只用于 hover/浮层；静态卡片靠边框分层
motion:  {duration: 240ms, ease: cubic-bezier(.4,0,.2,1)}
layout:
  container-max: 1440   # ⚠ tokens.css 现值 1920px·待按同 PR 铁律对齐（列入基线清单）
  container-pad: 24
  form-narrow: 720
  topbar-height: 56
focus-ring: "outline 2px var(--brand-primary) offset 2"   # v1.1 新增·消除 input focus 自相矛盾
```

规则：
1. **封闭 scale（never invent a value）**：色值/字号/间距/圆角只从上表取；表内没有的值禁止出现。AI 不得发明 token 外的任何色值、字号、间距。
2. 品牌红只出现在品牌位与危险语义，全站 ≤2 处/页。
3. 语义信息必须「文字＋色」双通道传达；禁仅色点、仅变色。
4. **新代码间距只用 `--os-space-*`**；旧 `--space-*` 存量不动、入迁移清单逐步替换。
5. **input focus ring 引用 `focus-ring` token**（= outline 2px var(--brand-primary) offset 2），不再硬编码「3px 品牌色 25%」。

# 1.5 Brand（品牌最低配置·v1.1 新增）

> 覆盖复核 P1/P5：登录页品牌视觉缺失、状态条残留、页脚重复。

- **登录页背景视觉层**：必须有品牌调性的背景处理（渐变/纹理/插图），禁纯白/纯灰裸底。
- **正式 Logo 区块**：登录页顶部居中或左上角放置正式 logo（SVG/PNG），禁文字占位符。
- **信任位**：登录表单下方或侧栏提供信任标识（安全认证/合规徽标/版本号），增强可信度。
- **页脚去重**：登录页页脚与主站页脚内容一致，禁重复版权/链接；若无独立页脚需求则省略。
- **状态条移除**：全局顶部状态条（如维护通知）不作为常驻元素；登出操作并入侧栏底部，不在顶栏占位。

# 1.6 Content（界面文案规范·v1.1 新增）

> 覆盖复核 P3/P5：黑话文案、统计卡问号、开发者备忘录上界面。

- **禁实现黑话上页面**：文件名、ADR 编号、迭代编号、环境变量名、内部 API 路径不得出现在用户可见文案中。
- **统计卡标签禁带问号**：所有指标名称必须是用户可理解的业务术语；不确定含义时加 tooltip 解释而非「?」。
- **状态词必须带一句解释**：如「降级」→「降级：当前流量超限，已自动切换备用通道」；禁孤立状态词。
- **开发者备忘录归代码注释**：TODO/FIXME/HACK/技术债备注只在源码注释中，不上界面。
- **APPKEY 等凭证展示须配引导**：显示密钥/token 时必须附带「如何使用」链接或折叠说明，禁裸值陈列。

# 2. Components（机器可读）

```yaml
button:
  primary:   {底: brand-primary, 字: brand-on 600}
  secondary: {底: bg-subtle, 边: border-strong, 字: text-strong}
  danger:    {透明底, 边/字: status-error}
  size: {md: 高36内距"4 16", sm: 高28, 向导: 高40 min-width 120}
  state:
    hover: primary 提亮 1.1；secondary 边转 brand-primary；danger 底 status-error 10%
    disabled: opacity .38＋cursor not-allowed（禁改灰字色）
    focus: 引用 §1 focus-ring token（全组件统一）
  rules: [每屏主按钮≤1, 危险不为主动作, 危险操作两段式确认]
input:
  base: {底: bg-input, 边: border-strong, 高: 36, 圆角: radius, 字: fs-body text-strong}
  focus: 引用 §1 focus-ring token（消除 v1 「3px 品牌色 25%」自相矛盾）
  error: 边 status-error；帮助文字转 status-error（fs-caption，框下 4px）
  label: 框上 8px fs-body 600；帮助文字框下 4px fs-caption text-muted
table:
  head: fs-caption text-muted 600 左对齐；底边 border-strong；上下内距 8
  row: 高≥40 fs-body；行线 border-light；上下内距 12；hover 底 bg-subtle（无边框变化无位移）
  empty: 居中＝图标24 text-muted＋一句 fs-body text-muted＋可选次按钮；上下 space-6
  loading: 骨架屏（底 bg-subtle）；禁转圈图标占满表格
card:
  base: {底: bg-card, 边: 1px border-strong, 圆角: radius, 内距: os-space-5, 阴影: 无}
  title: fs-title text-strong 600＋右侧操作区（次按钮/链接）
  分区: 卡内用 border-light 或 os-space-4；卡间距 os-space-5
badge:   # 胶囊型唯一形态；描边型（badge-offline 等）废弃
  base: {圆角: radius-full, 字: fs-caption, 内距: "2 8", 底: 语义色 12%, 字色: 语义色}
  map: {在线/已发布: ok＋圆点6, 下线/停用: text-muted 无圆点, 敏感: warn＋盾图标, 进行中: info＋圆点, 失败: error＋圆点}
toast:
  base: 右上浮层；底 bg-card 边 border-strong 圆角 radius 内距"12 16"；左 3px 语义竖条（四态）
  text: 标题 fs-body 600；正文 fs-body text-muted；出现/消失 240ms 滑入出
  rules: 自动消失 4s（error 8s 或手动关）；同屏≤3 条挤掉最旧
```

# 3. Patterns

## 3.1 布局与滚动【D1·应用式布局·已批】
- 结构 `.os-layout > .os-sidebar + .os-main`（实测 .os-layout 39 次·v1 误写 os-shell 已更正）：两者各自 `overflow-y:auto` 独立滚动；`body` 锁视口（100dvh＋overflow:hidden）；`page-foot` 并入 os-main 尾部。依据：管理后台业界标准＋走查实证侧栏悬空。
- os-main 内：`os-container` 居中 max-width 1440、左右 pad 24。页头 page-head＝H1（fs-heading 600）＋hint（fs-body text-muted）＋page-actions（右对齐，主按钮≤1）；题→hint 4px、hint→actions 12px、页头→首区块 os-space-6、区块间 os-space-5；page-foot fs-caption text-muted 距末区块 32。
- 窄表单页（申请/向导）：内容列 720 居中、单列；禁同一步并排两列表单。

## 3.2 响应式【D2·汉堡＋抽屉·已批】
- ≤992px 侧栏收抽屉：汉堡按钮唤出，遮罩点击或 ESC 关闭，抽屉内焦点陷阱。
- 禁止窄屏全菜单平铺。内容断点沿用 1024/720；<720 时 Stepper 退化为「2/4 步骤名」文字式。

## 3.3 表单与校验
- 标签上置；错误就地显示（§2 input.error）；提交中按钮 disabled 且文字不变。
- **表单行对齐（结构对齐铁律，2026-10-16 维护者 批，依据 docs/reviews/表单对齐规范缺口分析_20261016.md）**：同一表单行内的输入框与操作按钮必须结构同容器、等高对齐（flex `align-items:stretch` 同行，或 grid 同行单元）；按钮的垂直基准是 input 本体，**不是** label 顶线或 field-hint 底线。禁用 margin-top/bottom 魔法数对齐相邻元素（含 px 值与 calc 补偿）；hint 有无、label 换行均不得引起按钮位移。带 label 上置的多列过滤行用 `align-items:flex-end` 时，行内不放无 label 的裸按钮列；按钮列须包一层与字段同构的容器（或该行改 stretch）。

## 3.4 数据展示
- KPI 大数字 fs-display；表格/空态/加载一律按 §2 table。

## 3.5 反馈
- 操作反馈走 toast（§2）；页面级错误用空态卡；禁 alert/confirm。

## 3.6 发布台向导（四步）
- ① 选数据：业务域→数据表两级级联；选项行高≥40；选中项边框 brand-primary。
- ② 自动验证：进入即跑、不可编辑；结果按 §2 table 逐项徽标；有 error→下一步 disabled＋一句 status-error 汇总；全过自动高亮下一步。
- ③ 加载上线：进度条高 8 圆角 full、轨道 bg-subtle、填充 brand-primary；百分比 fs-display；完成一行摘要＋ok 徽标。
- ④ 完成页：居中、上下 os-space-7；ok 图标 48→服务名 fs-display→一句话结果（服务地址 mono 可复制）→主按钮「完成」＋次按钮「查看服务」。
- Stepper（页头下 24px）：圆 28、连接线 2 border-strong；已完成＝底 ok 字 bg-card＋✓；当前＝底 brand-primary 字 brand-on＋名 600＋外圈引用 focus-ring；未到＝透明底边 border-strong 字 text-muted；步骤名 fs-caption。
- 操作条：每步底部、隔 os-space-5；「上一步」除首步永可见；「下一步」高 40 min-width 120。

# 4. Accessibility（机器可读）

```yaml
contrast:            # 门禁现场计算 §1 全部前景×背景组合；文档禁抄对比度数字
  text: ">=4.5:1"    # WCAG 1.4.3
  large: ">=3:1"     # >=24px 或 >=19px·600
  non_text: ">=3:1"  # 边框/图标 WCAG 1.4.11
keyboard:
  focus: 引用 §1 focus-ring token；禁 outline:none 无替代
  reach: 全部交互件可 Tab 到达；抽屉/模态开→焦点困于内，ESC 关闭并复原
cjk:                 # 【D3·已批】
  default: "word-break: keep-all; overflow-wrap: anywhere"
  break_all: 仅白名单场景（URL/endpoint/trace-id/哈希/长机器串）；白名单文件登记制、另立
  nowrap: 禁对中文容器 white-space:nowrap（单行英文按钮/徽标除外）
motion:
  reduced: prefers-reduced-motion:reduce → 动效全关，仅留透明度切换
```

# 5. AI Agent Contract

- **可执行入口**：页面/样式任务**必跑 `node scripts/design_gate.mjs` 并在回报贴退出码**。退出码非零＝未合规，须修到绿。
- Must-do：① 动手前读 §1/§2；组件先用 §2 原语，缺则新建并同 PR 回填本文件。② 样式只写 var(--token)。③ 完成后跑 design_gate，失败修到绿，禁绕过。
- Never-do（✅/❌ 配对）：
  - ❌ `color:#E85B6B` → ✅ `color:var(--brand-primary)`
  - ❌ `font-size:14px` → ✅ `var(--fs-title)`
  - ❌ `padding:10px` → ✅ `var(--os-space-3)`
  - ❌ `class="text-primary bg-dark"` → ✅ 语义 token 自有类
  - ❌ `var(--x,#8c8c8c)` → ✅ `var(--x)`（禁浅色 fallback）
  - ❌ 中文容器 nowrap → ✅ keep-all＋anywhere
  - ❌ `border-radius:50%` → ✅ `var(--radius-full)`
  - ❌ 新建 `--admin-*` 平行变量 → ✅ tokens.css 单源
- Token 速查（高频错字面量→token·精简 3 项）：
  - 色值：#E85B6B/#C8102E→--brand-primary｜#12151c→--bg-page｜#e8ebf1→--text-strong
  - 间距：新代码用 --os-space-1..7（4/8/12/16/24/32/48）
  - 圆角：4px→--radius-sm｜8px→--radius｜999px→--radius-full
- Scope 与例外：vendor/** 与存量未迁移区豁免（存量清单另立、只增不减）；一切例外必须登记，未登记即违规。

# 6. Enforcement（门禁·可执行）

| # | 件 | 工具 | 绑定 |
|---|---|---|---|
| 1 | 静态断言 | `scripts/design_gate.mjs` | §1 全部 scale＋§5 never-do；10 项可判定断言 |
| 2 | Lint | stylelint（增量） | §1 裸 hex / 自定义属性；仅查 diff 新增/修改行 |
| 3 | 视觉 | BackstopJS admin 域基线 | §3 布局/向导/门户↔管理端一致性（⚠ 待装浏览器二进制） |
| 4 | 文本 | pangu | 中西文间距，全量 |

- **design_gate 注册**：已注册进 `scripts/run_gates.sh`（容错：node/npm 缺失时报「design_gate 未执行」并置 GATES_FAILED=1，不静默绿）。
- **10 项可判定断言**（design_gate.mjs 逐条实现）：
  1. A1·裸 hex（CSS·tokens.css 外纯裸 hex＝0）[脚本]
  2. A2·font-size ⊆ 合法集（含存量正当值 9/10/11/14/15/18/24/26/30/36px）[脚本]
  3. A3·margin/padding/gap px ⊆ 合法集（含存量正当值 1/2/3/6/7/10/15/18/22px）[脚本]
  4. A4·border-radius ⊆ {0,2,3,4,6,8,10,14,999px,var(--radius-*)} [脚本]
  5. A5·HTML 内联 style 裸 hex＝0 [脚本]
  6. A6·每页 .btn-primary ≤1 [脚本]
  7. A7·每页品牌红引用 ≤2 [脚本]
  8. A8·outline:none 无替代焦点样式＝0 [脚本]
  9. A9·border-radius:50%＝0（→var(--radius-full)）[脚本]
  10. A10·新定义 --admin-*＝0 [脚本]
- **存量基线**：首跑报告见 `docs/reviews/设计门禁基线_20261015.md`；后续「全量清账批」以此为输入。
- CI 阻断：新增行命中断言 1–10 任一 → PR 阻断。
- 变更联动：token **增删或改值** → 同 PR 改 tokens.css＋本文件＋门禁映射＋BackstopJS 基线重摄（同 PR 铁律）。
- ~~止损"被 --no-verify 绕过 ≥3 次砍规则"~~：**已删除**（v1.1·复核意见：坏激励，混淆误报与蓄意绕过。高误报→修规则，蓄意绕过→升级审批，另案处理门禁钩子）。

# 附录（不进正文条款）
- 上游：docs/research/DESIGN_md骨架调研_20261015.md（骨架与 AI 铁律）；docs/plans/页面视觉标准_v1.md（token 表/组件规范/七断言来源）。
- 验证案例：docs/reviews/协作走查_20261015.md（D1 侧栏悬空实证）。
- 对抗复核：docs/reviews/DESIGN_md_v1对抗复核_deepseek-flash_20261015.md（v1→v1.1 修订依据）。
- 存量基线：docs/reviews/设计门禁基线_20261015.md（design_gate 首跑结果）。
- 另立待建：存量迁移清单、break-all 白名单。
