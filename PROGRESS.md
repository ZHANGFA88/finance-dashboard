# FinSight 金融大屏 · 任务进度拆解

> 规则：每完成一项立即勾选并写盘。被打断后先读本文件，从第一个未完成项继续，不重头再来。

## Day 1 ✅ 已完成（git: f1cbf80）
- [x] 后端 serve.py（429行，纯标准库 + 后台刷新线程）
- [x] SQLite 持久化（5表：watchlist/quotes/quote_latest/kline/meta）
- [x] 多源容灾：A股新浪主+东财备，港美股/ETF/外汇/加密走 Yahoo
- [x] 3个 API：/api/finance/quotes、/kline、/watchlist
- [x] 默认13只自选股（6类：cn3/hk2/us3/etf2/crypto2/fx1）
- [x] README 规划

## Day 2 🚧 前端大屏（进行中）
- [x] **S1** finance.html 骨架（科幻深色 UI + 网格 + 霓虹）
- [x] **S2** 顶部状态栏（标题 / 时钟 / 数据新鲜度 / 刷新指示）
- [x] **S3** 6分类行情面板（卡片：名称/代码/现价/涨跌幅/涨跌额，涨红跌绿）
- [x] **S4** 接 /api/finance/quotes，15秒自动轮询刷新
- [x] **S5** cobe 3D 地球（背景/侧栏装饰，window.createGlobe UMD）
- [x] **S6** 点击个股 → K线弹窗（canvas 折线，接 /kline）
- [x] **S7** 数据陈旧(stale)/涨跌 视觉标识 + 动画
- [ ] **S8** 本地实测（curl + 浏览器验收截图逻辑）
- [ ] **S9** git 提交 Day2

## Day 3 📋 待规划
- [ ] K线历史数据抓取补全（当前 kline 表为空）
- [ ] AI 盘面辅助（异动分析/个股解读）
- [ ] 自选股增删管理 UI

## 数据格式备忘
quotes item: `{symbol,name,market,price,change_pct,change_amt,open,high,low,prev_close,volume,ts,stale?}`
market枚举: cn=A股 hk=港股 us=美股 etf=ETF crypto=加密 fx=外汇
服务: `python3 scripts/serve.py`（端口8770），根路径 `/` → public/finance.html
