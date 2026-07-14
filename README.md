# FinSight 金融大屏

全品类金融行情监控大屏（A股 / 港股 / 美股 / ETF / 外汇 / 加密货币），科幻 UI + 3D 地球 + 本地数据持久化 + AI 盘面辅助。

> 数据仅供参考，不构成投资建议。

## 特性

- **全品类行情**：A股（新浪/东财，~实时）、港美股/ETF/外汇/加密（Yahoo Finance）
- **多源容灾**：A股新浪主源 + 东财备源；抓取失败自动用本地 DB 旧值兜底
- **本地持久化**：SQLite 存储实时报价快照 + 历史 K 线，边抓边存，可离线查看/分析
- **后台刷新**：独立线程定时抓取，API 秒回（读 DB），不阻塞前端
- **3D 地球**：全球市场可视化（规划中）
- **AI 盘面辅助**：接入 AI 做盘面速读 / 异动分析 / 个股解读（规划中）
- **零第三方依赖**：纯 Python 标准库 + SQLite

## 快速开始

```bash
python3 scripts/serve.py          # 启动服务(默认端口8770)
python3 scripts/serve.py --test   # 抓一批自选报价测试
```

访问 http://127.0.0.1:8770/

## API

| 接口 | 说明 |
|------|------|
| `GET /api/finance/quotes` | 自选股实时报价（读DB秒回，后台刷新）|
| `GET /api/finance/kline?symbol=AAPL&period=daily` | 历史K线（daily/weekly/monthly）|
| `GET /api/finance/watchlist` | 自选股清单 |

## 数据源

| 品类 | 源 | 延迟 |
|------|-----|------|
| A股 | 新浪财经（主）+ 东方财富（备）| ~实时 |
| 港股/美股/ETF/外汇/加密 | Yahoo Finance | 实时~15分钟 |

> Yahoo 有 429 限流，已内置退避重试 + DB 兜底。

## 存储结构（SQLite）

- `quote_latest` — 各标的最新报价
- `quotes` — 报价历史快照
- `kline` — 历史K线（日/周/月）
- `watchlist` — 自选股清单
- `meta` — 更新时间元信息

## 开发进度

- [x] Day1: 数据后端 + SQLite 持久化 + 多源容灾
- [ ] Day2: 大屏三栏布局 + 行情榜 + 跑马灯
- [ ] Day3: 3D 地球金融化
- [ ] Day4: 详情面板 + K线图 + 异动流
- [ ] Day5: AI 盘面辅助 + 联调
