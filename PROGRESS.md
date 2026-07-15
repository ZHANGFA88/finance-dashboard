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
- [x] **S8** 本地实测（curl 全端点 + playwright 浏览器验收）
  - 5个API全通：quotes(13只)/kline(250根)/watchlist(13)/alerts(2条)/analyze(偏多)，playwright截图无控制台错误
- [x] **S9** git 提交 Day2

## Day 3 📋 里程碑A：异动监控报警（拆封中）

> 来源：Mstock 双窗算法 + 冷却机制。只搬算法，跳过登录/CSRF/多用户安全模块。
> 原理：每只股维护(时间,价格)历史队列 → 找N分钟前基准价 → 算涨跌速度 → 超阈值且过冷却期就报警。

- [x] **A1** 建表 + 双窗异动算法（长窗180min≥5% / 短窗5min≥1% + 冷却10min）
  - alerts表(id,symbol,name,window,pct,direction,price,ts)
  - monitor_config表(阈值/窗口/冷却，可改)
  - AnomalyDetector类：history队列 + price_at_or_before + should_alert(带冷却)
  - 验收：喂模拟数据能正确判定异动+冷却
- [x] **A2** 后台盯盘线程：轮询自选股→喂检测器→触发写alerts表
  - 复用现有后台刷新线程的行情数据
  - A股交易时段判断（9:30-11:30/13:00-15:00，非交易时段不误报）
  - 验收：日志能看到"XX触发异动"
- [x] **A3** 加腾讯行情源(stock.gtimg.cn)进多源容灾
  - 验收：Yahoo挂时腾讯能顶上
- [x] **A4** /api/finance/alerts + 大屏顶部异动提示条(滚动/闪烁)
  - 验收：触发时大屏能看到提示
- [ ] **A5** (可选)飞书/TG推送

## Day 3 里程碑B 📋 盘中技术分析（拆封中）

> 来源：a-share-intraday 技能方法论。核心：抓K线算指标 → 判多空分歧 → 配大盘/板块环境 → 出结构化解读。
> 复用已有：K线接口(东财/腾讯)、行情接口。指标全用纯Python算，不加重依赖。

- [x] **B1** 技术指标计算引擎（纯Python，无依赖）
  - MA5/10/20/60/120 均线
  - MACD（DIF/DEA/柱体 + 较前日变化）
  - KDJ（K/D/J + 金叉死叉/钝化）
  - RSI14
  - 量能：成交额、换手率、当日量能相对5日/20日均量
  - 近20/60日高低点
  - 验收：对比东财真实指标值，误差<1%
- [x] **B2** 大盘环境 + 板块热点数据源
  - 大盘指数：上证/深成/创业板/科创50（新浪 s_sh000001 等）当日涨跌+均线位置
  - 板块热点：东财 clist/get（概念 fs=m:90+t:3 / 行业 t:2）涨幅榜+资金流
  - 验收：API能返回四大指数 + 板块涨幅前列
- [x] **B3** 多空分歧判断框架（judge_bull_bear）
  - 输入 B1 指标 + 原始K线(算影线/位置) → 输出：stance/strength/bull_score/bear_score/divergence/因子清单/次日验证信号
  - 10项判据：涨跌幅、K线实体与上下影线、均线位置、MACD、KDJ、RSI、量能比、突破/跌破近20日高低、大盘环境、板块强弱
  - 验收：喂模拟上涨股输出"多头主导/强"，因子分明，次日验证价位合理 ✅
- [x] **B4** /api/finance/analyze?symbol= + K线弹窗"深度分析"面板
  - 后端聚合 B1+B2+B3 → 返回结构化分析JSON（analyze端点 + _index_cache_get 30s缓存）
  - 前端 K线弹窗加 Tab（K线/深度分析），面板展示：结论(stance/强度/分歧)、多空双栏因子、技术面指标网格、大盘环境、次日验证信号
  - 验收：playwright 实测点茅台→切分析，真实数据渲染「偏多/中」、多空因子分明、零控制台错误 ✅
  - 前端 K线弹窗加"分析"标签页，展示技术面/大盘/板块/多空/关键价位
  - 验收：点个股能看到完整技术面解读
- [x] **B5** 自然语言解读（方案A：模板化，零依赖，后续可叠加真AI）generate_narrative()拼流畅中文点评 + 前端「📝解读」卡片，playwright验收通过 ✅
  - 把结构化数据喂 AI → 一段话讲清这只股

## Day 3 后续 📋 待规划（C 筹码分析）
> 见 docs/EXPANSION_PLAN.md。akshare依赖重，独立环境隔离。

## 数据格式备忘
quotes item: `{symbol,name,market,price,change_pct,change_amt,open,high,low,prev_close,volume,ts,stale?}`
market枚举: cn=A股 hk=港股 us=美股 etf=ETF crypto=加密 fx=外汇
服务: `python3 scripts/serve.py`（端口8770），根路径 `/` → public/finance.html
