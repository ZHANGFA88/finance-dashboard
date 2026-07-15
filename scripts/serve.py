#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FinSight 金融大屏后端 - 纯标准库 + SQLite
多源: A股走东方财富(~3秒), 港美股/ETF/外汇/加密走 Yahoo Finance
"""
import os, json, time, sqlite3, urllib.parse, urllib.request, re, threading
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PUBLIC_DIR = os.path.join(BASE_DIR, 'public')
DATA_DIR = os.path.join(BASE_DIR, 'data')
DB_PATH = os.path.join(DATA_DIR, 'finance.db')
PORT = int(os.environ.get('FINANCE_PORT', '8770'))

os.makedirs(DATA_DIR, exist_ok=True)

UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36'

# ========== SQLite ==========
_db_lock = threading.Lock()

def db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with _db_lock, db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS quotes(
            symbol TEXT, name TEXT, market TEXT,
            price REAL, change_pct REAL, change_amt REAL,
            open REAL, high REAL, low REAL, prev_close REAL,
            volume REAL, ts INTEGER,
            PRIMARY KEY(symbol, ts)
        );
        CREATE TABLE IF NOT EXISTS quote_latest(
            symbol TEXT PRIMARY KEY, name TEXT, market TEXT,
            price REAL, change_pct REAL, change_amt REAL,
            open REAL, high REAL, low REAL, prev_close REAL,
            volume REAL, ts INTEGER
        );
        CREATE TABLE IF NOT EXISTS kline(
            symbol TEXT, period TEXT, date TEXT,
            open REAL, high REAL, low REAL, close REAL, volume REAL,
            PRIMARY KEY(symbol, period, date)
        );
        CREATE TABLE IF NOT EXISTS watchlist(
            symbol TEXT PRIMARY KEY, name TEXT, market TEXT,
            sort_order INTEGER DEFAULT 0, added_at INTEGER
        );
        CREATE TABLE IF NOT EXISTS meta(
            symbol TEXT PRIMARY KEY, last_kline_date TEXT, last_quote_ts INTEGER
        );
        CREATE TABLE IF NOT EXISTS alerts(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT, name TEXT, market TEXT,
            window TEXT, pct REAL, direction TEXT,
            price REAL, pct_today REAL, ts INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_alerts_ts ON alerts(ts DESC);
        CREATE TABLE IF NOT EXISTS monitor_config(
            k TEXT PRIMARY KEY, v TEXT
        );
        """)
        # 默认自选池(若空)
        n = c.execute("SELECT COUNT(*) FROM watchlist").fetchone()[0]
        if n == 0:
            defaults = [
                ('600519.SS','贵州茅台','cn'), ('000001.SZ','平安银行','cn'),
                ('300750.SZ','宁德时代','cn'),
                ('0700.HK','腾讯控股','hk'), ('9988.HK','阿里巴巴','hk'),
                ('AAPL','苹果','us'), ('NVDA','英伟达','us'), ('TSLA','特斯拉','us'),
                ('SPY','标普500ETF','etf'), ('QQQ','纳指100ETF','etf'),
                ('BTC-USD','比特币','crypto'), ('ETH-USD','以太坊','crypto'),
                ('USDCNY=X','美元人民币','fx'),
            ]
            for i,(s,nm,mk) in enumerate(defaults):
                c.execute("INSERT OR IGNORE INTO watchlist(symbol,name,market,sort_order,added_at) VALUES(?,?,?,?,?)",
                          (s,nm,mk,i,int(time.time())))

def http_get(url, headers=None, timeout=10):
    req = urllib.request.Request(url, headers=headers or {'User-Agent': UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode('utf-8', 'ignore')

def http_get_curl(url, headers=None, timeout=12):
    """curl 兑底：urllib 被 Yahoo 429 限流时，curl 的 TLS 指纹往往能正常返回"""
    import subprocess
    cmd = ['curl', '-s', '--max-time', str(int(timeout)),
           '-H', 'User-Agent: ' + (headers or {}).get('User-Agent', UA),
           '-H', 'Accept: application/json,text/plain,*/*',
           '-H', 'Referer: https://finance.yahoo.com/', url]
    out = subprocess.run(cmd, capture_output=True, timeout=timeout + 3)
    if out.returncode != 0:
        raise RuntimeError('curl failed rc=%d' % out.returncode)
    return out.stdout.decode('utf-8', 'ignore')

def http_get_retry(url, headers=None, timeout=12, tries=3):
    """先 urllib，429/失败时退避重试；多次失败后用 curl 兑底"""
    last = None
    for i in range(tries):
        try:
            return http_get(url, headers=headers, timeout=timeout)
        except urllib.error.HTTPError as e:
            last = e
            if e.code == 429:
                time.sleep(1.5 * (i + 1))  # 退避
                continue
            # 非 429 的 HTTP 错误也试一次 curl
            break
        except Exception as e:
            last = e
            time.sleep(0.8)
    # urllib 全部失败 → curl 兑底
    try:
        return http_get_curl(url, headers=headers, timeout=timeout)
    except Exception as e:
        last = e
    if last:
        raise last

# ========== 数据抓取 ==========
def _eastmoney_secid(symbol):
    """600519.SS -> 1.600519 ; 000001.SZ -> 0.000001"""
    code = symbol.split('.')[0]
    if symbol.endswith('.SS'):
        return f'1.{code}'
    if symbol.endswith('.SZ'):
        return f'0.{code}'
    return None

def fetch_cn_sina(symbols):
    """A股新浪源(主), ~实时。格式: 名称,今开,昨收,现价,最高,最低,...,成交量,成交额"""
    out = {}
    # 新浪支持批量: list=sh600519,sz000001
    sina_codes = []
    code_map = {}
    for sym in symbols:
        code = sym.split('.')[0]
        pfx = 'sh' if sym.endswith('.SS') else ('sz' if sym.endswith('.SZ') else None)
        if not pfx:
            continue
        sc = pfx + code
        sina_codes.append(sc)
        code_map[sc] = sym
    if not sina_codes:
        return out
    for attempt in range(2):
        try:
            url = 'https://hq.sinajs.cn/list=' + ','.join(sina_codes)
            req = urllib.request.Request(url, headers={'User-Agent': UA, 'Referer': 'https://finance.sina.com.cn'})
            with urllib.request.urlopen(req, timeout=8) as r:
                raw = r.read().decode('gbk', 'ignore')
            for line in raw.splitlines():
                m = re.match(r'var hq_str_(\w+)="(.*)";', line)
                if not m:
                    continue
                sc, body = m.group(1), m.group(2)
                sym = code_map.get(sc)
                if not sym or not body:
                    continue
                p = body.split(',')
                if len(p) < 32:
                    continue
                name = p[0]; op = float(p[1]); prev = float(p[2]); price = float(p[3])
                high = float(p[4]); low = float(p[5]); vol = float(p[8] or 0)
                chg = price - prev
                out[sym] = {
                    'symbol': sym, 'name': name, 'market': 'cn',
                    'price': round(price, 2),
                    'change_pct': round((chg / prev * 100) if prev else 0, 2),
                    'change_amt': round(chg, 2),
                    'open': round(op, 2), 'high': round(high, 2), 'low': round(low, 2),
                    'prev_close': round(prev, 2), 'volume': vol,
                    'ts': int(time.time()),
                }
            if out:
                return out
        except Exception:
            time.sleep(0.6)
    return out

def fetch_cn_eastmoney(symbols):
    """A股走东财, ~3秒延迟（带重试）"""
    out = {}
    for sym in symbols:
        secid = _eastmoney_secid(sym)
        if not secid:
            continue
        for attempt in range(2):
            try:
                url = f'https://push2.eastmoney.com/api/qt/stock/get?secid={secid}&fields=f43,f57,f58,f60,f44,f45,f46,f47,f169,f170'
                raw = http_get(url, timeout=8)
                if not raw.strip():
                    raise ValueError('empty')
                d = json.loads(raw).get('data') or {}
                if not d or d.get('f43') in (None, '-'):
                    raise ValueError('no data')
                price = d.get('f43', 0) / 100
                prev = d.get('f60', 0) / 100
                out[sym] = {
                    'symbol': sym, 'name': d.get('f58') or sym, 'market': 'cn',
                    'price': round(price, 2),
                    'change_pct': round((d.get('f170') or 0) / 100, 2),
                    'change_amt': round((d.get('f169') or 0) / 100, 2),
                    'open': round((d.get('f46') or 0) / 100, 2),
                    'high': round((d.get('f44') or 0) / 100, 2),
                    'low': round((d.get('f45') or 0) / 100, 2),
                    'prev_close': round(prev, 2),
                    'volume': d.get('f47') or 0,
                    'ts': int(time.time()),
                }
                break
            except Exception:
                time.sleep(0.6)
        time.sleep(0.2)
    return out

def fetch_yahoo(symbols):
    """港股/美股/ETF/外汇/加密走 Yahoo（带间隔+重试防限流）"""
    out = {}
    for i, sym in enumerate(symbols):
        got = False
        for attempt in range(2):
            try:
                host = 'query1' if attempt == 0 else 'query2'
                url = f'https://{host}.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(sym)}?interval=1d&range=1d'
                d = json.loads(http_get_retry(url, timeout=12))
                res = d.get('chart', {}).get('result')
                if not res:
                    raise ValueError('no result')
                m = res[0]['meta']
                price = m.get('regularMarketPrice')
                prev = m.get('chartPreviousClose') or m.get('previousClose') or price
                if price is None:
                    raise ValueError('no price')
                chg = price - prev if prev else 0
                out[sym] = {
                    'symbol': sym, 'name': m.get('shortName') or sym, 'market': None,
                    'price': round(price, 4),
                    'change_pct': round((chg / prev * 100) if prev else 0, 2),
                    'change_amt': round(chg, 4),
                    'open': m.get('regularMarketOpen') or 0,
                    'high': m.get('regularMarketDayHigh') or 0,
                    'low': m.get('regularMarketDayLow') or 0,
                    'prev_close': round(prev, 4) if prev else 0,
                    'volume': m.get('regularMarketVolume') or 0,
                    'ts': int(time.time()),
                }
                got = True
                break
            except Exception:
                time.sleep(0.8)
        # 防限流：请求间隔
        time.sleep(0.4)
    return out

def _tencent_code(sym):
    """统一代码 -> 腾讯代码。AAPL->usAAPL, 0700.HK->hk00700"""
    if sym.endswith('.HK'):
        code = sym[:-3]
        return 'hk' + code.zfill(5)
    if sym.endswith('-USD') or sym.endswith('=X'):
        return None  # 加密/外汇腾讯不支持，保留给DB兑底
    # 美股（无后缀）
    return 'us' + sym.upper()

def fetch_tencent(symbols):
    """腾讯行情源（Yahoo后路，美股/港股）。返回 GBK 编码，字段 ~ 分隔"""
    out = {}
    code_map = {}
    for s in symbols:
        tc = _tencent_code(s)
        if tc:
            code_map[tc] = s
    if not code_map:
        return out
    try:
        import subprocess
        url = 'https://qt.gtimg.cn/q=' + ','.join(code_map.keys())
        raw = subprocess.run(['curl', '-s', '--max-time', '10', url],
                             capture_output=True, timeout=13).stdout.decode('gbk', 'ignore')
        for line in raw.strip().split(';'):
            line = line.strip()
            if not line or '=' not in line:
                continue
            tc = line.split('=')[0].replace('v_', '').strip()
            sym = code_map.get(tc)
            if not sym:
                continue
            payload = line.split('"')[1] if '"' in line else ''
            f = payload.split('~')
            if len(f) < 6:
                continue
            try:
                price = float(f[3]); prev = float(f[4]); openp = float(f[5])
            except (ValueError, IndexError):
                continue
            if price <= 0:
                continue
            chg = price - prev if prev else 0
            high = float(f[33]) if len(f) > 33 and f[33] else 0
            low = float(f[34]) if len(f) > 34 and f[34] else 0
            out[sym] = {
                'symbol': sym, 'name': f[1] or sym, 'market': None,
                'price': round(price, 4),
                'change_pct': round((chg / prev * 100) if prev else 0, 2),
                'change_amt': round(chg, 4),
                'open': openp, 'high': high, 'low': low,
                'prev_close': round(prev, 4) if prev else 0,
                'volume': float(f[6]) if len(f) > 6 and f[6] else 0,
                'ts': int(time.time()),
            }
    except Exception:
        pass
    return out

def get_latest_from_db(symbols):
    """从DB读取最新快照（当实时抓取失败时兼底）"""
    out = {}
    if not symbols:
        return out
    with db() as c:
        qs = ','.join('?' * len(symbols))
        for r in c.execute(f"SELECT * FROM quote_latest WHERE symbol IN ({qs})", symbols):
            out[r['symbol']] = dict(r)
    return out

def fetch_quotes(symbols, fallback_db=True):
    """根据后缀分流到不同源；A股新浪主+东财备；失败用DB旧值兼底"""
    cn = [s for s in symbols if s.endswith('.SS') or s.endswith('.SZ')]
    other = [s for s in symbols if s not in cn]
    result = {}
    if cn:
        r = fetch_cn_sina(cn)
        missing = [s for s in cn if s not in r]
        if missing:
            r.update(fetch_cn_eastmoney(missing))
        result.update(r)
    if other:
        r = fetch_yahoo(other)
        # A3: Yahoo 失败的用腾讯顶上（美股/港股）
        missing = [s for s in other if s not in r]
        if missing:
            r.update(fetch_tencent(missing))
        result.update(r)
    # DB旧值兼底
    if fallback_db:
        failed = [s for s in symbols if s not in result]
        if failed:
            old = get_latest_from_db(failed)
            for s, q in old.items():
                q['stale'] = True
                result[s] = q
    return result

def _guess_market(sym):
    if sym.endswith('.SS') or sym.endswith('.SZ'):
        return 'cn'
    if sym.endswith('.HK'):
        return 'hk'
    if sym.endswith('=X'):
        return 'fx'
    if sym.endswith('-USD'):
        return 'crypto'
    return 'us'

def save_quotes(quotes, name_market_map=None):
    """存入 quote_latest + quotes 历史快照"""
    nm = name_market_map or {}
    with _db_lock, db() as c:
        for sym, q in quotes.items():
            mk = q.get('market') or nm.get(sym, {}).get('market') or _guess_market(sym)
            name = q.get('name') or nm.get(sym, {}).get('name') or sym
            row = (sym, name, mk, q['price'], q['change_pct'], q['change_amt'],
                   q['open'], q['high'], q['low'], q['prev_close'], q['volume'], q['ts'])
            c.execute("""INSERT OR REPLACE INTO quote_latest
                (symbol,name,market,price,change_pct,change_amt,open,high,low,prev_close,volume,ts)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""", row)
            c.execute("""INSERT OR REPLACE INTO quotes
                (symbol,name,market,price,change_pct,change_amt,open,high,low,prev_close,volume,ts)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""", row)

# ========== 异动检测（里程A / 移植Mstock双窗算法）==========
MONITOR_DEFAULTS = {
    'long_window_min': 180, 'long_threshold_pct': 5.0,
    'short_window_min': 5, 'short_threshold_pct': 1.0,
    'alert_cooldown_min': 10, 'enabled': 1,
}

def get_monitor_config():
    cfg = dict(MONITOR_DEFAULTS)
    try:
        with db() as c:
            for r in c.execute('SELECT k,v FROM monitor_config'):
                k = r['k']
                if k in cfg:
                    cfg[k] = type(MONITOR_DEFAULTS[k])(float(r['v'])) if k != 'enabled' else int(float(r['v']))
    except Exception:
        pass
    return cfg

class AnomalyDetector:
    """每只股维护(ts,price)历史队列，找N分钟前基准价→算涨跌速度→超阈且过冷却就报警"""
    def __init__(self):
        from collections import defaultdict, deque
        self.history = defaultdict(lambda: deque(maxlen=2000))  # symbol -> [(ts,price)]
        self.alerted_at = {}  # (symbol,window,direction) -> ts
        self._lock = threading.RLock()

    def prune(self, q, now, keep_sec):
        while q and now - q[0][0] > keep_sec:
            q.popleft()

    def price_at_or_before(self, q, target_ts):
        """找 <= target_ts 的最晚一个价格点"""
        found = None
        for ts, price in q:
            if ts <= target_ts:
                found = (ts, price)
            else:
                break
        return found

    def should_alert(self, symbol, window, pct, threshold, cooldown_min, now):
        crossed = abs(pct) >= threshold
        if not crossed:
            return False, None
        direction = 'up' if pct >= 0 else 'down'
        key = (symbol, window, direction)
        last = self.alerted_at.get(key, 0)
        if now - last < cooldown_min * 60:
            return False, direction
        self.alerted_at[key] = now
        return True, direction

    def feed(self, item, now=None):
        """喂一条行情，返回触发的异动列表 [dict]"""
        now = now or int(time.time())
        cfg = get_monitor_config()
        if not cfg.get('enabled'):
            return []
        symbol = item.get('symbol')
        price = float(item.get('price') or 0)
        if not symbol or price <= 0:
            return []
        with self._lock:
            q = self.history[symbol]
            q.append((now, price))
            keep = max(cfg['long_window_min'], cfg['short_window_min']) * 60 + 120
            self.prune(q, now, keep)
            out = []
            windows = [
                (f"{cfg['long_window_min']}分钟", cfg['long_window_min'], cfg['long_threshold_pct']),
                (f"{cfg['short_window_min']}分钟", cfg['short_window_min'], cfg['short_threshold_pct']),
            ]
            for label, wmin, thr in windows:
                base = self.price_at_or_before(q, now - wmin * 60)
                if not base or base[1] <= 0:
                    continue
                pct = (price / base[1] - 1.0) * 100.0
                ok, direction = self.should_alert(symbol, label, pct, thr, cfg['alert_cooldown_min'], now)
                if ok:
                    out.append({
                        'symbol': symbol, 'name': item.get('name') or symbol,
                        'market': item.get('market') or '', 'window': label,
                        'pct': round(pct, 2), 'direction': direction,
                        'price': price, 'pct_today': float(item.get('change_pct') or 0),
                        'ts': now,
                    })
            return out

_detector = AnomalyDetector()

def is_a_share_trading_time(now=None):
    """A股交易时段判断（9:30-11:30 / 13:00-15:00，周一至周五，北京时间）"""
    import datetime
    t = datetime.datetime.utcfromtimestamp(now or time.time()) + datetime.timedelta(hours=8)
    if t.weekday() >= 5:  # 周六日
        return False
    hm = t.hour * 60 + t.minute
    return (9*60+30 <= hm <= 11*60+30) or (13*60 <= hm <= 15*60)

def save_alerts(alerts):
    if not alerts:
        return 0
    with _db_lock, db() as c:
        for a in alerts:
            c.execute("""INSERT INTO alerts(symbol,name,market,window,pct,direction,price,pct_today,ts)
                VALUES(?,?,?,?,?,?,?,?,?)""",
                (a['symbol'], a['name'], a['market'], a['window'], a['pct'],
                 a['direction'], a['price'], a['pct_today'], a['ts']))
    return len(alerts)

# ========== 历史 K 线 ==========
def fetch_kline_eastmoney(symbol, period='daily', count=250):
    """A股 K线走东方财富（不限流，秒回）"""
    secid = _eastmoney_secid(symbol)
    if not secid:
        return []
    klt = {'daily': '101', 'weekly': '102', 'monthly': '103'}.get(period, '101')
    rows = []
    try:
        url = (f'https://push2his.eastmoney.com/api/qt/stock/kline/get?secid={secid}'
               f'&fields1=f1&fields2=f51,f52,f53,f54,f55,f56&klt={klt}&fqt=1&end=20500101&lmt={count}')
        d = json.loads(http_get(url, timeout=10))
        klines = (d.get('data') or {}).get('klines') or []
        for line in klines:
            p = line.split(',')
            # f51日期 f52开 f53收 f54高 f55低 f56量
            date, o, cl, h, l, v = p[0], float(p[1]), float(p[2]), float(p[3]), float(p[4]), float(p[5])
            rows.append((symbol, period, date, o, h, l, cl, v))
    except Exception:
        pass
    return rows

def fetch_kline(symbol, period='daily', count=250):
    """拉历史K线。A股走东财（不限流），其余Yahoo。period: daily/weekly/monthly"""
    # A股优先东财
    if symbol.endswith('.SS') or symbol.endswith('.SZ'):
        rows = fetch_kline_eastmoney(symbol, period, count)
        if rows:
            return rows
    rng = {'daily': '1y', 'weekly': '2y', 'monthly': '5y'}.get(period, '1y')
    iv = {'daily': '1d', 'weekly': '1wk', 'monthly': '1mo'}.get(period, '1d')
    rows = []
    try:
        url = f'https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(symbol)}?interval={iv}&range={rng}'
        d = json.loads(http_get_retry(url, timeout=15))
        res = d['chart']['result'][0]
        ts = res['timestamp']
        q = res['indicators']['quote'][0]
        import datetime
        for i, t in enumerate(ts):
            o, h, l, cl, v = q['open'][i], q['high'][i], q['low'][i], q['close'][i], q['volume'][i]
            if cl is None:
                continue
            date = datetime.datetime.utcfromtimestamp(t).strftime('%Y-%m-%d')
            rows.append((symbol, period, date, o or 0, h or 0, l or 0, cl, v or 0))
    except Exception:
        pass
    return rows

def save_kline(rows):
    if not rows:
        return 0
    with _db_lock, db() as c:
        c.executemany("""INSERT OR REPLACE INTO kline
            (symbol,period,date,open,high,low,close,volume) VALUES(?,?,?,?,?,?,?,?)""", rows)
    return len(rows)

def get_kline_db(symbol, period='daily', limit=250):
    with db() as c:
        rs = c.execute("SELECT date,open,high,low,close,volume FROM kline WHERE symbol=? AND period=? ORDER BY date DESC LIMIT ?",
                       (symbol, period, limit)).fetchall()
    return [dict(r) for r in reversed(rs)]

# ========== HTTP 服务 ==========
_quote_cache = {'data': None, 'ts': 0}
QUOTE_TTL = 8  # 秒

class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **k):
        super().__init__(*a, directory=BASE_DIR, **k)

    def log_message(self, *a):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(body)

    def end_headers(self):
        # 静态资源禁止缓存，避免浏览器加载旧版 HTML/JS
        p = urllib.parse.urlparse(self.path).path
        if not p.startswith('/api/'):
            self.send_header('Cache-Control', 'no-store, no-cache, must-revalidate, max-age=0')
            self.send_header('Pragma', 'no-cache')
            self.send_header('Expires', '0')
        super().end_headers()

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        try:
            if path == '/api/finance/quotes':
                return self._api_quotes(q)
            if path == '/api/finance/kline':
                return self._api_kline(q)
            if path == '/api/finance/watchlist':
                return self._api_watchlist_get()
            if path == '/api/finance/alerts':
                return self._api_alerts(q)
            if path == '/' :
                self.path = '/public/finance.html'
        except Exception as e:
            return self._json(500, {'ok': False, 'error': str(e)})
        return super().do_GET()

    def _api_quotes(self, q):
        """立即返回DB数据（秒回），实时刷新由后台线程做"""
        with db() as c:
            syms = [r['symbol'] for r in c.execute('SELECT symbol FROM watchlist ORDER BY sort_order')]
            latest = get_latest_from_db(syms)
        items = [latest[s] for s in syms if s in latest]
        now = int(time.time())
        fresh = sum(1 for i in items if now - (i.get('ts') or 0) < 120)
        return self._json(200, {'ok': True, 'items': items, 'ts': now,
                                'fresh': fresh, 'total': len(syms)})

    def _api_kline(self, q):
        symbol = (q.get('symbol') or [''])[0]
        period = (q.get('period') or ['daily'])[0]
        if not symbol:
            return self._json(400, {'ok': False, 'error': 'symbol required'})
        rows = get_kline_db(symbol, period)
        if not rows:
            save_kline(fetch_kline(symbol, period))
            rows = get_kline_db(symbol, period)
        return self._json(200, {'ok': True, 'symbol': symbol, 'period': period, 'kline': rows})

    def _api_watchlist_get(self):
        with db() as c:
            rs = c.execute('SELECT symbol,name,market,sort_order FROM watchlist ORDER BY sort_order').fetchall()
        return self._json(200, {'ok': True, 'items': [dict(r) for r in rs]})

    def _api_alerts(self, q):
        """返回最近异动报警。since=时间戳只返回之后的；limit限数量"""
        try:
            since = int((q.get('since') or ['0'])[0])
        except ValueError:
            since = 0
        try:
            limit = min(int((q.get('limit') or ['30'])[0]), 100)
        except ValueError:
            limit = 30
        with db() as c:
            if since > 0:
                rs = c.execute('SELECT * FROM alerts WHERE ts>? ORDER BY ts DESC LIMIT ?', (since, limit)).fetchall()
            else:
                rs = c.execute('SELECT * FROM alerts ORDER BY ts DESC LIMIT ?', (limit,)).fetchall()
        items = [dict(r) for r in rs]
        return self._json(200, {'ok': True, 'items': items, 'ts': int(time.time()),
                                'trading': is_a_share_trading_time()})

if __name__ == '__main__':
    init_db()
    print('DB initialized at', DB_PATH)
    # 自测: 抓一批自选报价
    import sys
    if '--test' in sys.argv:
        with db() as c:
            syms = [r['symbol'] for r in c.execute('SELECT symbol FROM watchlist ORDER BY sort_order')]
        print('fetching', len(syms), 'symbols...')
        q = fetch_quotes(syms)
        for s in syms:
            if s in q:
                print(f"  {q[s]['name']:<12} {q[s]['price']:>12}  {q[s]['change_pct']:>+6.2f}%")
            else:
                print(f"  {s:<12} FAILED")
        save_quotes(q)
        print('saved', len(q), 'quotes to DB')
    else:
        # 后台刷新线程：定时抓自选报价存DB（不阻塞API）+ 异动检测
        def bg_refresh():
            while True:
                try:
                    with db() as c:
                        syms = [r['symbol'] for r in c.execute('SELECT symbol FROM watchlist ORDER BY sort_order')]
                    q = fetch_quotes(syms, fallback_db=False)
                    save_quotes(q)
                    # A2: 异动检测（仅A股交易时段，避免非交易时段误报）
                    if is_a_share_trading_time():
                        hits = []
                        for s, item in q.items():
                            hits.extend(_detector.feed(item))
                        if hits:
                            save_alerts(hits)
                            for h in hits:
                                arrow = '↑' if h['direction'] == 'up' else '↓'
                                print(f"[异动] {h['name']}({h['symbol']}) {h['window']} {arrow}{h['pct']:+.2f}%")
                except Exception:
                    pass
                time.sleep(20)
        t = threading.Thread(target=bg_refresh, daemon=True)
        t.start()
        srv = ThreadingHTTPServer(('0.0.0.0', PORT), Handler)
        print(f'FinSight serving at http://0.0.0.0:{PORT}')
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            srv.shutdown()
