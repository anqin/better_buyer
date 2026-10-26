# Better Buyer

品类可插拔、多模型协同决策的**优惠券采集 + 盯盘预测 + 半自动购买**系统。
云端（WSL2 + Ubuntu 24.04）跑全套能力，手机端（Android 13+ Chrome）只承载交互与登录授权。

- 主交互：自然语言说需求 → 自动补齐槽位 → 询问是否启动后台盯盘 → 命中买点主动推送 → 选券/选平台 → 深链下单 → 回执与出发提醒
- 品类：机票、酒店、茅台，另含可复制的 `custom_example`
- 数据源：Mock（默认离线可用）/ Playwright / 官方 API / 聚合 API
- 价格预测：EWMA + 趋势 + 季节因子 + 多模型中位数 + 加权融合 + 硬约束护栏
- 存储：SQLite（WAL）为唯一真源，原子导出 Markdown 台账

> 设计文档：`docs/design.md` · 需求文档：`docs/requirements.md`

---

## 1. 快速开始（5 分钟，离线 Mock）

### 1.1 准备环境（WSL2 + Ubuntu 24.04）

```bash
# 在 Windows 中进入 WSL
wsl
cd /mnt/c/Users/qin_a/workspace/github/better_buyer

# 安装 uv（已安装可跳过）
curl -LsSf https://astral.sh/uv/install.sh | sh
source ~/.local/bin/env
uv --version
```

### 1.2 安装依赖并跑通离线全链路

```bash
uv sync                    # 创建/更新 .venv，安装 pyyaml、fastapi、uvicorn、cryptography、pytest
uv run better-buyer init   # 初始化 data/ memory/ vault/ notifications/ 与 Markdown 导出
uv run better-buyer demo   # Mock 全链路：采集 → 预测 → 选券 → 报价 → 订单深链
```

`demo` 会输出预约、采集条数、决策（`buy_now/watch/no_buy/no_go`）、前 3 个方案与下单深链。
Mock 数据是确定性的，同一天多次运行结果一致，便于对照测试。

### 1.3 自然语言主界面（CLI）

```bash
uv run better-buyer chat
```

依次输入（`#` 后为说明，不需要输入）：

```text
11月8号北京飞上海，预算2000
  # 条件齐全，助手询问是否启动后台盯盘（进入 Ready）
1
  # 启动后台盯盘（Ready → Watching）
今天天气不错啊
  # 闲聊不会打断流程，状态保持 Watching
3
  # 查看各平台优惠券
2
  # 继续观望
4
  # 停止盯盘（回到 Ready）
1
  # 重新启动盯盘
1
  # 现在购买 → 进入「选择平台（实付价从低到高）」
1
  # 选择携程
my_account@example.com
  # 首次使用该平台，先输入登录账号（回复“取消”可退出）
my_password
  # 再输入密码/令牌（回复“跳过”只保存脱敏别名）
已支付 订单号 1234567890
谢谢
  # 购买完成后仍可继续闲聊，直到输入「退出」
退出
```

盯盘命中买点时会在主界面插入一行摘要，并重绘决策菜单：

```text
[盯盘] 当前最低 ¥1180.00（携程）｜预测 ¥1120.00｜最优窗口 2026-10-27｜建议观望

=== 盯盘决策 ===
[1] 现在购买  [2] 继续观望  [3] 查看各平台优惠券  [4] 停止盯盘
```

输出在交互终端下带颜色（助手为绿色、盯盘为紫色、状态行为灰色），重定向到文件或管道时自动关闭；也可设置 `NO_COLOR=1` 强制纯文本。

### 1.4 云端服务 + 手机端 PWA

```bash
# 终端 A：启动 API/PWA（默认 0.0.0.0:8000，带后台调度器）
uv run better-buyer serve

# 终端 B：生成手机配对码（写入 SQLite，serve 进程可直接读取）
uv run better-buyer pair --base-url http://192.168.1.10:8000
```

手机步骤（Android 13+ Chrome）：

1. 手机与云端在同一网络，Chrome 打开 `http://192.168.1.10:8000`
2. 点右上角 ⚙ → 「开启通知」授权系统通知
3. 在「配对码」输入终端 B 输出的 6 位码 → 「配对我的云端账号」
4. 回到主界面直接说需求；后台命中买点会通过 SSE 推送决策卡与系统通知
5. 可在 Chrome 菜单中「添加到主屏幕」，以 PWA 独立窗口运行

> 手机端只保存访问/刷新令牌、设备 ID、会话 ID 与 SSE 游标（localStorage）。
> 采集、预测、下单深链、凭据保险箱全部在云端完成。

### 1.5 生产环境（干净数据，不含演示数据）

`init` 只创建目录、同步品类、初始化空库并导出空台账，**不会写入任何演示数据**；演示数据只来自 `demo` 命令和本地冒烟测试。生产启动请隔离数据目录。

方式 A：同仓库、数据目录隔离（推荐）

```bash
export BB_DATA_DIR_NAME=/srv/bb-prod/data      # 空目录 → 全新 SQLite
export BB_MEMORY_DIR_NAME=/srv/bb-prod/memory  # 空目录 → 全新台账
export BB_PLATFORM_MODE=playwright             # 真实采集（不要用 --mock）
export BB_JWT_SECRET="$(openssl rand -hex 32)"
export BB_MASTER_KEY="$(openssl rand -hex 32)"

uv sync --extra browser
uv run playwright install chromium
uv run better-buyer init
uv run better-buyer health                     # 确认适配器 source 不是 mock
uv run better-buyer serve --host 0.0.0.0 --port 8000
```

方式 B：独立部署目录（代码与数据彻底隔离）

```bash
mkdir -p /srv/better-buyer
rsync -a --exclude .venv --exclude data --exclude memory \
      --exclude notifications --exclude vault ./ /srv/better-buyer/
cd /srv/better-buyer
# 之后同方式 A 的安装、init、serve 步骤
```

若复用当前目录，先把开发期数据移走再初始化：

```bash
mv data data.demo.bak
mv memory memory.demo.bak
mv notifications notifications.demo.bak
uv run better-buyer init
```

生产环境**不要**执行 `better-buyer demo`，也不要加 `--mock`；两者都会写入演示数据并使用假价格。

---

## 2. 架构

```
┌──────────────────────── 手机端（PWA）────────────────────────┐
│ index.html / styles.css / app.js / sw.js                     │
│ 对话渲染 · 决策卡 · 方案选择 · SSE 增量事件 · Notification     │
└───────────────▲──────────────────────────▲───────────────────┘
                │ HTTPS/JSON               │ SSE(/api/events)
┌───────────────┴──────────────────────────┴───────────────────┐
│ FastAPI（api/）                                              │
│ auth · chat · trading · system · sse                          │
├──────────────────────────────────────────────────────────────┤
│ MainAgent 状态机编排                                          │
│  Idle→Collecting→Ready→Watching→Deciding→SelectingOffer       │
│        →Purchasing→Completed / Exited                         │
├──────────────┬───────────────┬───────────────┬───────────────┤
│ Interaction  │ Collection    │ Predictor     │ Purchase      │
│ 槽位抽取/追问 │ 平台采集+券   │ 预测+多模型   │ 选券/订单/深链 │
├──────────────┴───────────────┴───────────────┴───────────────┤
│ WatchAgent（后台盯盘）· DataChecker（质检）· Notification      │
│ Scheduler（到期通知/盯盘/采券/质检/校准）· EventBus           │
├──────────────────────────────────────────────────────────────┤
│ services: ranking · prediction · scoring · model_router       │
│           notification · memory · calibration · state_machine │
├──────────────────────────────────────────────────────────────┤
│ categories（flight/hotel/moutai/custom_example）· adapters    │
│ （mock/playwright/official_api/aggregator）                   │
├──────────────────────────────────────────────────────────────┤
│ storage: SQLite(WAL) + repositories + 原子 Markdown 导出       │
└──────────────────────────────────────────────────────────────┘
```

### 目录结构

```
.
├── pyproject.toml              # 依赖、脚本入口、pytest 配置
├── README.md
├── config/
│   ├── app.yaml                # 品类、阈值、调度、城市、导出
│   ├── platforms.yaml          # 平台与采集模式（playwright|mock|adapter）
│   ├── llm.yaml                # 多 provider / 角色 / 权重 / 预算 / 熔断
│   ├── web.yaml                # 服务端口、令牌 TTL、SSE keepalive
│   └── categories/*.yaml       # 每个品类的参数覆盖
├── src/better_buyer/
│   ├── cli.py                  # 命令行客户端（默认入口 chat）
│   ├── models.py               # 领域模型与时间/ID 工具
│   ├── api/                    # FastAPI 路由、SSE、PWA 宿主
│   ├── agents/                 # main/interaction/collector/predictor/purchase/watcher/checker
│   ├── services/               # 排序、预测、打分、模型路由、通知、记忆、校准、状态机
│   ├── categories/             # 品类策略 ABC 与实现、注册表
│   ├── adapters/               # mock / playwright / official_api / aggregator
│   ├── core/                   # 配置、安全、事件总线、调度器、容器装配
│   └── storage/                # SQLite schema、仓储、Markdown 导出
├── web/                        # PWA（index.html/styles.css/app.js/sw.js/manifest/icon）
├── tests/                      # 单元与端到端 Mock 测试
├── data/                       # SQLite 数据库与备份
├── memory/                     # user_memory.md、platform_accounts.md（脱敏）
├── vault/                      # 凭据密文（仅 DB 内）与浏览器 profile
└── notifications/              # 通知 JSON / .ics
```

---

## 3. 核心流程

### 3.1 从一句话到下单

1. **启动即主界面**：`chat` / PWA 打开后先寒暄并询问需求（支持闲聊，不会误触发下单）
2. **实时意图预测**：先判断意图落点（机票/酒店/茅台），置信度 ≥ `collection.intent_threshold`（默认 `0.80`）才进入该品类收集；顶部意图置信度 > 40% 时会围绕该意图引导澄清（不再罗列无关品类）；状态栏会展示当前可能意图的 top-2 置信度（如 `意图 机票 55%、酒店 35%`）。进入收集后，补参数（城市/日期/预算等）不会触发“换个方向”的询问，已确认意图置信度锚定在高位；只有当用户闲聊其他内容、已确认意图置信度衰退到 `collection.intent_decay_threshold`（默认 `0.45`）以下且出现其它品类信号时，才会询问“是否继续上一个意图”。收集期间也可随时插入闲聊
3. **槽位抽取**：从中文里识别品类、城市、日期（`11月8号/明天/这周五`）、预算、舱位、数量
4. **缺口追问**：只问缺的字段；信息齐全后进入 `Ready`，由用户确认才采集 → 预测 → 选券 → 创建盯盘任务（`Ready → Watching`）
5. **后台盯盘**：按 `collection.price_interval_minutes` 周期采集与预测；推荐结果变化才推送，避免重复打扰
6. **交互与闲聊**：盯盘期间主交互仍然可用，可围绕本次购买闲聊，也可继续修改条件或做出决策
7. **盯盘决策**：`1` 现在购买 / `2` 继续观望 / `3` 查看各平台优惠券 / `4` 停止盯盘；闲聊时状态不变
8. **任务管理**：回复“有几个盯盘任务”列出后台任务及编号；回复“取消任务 w_xxx”或“取消盯盘 第N个”即可按编号取消指定任务
9. **确认购买**：实时刷新报价，按「标价 − 最优券」的**实付价升序**展示，默认推荐最低平台
10. **选择平台**：生成带幂等键的草稿订单与平台深链，进入 `Purchasing`
11. **凭据授权**：首次在该平台下单时分两步提示——先账号、再密码/令牌（回复“跳过”只存脱敏别名，回复“取消”退出）；也兼容一句「账号 … 密码 …」。密码走 AES-256-GCM 加密入库，Markdown 只留脱敏别名
12. **支付回执**：回复「已支付 订单号 …」→ 订单转 `paid`、预约转 `completed`、停止盯盘、追加购买记录、安排出发/入住提醒
13. **后续提醒**：到期通知写入 `notifications/*.json` 与 `.ics`，并通过 SSE/系统通知送达手机

### 3.2 决策与护栏

- 本地预测：EWMA、线性趋势、季节因子、加权中位数、置信区间
- 多模型：按角色（`nlu/predictor/judge`）选 provider，支持同 family 多 Key、限流、熔断、预算裁剪、缓存
- 聚合打分：本地 / 模型 / judge 加权，分歧超阈值才升级 judge
- 硬约束优先：超预算、数据过期、品类不匹配的券、缺货、异常价格 → 直接降级为 `no_buy` / `no_go`

---

## 4. CLI 命令

全局参数：`--root <目录>`、`--mock`（强制 Mock）、`--log-level`、`--json`

| 命令 | 说明 | 常用示例 |
|---|---|---|
| `init` | 初始化目录、同步品类、导出 Markdown | `uv run better-buyer init` |
| `chat`（默认） | 自然语言主交互 | `uv run better-buyer chat` |
| `demo` | Mock 全链路演示 | `uv run better-buyer demo` |
| `collect` | 采集价格与优惠券 | `uv run better-buyer collect --category flight --origin 北京 --destination 上海 --date 11月8日 --budget 2000` |
| `decide` | 采集+预测+方案 | `uv run better-buyer decide --category hotel --city 上海 --checkin 11月8日 --checkout 11月10日 --watch` |
| `buy` | 比价、选券、下单深链、回执 | `uv run better-buyer buy --booking-id b_xxx`；`uv run better-buyer buy --booking-id b_xxx --offer 1`；`uv run better-buyer buy --order-id o_xxx --receipt 1234567890` |
| `watch` | 执行/启动盯盘 | `uv run better-buyer watch --once`；`uv run better-buyer watch --booking-id b_xxx` |
| `pair` | 生成手机配对码 | `uv run better-buyer pair --base-url http://192.168.1.10:8000` |
| `check` | 数据质检与自动修复 | `uv run better-buyer check --json` |
| `export` | 导出 Markdown 台账 | `uv run better-buyer export` |
| `calibrate` | 回测并更新模型可靠性 | `uv run better-buyer calibrate --horizon 3` |
| `serve` | 启动 API + PWA | `uv run better-buyer serve --port 8000` |
| `status` | 运行状态，含后台盯盘任务数与内置命令数 | `uv run better-buyer status` |
| `health` | 适配器/模型健康 | `uv run better-buyer health` |

`status` 的 JSON 中可直接查看 `watch_task_count`（当前运行中的后台盯盘任务数）与 `builtin_command_count`（内置 CLI 命令数），并附带 `builtin_commands` 全量命令名。

茅台示例：

```bash
uv run better-buyer collect --category moutai --sku 飞天茅台 --quantity 2 --budget 3200
uv run better-buyer decide  --category moutai --sku 飞天茅台 --quantity 2 --budget 3200 --watch
```

---

## 5. HTTP API（PWA 使用）

鉴权：`Authorization: Bearer <access_token>`；SSE 用 `?token=<access_token>`。

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/auth/register` | 设备注册，返回 access/refresh |
| POST | `/api/auth/pairing/code` | 生成配对码（需登录，TTL 120s） |
| POST | `/api/auth/pairing/verify` | 手机用配对码换取同账号令牌 |
| POST | `/api/auth/refresh` | 刷新令牌 |
| GET | `/api/auth/me` | 当前用户 |
| POST | `/api/chat/sessions` | 新建会话（返回开场白） |
| POST | `/api/chat/sessions/{id}/messages` | 发送消息，返回决策/方案/订单 |
| GET | `/api/chat/sessions/{id}/messages` | 历史消息 |
| GET | `/api/events?token=&cursor=` | SSE：`decision` / `notification` |
| GET | `/api/bookings` | 预约列表 |
| GET | `/api/bookings/{id}` | 预约详情（决策/方案/盯盘/通知） |
| POST/DELETE | `/api/bookings/{id}/watch` | 启动/停止盯盘 |
| GET/POST | `/api/bookings/{id}/offers` | 读取/刷新方案 |
| POST | `/api/bookings/{id}/orders` | 按方案序号创建订单 |
| GET | `/api/orders/{id}` | 订单详情 |
| POST | `/api/orders/{id}/receipt` | 提交已支付订单号 |
| GET/POST | `/api/credentials` | 凭据状态/保存（脱敏返回） |
| GET | `/api/health` `/api/status` `/api/categories` | 健康与元信息 |
| POST | `/api/collect` `/api/check` `/api/export` | 手动触发任务 |
| GET | `/api/events/recent` | 最近事件（调试） |

打开 `http://localhost:8000/docs` 可查看自动生成的 OpenAPI。

---

## 6. 环境变量

| 变量 | 作用 | 默认 |
|---|---|---|
| `BB_ROOT` | 项目根目录 | 自动向上查找 `config/app.yaml` |
| `BB_PLATFORM_MODE` | `mock` / `playwright` / `official_api` | `config/platforms.yaml: mode` |
| `BB_JWT_SECRET` | 访问/刷新令牌签名密钥 | `dev-only-change-me`（生产必须改） |
| `BB_MASTER_KEY` | 凭据保险箱主密钥（AES-256-GCM） | 空（不加密，仅存别名） |
| `BB_NOTIFY_MODE` | `local`（写文件）/ `webhook` | `local` |
| `BB_NOTIFY_WEBHOOK` | Webhook 通知地址 | 空 |
| `BB_PUBLIC_BASE_URL` | PWA 对外地址 | `config/web.yaml` |
| `BB_DEVICE_ID` | CLI 本地账号设备 ID | `cli-local` |
| `BB_WATCH_INTERVAL_MINUTES` | 盯盘间隔 | `30` |
| `BB_WATCH_DECISION_COUNTDOWN_SECONDS` | 发建议后未响应的自动继续盯盘倒计时 | `15` |
| `OPENAI_API_KEY_1` / `_2` / `DEEPSEEK_API_KEY` / `OPENAI_JUDGE_API_KEY` | 模型 Key（按需） | 空 → 纯本地预测 |

推荐 `.env`（uv/uvicorn 不会自动加载，需 `export` 或使用 `direnv`）：

```bash
export BB_JWT_SECRET="$(openssl rand -hex 32)"
export BB_MASTER_KEY="$(openssl rand -hex 32)"   # 设置后新保存的凭据才会加密
```

---

## 7. 数据与记忆

| 位置 | 内容 |
|---|---|
| `data/better_buyer.db` | SQLite 唯一真源：会话、预约、快照、券、方案、订单、凭据密文、通知、决策、模型统计 |
| `memory/user_memory.md` | 偏好与购买记录表 |
| `memory/platform_accounts.md` | 平台账号**脱敏别名**、授权方式与状态 |
| `memory/platform_credentials.md` | 兼容旧文件名，内容同样脱敏 |
| `notifications/*.json` / `*.ics` | 到期提醒与日历文件 |
| `vault/` | 浏览器 profile 等本地私有数据（密文在 DB，不在此） |

`export` 会原子写出（临时文件 + `os.replace`）：

- `memory/coupons.md`：优惠券台账
- `memory/price_history.md`：价格变化
- `memory/decisions.md`：决策记录

---

## 8. 真实平台采集（可选）

默认 `config/platforms.yaml` 为 `mode: playwright`。未安装浏览器时适配器自动降级为 Mock，保证开箱可用。

```bash
uv sync --extra browser
uv run playwright install chromium
uv run better-buyer health          # 查看各平台适配器状态
uv run better-buyer collect --category flight --origin 北京 --destination 上海 --date 明天
```

说明：

- 携带 `vault/browser` profile 降低重复登录成本
- 遇到验证码按 `captcha_policy: report_and_skip` 记录并跳过，不绕过
- 平台结构变化时，仅需在 `adapters/playwright_adapter.py` 实现对应 `fetch_prices/fetch_coupons`
- 新增品类：复制 `config/categories/custom_example.yaml`，实现一个 `CategoryStrategy` 子类并在 `config/app.yaml` 注册，无需改动主流程

---

## 9. 凭据安全

- 密码/会话令牌使用 AES-256-GCM 加密后写入 `credentials` 表（`BB_MASTER_KEY` 派生密钥）
- Markdown 只写 `account_alias`（如 `my_***@example.com`）与授权状态，绝不写明文
- 未设置 `BB_MASTER_KEY` 时退化为「仅保存别名」，不落任何敏感值
- 下单始终为**半自动**：生成平台深链，用户在官方 App/H5 完成支付，系统只记录回执
- 访问令牌 15 分钟、刷新令牌 30 天；配对码 120 秒、最多 5 次尝试、一次性消费

---

## 10. 开发与测试

```bash
uv sync
uv run pytest -q            # 单元 + Mock 端到端
uv run better-buyer demo    # 冒烟
uv run better-buyer check   # 数据质检
```

测试覆盖：品类槽位与券过滤、实付排序、预测与护栏、聚合打分、模型路由退避、SQLite/仓储/Markdown 导出、状态机、事件总线、Mock 端到端、安全（令牌/配对/脱敏/保险箱）、交互抽取。

新增或修改功能时，请同步更新本 README 与 `pyproject.toml`（依赖、脚本、测试配置）。

---

## 11. 常见问题

| 现象 | 处理 |
|---|---|
| `Readme file does not exist: README.md` | 仓库根目录需保留本文件（hatchling 构建需要） |
| `Missing dependencies: fastapi` | 执行 `uv sync` |
| 手机打不开 PWA | 确认 `serve` 监听 `0.0.0.0`、防火墙放行端口、手机与云端同网段 |
| 通知不弹 | Chrome 需 HTTPS 或 `localhost` 才允许通知；跨设备访问请用反向代理加 TLS |
| 采不到真实价格 | 未装 Playwright 浏览器时会降级 Mock；先 `better-buyer health` 确认 |
| 凭据未加密 | 设置 `BB_MASTER_KEY` 后重新保存一次凭据 |
| 决策总是 `no_go` | 数据不足或价格异常，先 `better-buyer check` 与 `better-buyer collect` |

---

## 12. 免责声明

本项目用于个人比价与提醒，不绕过平台风控、验证码与实名/支付授权。请遵守各平台用户协议，最终价格与库存以平台页面为准。
