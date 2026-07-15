# FinSight 金融行情大屏

> 全品类金融行情监控 + 异动报警 + 盘中技术分析 + 筹码分布的**个人盯盘工作站**。
> 科幻深色 UI + 3D 地球 + 本地数据持久化，主后端纯 Python 标准库零依赖。
>
> ⚠️ 数据仅供参考，不构成投资建议。

---

## ✨ 能力全景

FinSight 覆盖「**看 → 盯 → 问**」完整闭环：

| 维度 | 能力 | 说明 |
|------|------|------|
| 👀 **看** | 全品类实时行情 | A股/港股/美股/ETF/外汇/加密，6 分类卡片，涨红跌绿，15s 自动轮询 |
| 🚨 **盯** | 异动监控报警 | 双窗算法（长窗 180min≥5% / 短窗 5min≥1% + 10min 冷却），大屏顶部滚动提示 |
| 🔍 **问** | 盘中技术分析 | MA/MACD/KDJ/RSI/量能 + 多空分歧判断 + 大盘环境 + 自然语言解读 |
| 🎰 **问** | 筹码分布分析 | 获利盘 / 平均成本 / 90-70 成本区 / 集中度 + 成本区间带状图 |

---

## 🚀 快速开始

### 主服务（零依赖，开箱即用）

```bash
python3 scripts/serve.py          # 启动服务（默认端口 8770）
python3 scripts/serve.py --test   # 抓一批自选报价做连通性测试
```

访问 **http://127.0.0.1:8770/**

自定义端口：`FINANCE_PORT=9000 python3 scripts/serve.py`

### 筹码功能（可选，需独立环境）

筹码分布依赖 akshare（重依赖），**完全隔离**在 `.cyqenv` 独立虚拟环境，绝不污染主服务：

```bash
bash scripts/setup_cyqenv.sh      # 一键建 .cyqenv + 装依赖 + 跑通验证
```

不装也不影响其他功能——筹码 Tab 会友好提示"环境未就绪"。

---

## 🏗️ 架构

```
              ┌─────────────────────────────────┐
              │     FinSight 大屏 (前端)          │
              │  行情卡片 │ 3D地球 │ K线弹窗       │
              │  + 异动提示条 + 分析/筹码面板      │
              └───────────────┬─────────────────┘
                              │ HTTP API
              ┌───────────────┴─────────────────┐
              │      serve.py (主后端 · 纯标准库)  │
              │  /quotes /kline /watchlist        │
              │  /alerts (异动) /analyze (分析)    │
              │  /cyq (筹码, subprocess 转发)      │
              └──┬──────────┬─────────┬──────────┘
                 │          │         │
          后台盯盘线程   指标计算   subprocess 调
          (双窗+冷却)  (纯Python)  .cyqenv (akshare)
                 │                    │
             SQLite 持久化       独立虚拟环境(隔离)
```

**核心设计**：主后端全程纯标准库零依赖，唯一重依赖 akshare 完全隔离在 `.cyqenv` 子进程。

---

## 📡 API

| 接口 | 说明 |
|------|------|
| `GET /api/finance/quotes` | 自选股实时报价（读 DB 秒回，后台线程刷新）|
| `GET /api/finance/kline?symbol=600519.SS&period=daily` | 历史 K 线（daily/weekly/monthly）|
| `GET /api/finance/watchlist` | 自选股清单 |
| `GET /api/finance/alerts?since=&limit=` | 异动报警列表 |
| `GET /api/finance/analyze?symbol=` | 技术面分析（指标+多空+大盘+自然语言解读）|
| `GET /api/finance/cyq?symbol=` | 筹码分布（获利盘/成本区/集中度，需 .cyqenv）|

---

## 🗄️ 数据源与容灾

| 品类 | 源 | 延迟 |
|------|-----|------|
| A股 | 新浪财经（主）+ 东方财富 + 腾讯（备）| ~实时 |
| 港股/美股/ETF/外汇/加密 | Yahoo Finance | 实时~15分钟 |
| 大盘指数 | 新浪轻量接口 | ~实时 |
| 板块热点 | 东方财富 clist | ~实时 |
| 筹码分布 | 东方财富 `stock_cyq_em`（akshare，唯一源）| T+1 盘后 |

**容灾策略**：多源自动切换 + 抓取失败用本地 DB 旧值兜底 + Yahoo 429 退避重试 + 筹码 5 次重试 + 60s 缓存 + 优雅降级。

---

## 📊 技术指标（纯 Python 实现，无第三方库）

- **均线** MA5/10/20/60/120
- **MACD** DIF/DEA/柱体 + 金叉死叉
- **KDJ** K/D/J + 金叉死叉/超买超卖
- **RSI14** Wilder 平滑
- **量能** 成交额 + 量比 5日/20日
- **多空分歧** 10 项判据（涨跌幅/K线影线/均线位置/MACD/KDJ/RSI/量能/突破跌破/大盘/板块）→ stance + 强度 + 分歧度 + 次日验证信号

---

## 🗃️ 存储结构（SQLite）

- `quote_latest` — 各标的最新报价
- `quotes` — 报价历史快照
- `kline` — 历史 K 线（日/周/月）
- `watchlist` — 自选股清单（默认 13 只，6 大品类）
- `alerts` — 异动报警记录
- `monitor_config` — 异动监控阈值配置
- `meta` — 更新时间元信息

---

## ✅ 开发进度

| 里程碑 | 内容 | 状态 |
|--------|------|------|
| Day1 后端 | serve.py + SQLite + 多源容灾 + 5 API | ✅ |
| Day2 前端 | 科幻大屏 S1–S9（行情/3D地球/K线/动画）| ✅ |
| **A 异动监控** | 双窗算法 + 盯盘线程 + 提示条 | ✅ |
| **B 盘中分析** | 指标引擎/大盘板块/多空判断/analyze/解读 | ✅ |
| **C 筹码分析** | 隔离环境/转发缓存/筹码 Tab | ✅ |
| A5 推送 | 飞书/TG 异动推送 | 📋 可选 |
| B5-B 真 AI | 接 LLM 生成自然语言解读（现为模板版）| 📋 可选 |

详见 [`PROGRESS.md`](PROGRESS.md) 与 [`docs/EXPANSION_PLAN.md`](docs/EXPANSION_PLAN.md)。

---

## 📁 目录结构

```
finance-dashboard/
├── scripts/
│   ├── serve.py              # 主后端（纯标准库，1258 行）
│   ├── cyq_report.py         # 筹码分析脚本（在 .cyqenv 中运行）
│   ├── cyq-requirements.txt  # 筹码环境依赖清单
│   └── setup_cyqenv.sh       # 一键搭建 .cyqenv
├── public/
│   ├── finance.html          # 大屏前端（单文件）
│   └── cobe.js               # 3D 地球库
├── data/                     # SQLite 数据库（gitignore）
├── docs/EXPANSION_PLAN.md    # 扩展整合方案
├── PROGRESS.md               # 任务进度拆解
└── README.md
```
