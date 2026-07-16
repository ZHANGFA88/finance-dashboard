#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FinSight 金融大屏后端 - 纯标准库 + SQLite
多源: A股走东方财富(~3秒), 港美股/ETF/外汇/加密走 Yahoo Finance
"""
import os, json, time, sqlite3, urllib.parse, urllib.request, re, threading, random
import http.cookiejar, gzip
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PUBLIC_DIR = os.path.join(BASE_DIR, 'public')
DATA_DIR = os.path.join(BASE_DIR, 'data')
DB_PATH = os.path.join(DATA_DIR, 'finance.db')
PORT = int(os.environ.get('FINANCE_PORT', '8770'))
HOST = os.environ.get('FINANCE_HOST', '127.0.0.1')  # 默认仅本机；局域网访问设 FINANCE_HOST=0.0.0.0

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

# ========== 主动限流器(吸收 Vibe-Trading HostThrottle: 从源头防封IP) ==========
class _HostThrottle:
    """按host分桶强制最小请求间隔+随机抖动, 进程内共享。
    东财等公共免费源按IP封禁, 突发请求会被软封; 主动限速从根上避免触发。"""
    _JITTER = 0.3
    def __init__(self):
        self._last = {}
        self._lock = threading.Lock()
    def wait(self, bucket, min_interval):
        if min_interval <= 0:
            return
        with self._lock:
            now = time.monotonic()
            last = self._last.get(bucket)
            if last is None or now >= last + min_interval:
                fire_at = now
            else:
                fire_at = last + min_interval + random.uniform(0.0, self._JITTER)
            self._last[bucket] = fire_at
        sleep_for = fire_at - time.monotonic()
        if sleep_for > 0:
            time.sleep(sleep_for)

_THROTTLE = _HostThrottle()

# 各host的最小请求间隔(秒): 东财封IP最凶, 间隔最大; 新浪/腾讯宽松
_HOST_INTERVALS = {
    'push2.eastmoney.com': 0.6,
    'push2his.eastmoney.com': 0.6,
    'quote.eastmoney.com': 0.6,
    'hq.sinajs.cn': 0.2,
    'money.finance.sina.com.cn': 0.25,
    'vip.stock.finance.sina.com.cn': 0.25,
    'qt.gtimg.cn': 0.2,
    'finance.pae.baidu.com': 0.25,
}

def _throttle_url(url):
    """根据URL的host自动限速。未登记的host不限速。"""
    try:
        host = urllib.parse.urlparse(url).hostname or ''
    except Exception:
        return
    iv = _HOST_INTERVALS.get(host)
    if iv:
        _THROTTLE.wait(host, iv)

def http_get(url, headers=None, timeout=10):
    _throttle_url(url)
    req = urllib.request.Request(url, headers=headers or {'User-Agent': UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode('utf-8', 'ignore')

def http_get_curl(url, headers=None, timeout=12):
    """curl 兑底：urllib 被 Yahoo 429 限流时，curl 的 TLS 指纹往往能正常返回"""
    import subprocess
    _throttle_url(url)
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

_yahoo_breaker = {'until': 0}  # Yahoo 429熔断: until前直接跳过不死等
def fetch_yahoo(symbols):
    """港股/美股/ETF/外汇/加密走 Yahoo（带熔断+重试防限流）"""
    out = {}
    # 熔断期内直接跳过Yahoo(避免429时死等拖垮后台刷新)
    if time.time() < _yahoo_breaker['until']:
        return out
    for i, sym in enumerate(symbols):
        got = False
        for attempt in range(1):
            try:
                host = 'query1'
                url = f'https://{host}.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(sym)}?interval=1d&range=1d'
                d = json.loads(http_get_retry(url, timeout=5))
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
                # Yahoo任何失败(429/超时/curl rc28)都触发熔断: 冷却60s内直接跳过,
                # 不死等拖垮后台刷新。加密/外汇走DB兜底，影响小。
                _yahoo_breaker['until'] = time.time() + 60
                return out
        # 防限流：请求间隔
        time.sleep(0.15)
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
        _throttle_url(url)
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

# ========== 雪球 token 管理器(第5源, token缓存~25分钟) ==========
_xq_state = {'opener': None, 'ts': 0}
_xq_lock = threading.Lock()

def _xq_opener(force=False):
    """获取带雪球 token 的 opener，token 缓存 1500s。两步式: 首页拿acw_tc → /hq下发xq_a_token。"""
    now = time.time()
    with _xq_lock:
        if not force and _xq_state['opener'] and now - _xq_state['ts'] < 1500:
            return _xq_state['opener']
        cj = http.cookiejar.CookieJar()
        op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
        op.addheaders = [('User-Agent', UA)]
        try:
            _THROTTLE.wait('xueqiu.com', 0.3)
            op.open('https://xueqiu.com', timeout=10).read()
            _THROTTLE.wait('xueqiu.com', 0.3)
            op.open('https://xueqiu.com/hq', timeout=10).read()
        except Exception:
            return None
        names = [c.name for c in cj]
        if 'xq_a_token' not in names:
            return None
        _xq_state['opener'] = op
        _xq_state['ts'] = now
        return op

def fetch_cn_xueqiu(symbols):
    """A股行情备源: 雪球 batch/quote(第5层兜底, 字段最全, 支持批量)。"""
    out = {}
    xq_syms = []
    code_map = {}
    for sym in symbols:
        code = sym.split('.')[0]
        if sym.endswith('.SS'):
            xs = 'SH' + code
        elif sym.endswith('.SZ'):
            xs = 'SZ' + code
        else:
            continue
        xq_syms.append(xs)
        code_map[xs] = sym
    if not xq_syms:
        return out
    for attempt in range(2):
        op = _xq_opener(force=(attempt > 0))
        if not op:
            time.sleep(0.5)
            continue
        try:
            url = ('https://stock.xueqiu.com/v5/stock/batch/quote.json?symbol='
                   + ','.join(xq_syms))
            _THROTTLE.wait('xueqiu.com', 0.3)
            req = urllib.request.Request(url, headers={
                'User-Agent': UA, 'Referer': 'https://xueqiu.com',
                'Accept': 'application/json'})
            r = op.open(req, timeout=10)
            raw = r.read()
            if r.headers.get('Content-Encoding') == 'gzip':
                raw = gzip.decompress(raw)
            d = json.loads(raw.decode('utf-8', 'ignore'))
            items = (d.get('data') or {}).get('items') or []
            for it in items:
                q = it.get('quote') or {}
                xs = q.get('symbol')
                sym = code_map.get(xs)
                if not sym:
                    continue
                price = q.get('current')
                if price in (None, '', 0):
                    continue
                prev = q.get('last_close') or 0
                out[sym] = {
                    'symbol': sym, 'name': q.get('name') or sym, 'market': 'cn',
                    'price': round(float(price), 2),
                    'change_pct': round(float(q.get('percent') or 0), 2),
                    'change_amt': round(float(q.get('chg') or 0), 2),
                    'open': round(float(q.get('open') or 0), 2),
                    'high': round(float(q.get('high') or 0), 2),
                    'low': round(float(q.get('low') or 0), 2),
                    'prev_close': round(float(prev), 2),
                    'volume': float(q.get('volume') or 0),
                    'ts': int(time.time()),
                }
            if out:
                return out
        except Exception:
            _xq_state['opener'] = None  # 下次强制重取token
            time.sleep(0.5)
    return out

def fetch_cn_baidu(symbols):
    """A股行情备源: 百度股市通(新浪/东财都挂时的第4层兜底)。逐只拉, 带限流。"""
    import subprocess
    out = {}
    for sym in symbols:
        code = sym.split('.')[0]
        if not (sym.endswith('.SS') or sym.endswith('.SZ')):
            continue
        url = ('https://finance.pae.baidu.com/vapi/v1/getquotation?srcid=5353'
               '&pointType=string&group=quotation_minute_ab&query=%s&code=%s'
               '&market_type=ab&newFormat=1' % (code, code))
        try:
            _throttle_url(url)
            raw = subprocess.run(['curl', '-s', '--max-time', '10',
                                  '-H', 'User-Agent: ' + UA,
                                  '-H', 'Referer: https://gushitong.baidu.com/', url],
                                 capture_output=True, timeout=13).stdout.decode('utf-8', 'ignore')
            d = json.loads(raw)
            r = d.get('Result') or {}
            cur = r.get('cur') or {}
            basic = r.get('basicinfos') or {}
            price = cur.get('price')
            if price in (None, '', '--'):
                continue
            price = float(price)
            if price <= 0:
                continue
            # ratio 形如 "+0.00%"; increase 形如 "+0.03"
            def _num(s):
                try:
                    return float(str(s).replace('%', '').replace('+', '').strip())
                except (TypeError, ValueError):
                    return 0
            chg_amt = _num(cur.get('increase'))
            pct = _num(cur.get('ratio'))
            prev = round(price - chg_amt, 2) if chg_amt else price
            # 从盘口拿开/高/低
            pk = {x.get('ename'): x.get('value') for x in (r.get('pankouinfos') or {}).get('list', [])}
            def _pk(k):
                v = pk.get(k)
                try:
                    return float(v)
                except (TypeError, ValueError):
                    return 0
            out[sym] = {
                'symbol': sym, 'name': basic.get('name') or sym, 'market': 'cn',
                'price': round(price, 2),
                'change_pct': round(pct, 2),
                'change_amt': round(chg_amt, 2),
                'open': _pk('open'), 'high': _pk('high'), 'low': _pk('low'),
                'prev_close': prev,
                'volume': _num(cur.get('volume')),
                'ts': int(time.time()),
            }
        except Exception:
            continue
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
        missing = [s for s in cn if s not in r]
        if missing:
            r.update(fetch_cn_xueqiu(missing))
        missing = [s for s in cn if s not in r]
        if missing:
            r.update(fetch_cn_baidu(missing))
        result.update(r)
    if other:
        # 港股/美股: 腾讯优先(国内快且稳, 0.2s), Yahoo只留给腾讯不支持的加密/外汇
        tencent_ok = [s for s in other if _tencent_code(s)]  # 港美股
        yahoo_only = [s for s in other if not _tencent_code(s)]  # 加密-USD/外汇=X
        r = {}
        if tencent_ok:
            r.update(fetch_tencent(tencent_ok))
        # 腾讯没拿到的 + 加密外汇 → Yahoo兼底
        missing = [s for s in other if s not in r]
        if missing:
            r.update(fetch_yahoo(missing))
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

def _normalize_symbol(raw):
    """智能规范化用户输入的代码, 自动补后缀。
    纯数字: 6/9开头→沪市.SS, 0/2/3开头→深市.SZ, 4/8开头→深(北交所暂归.SZ探测)
    已带后缀/美股/加密等保持原样。"""
    s = (raw or '').strip().upper()
    if not s:
        return s
    # 已带后缀或特殊后缀, 不动
    if any(s.endswith(x) for x in ('.SS', '.SZ', '.HK', '=X', '-USD')):
        return s
    # 纯数字 A股代码: 按首位判断沪深
    if s.isdigit():
        if len(s) == 6:
            # 6/9→沪; 0/2/3→深; 其余默认沪
            if s[0] in ('6', '9'):
                return s + '.SS'
            if s[0] in ('0', '2', '3'):
                return s + '.SZ'
            return s + '.SS'
        # 4位/5位数字很可能是港股
        if len(s) in (4, 5):
            return s.zfill(4) + '.HK'
    return s

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
        d = json.loads(http_get_retry(url, timeout=10))
        klines = (d.get('data') or {}).get('klines') or []
        for line in klines:
            p = line.split(',')
            # f51日期 f52开 f53收 f54高 f55低 f56量
            date, o, cl, h, l, v = p[0], float(p[1]), float(p[2]), float(p[3]), float(p[4]), float(p[5])
            rows.append((symbol, period, date, o, h, l, cl, v))
    except Exception:
        pass
    return rows

def fetch_kline_sina(symbol, period='daily', count=250):
    """A股 K线备用源: 新浪(东财push2his被封时兜底)"""
    # symbol: 600519.SS/000001.SZ -> sh600519/sz000001
    code = symbol.split('.')[0]
    prefix = 'sh' if symbol.endswith('.SS') else 'sz'
    sina_sym = prefix + code
    scale = {'daily': 240, 'weekly': 1680, 'monthly': 7200}.get(period, 240)
    import subprocess
    url = ('https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/'
           'CN_MarketData.getKLineData?symbol=%s&scale=%d&ma=no&datalen=%d'
           % (sina_sym, scale, count))
    rows = []
    try:
        _throttle_url(url)
        raw = subprocess.run(['curl', '-s', '--max-time', '10',
                              '-H', 'Referer: https://finance.sina.com.cn',
                              '-H', 'User-Agent: ' + UA, url],
                             capture_output=True, timeout=13).stdout.decode('utf-8', 'ignore')
        arr = json.loads(raw)
        for r in arr:
            date = (r.get('day') or '')[:10]
            if not date:
                continue
            o = float(r.get('open')); cl = float(r.get('close'))
            h = float(r.get('high')); l = float(r.get('low')); v = float(r.get('volume'))
            rows.append((symbol, period, date, o, h, l, cl, v))
    except Exception:
        pass
    return rows

def fetch_kline(symbol, period='daily', count=250):
    """拉历史K线。A股多源降级: 东财 → 新浪; 其余Yahoo。period: daily/weekly/monthly"""
    # A股多源降级
    if symbol.endswith('.SS') or symbol.endswith('.SZ'):
        rows = fetch_kline_eastmoney(symbol, period, count)
        if rows:
            return rows
        rows = fetch_kline_sina(symbol, period, count)
        if rows:
            print('[kline] %s 东财失败, 已降级新浪 (%d条)' % (symbol, len(rows)))
            return rows
        return []
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

# ========== 技术指标引擎（里程B1 / 纯Python无依赖）==========
def _ema(vals, n):
    """指数移动平均"""
    if not vals:
        return []
    k = 2.0 / (n + 1)
    out = [vals[0]]
    for v in vals[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out

def _sma(vals, n):
    """简单移动平均，返回与vals等长(不足n的位置为None)"""
    out = []
    s = 0.0
    from collections import deque
    q = deque()
    for v in vals:
        q.append(v); s += v
        if len(q) > n:
            s -= q.popleft()
        out.append(s / len(q) if len(q) == n else None)
    return out

def calc_ma(closes, periods=(5, 10, 20, 60, 120)):
    """均线：取每条均线的最新值"""
    out = {}
    for p in periods:
        if len(closes) >= p:
            out['MA%d' % p] = round(sum(closes[-p:]) / p, 3)
        else:
            out['MA%d' % p] = None
    return out

def calc_macd(closes, fast=12, slow=26, signal=9):
    """MACD: DIF/DEA/柱体(MACD)"""
    if len(closes) < slow:
        return {'DIF': None, 'DEA': None, 'MACD': None, 'trend': None}
    ema_fast = _ema(closes, fast)
    ema_slow = _ema(closes, slow)
    dif = [f - s for f, s in zip(ema_fast, ema_slow)]
    dea = _ema(dif, signal)
    macd = [(d - e) * 2 for d, e in zip(dif, dea)]
    # 趋势：柱体较前日变化
    trend = None
    if len(macd) >= 2:
        trend = 'up' if macd[-1] > macd[-2] else ('down' if macd[-1] < macd[-2] else 'flat')
    return {'DIF': round(dif[-1], 3), 'DEA': round(dea[-1], 3),
            'MACD': round(macd[-1], 3), 'trend': trend,
            'gold_cross': dif[-1] > dea[-1] and (len(dif) >= 2 and dif[-2] <= dea[-2]),
            'dead_cross': dif[-1] < dea[-1] and (len(dif) >= 2 and dif[-2] >= dea[-2])}

def calc_kdj(highs, lows, closes, n=9, m1=3, m2=3):
    """KDJ"""
    if len(closes) < n:
        return {'K': None, 'D': None, 'J': None, 'signal': None}
    rsv = []
    for i in range(len(closes)):
        lo = min(lows[max(0, i - n + 1):i + 1])
        hi = max(highs[max(0, i - n + 1):i + 1])
        rsv.append((closes[i] - lo) / (hi - lo) * 100 if hi > lo else 50.0)
    k = [50.0]; d = [50.0]
    for i in range(1, len(rsv)):
        k.append((m1 - 1) / m1 * k[-1] + 1.0 / m1 * rsv[i])
        d.append((m2 - 1) / m2 * d[-1] + 1.0 / m2 * k[-1])
    j = [3 * k[i] - 2 * d[i] for i in range(len(k))]
    kv, dv, jv = k[-1], d[-1], j[-1]
    sig = None
    if len(k) >= 2:
        if k[-1] > d[-1] and k[-2] <= d[-2]:
            sig = 'gold_cross'
        elif k[-1] < d[-1] and k[-2] >= d[-2]:
            sig = 'dead_cross'
        elif jv > 100 or kv > 80:
            sig = 'overbought'
        elif jv < 0 or kv < 20:
            sig = 'oversold'
    return {'K': round(kv, 2), 'D': round(dv, 2), 'J': round(jv, 2), 'signal': sig}

def calc_rsi(closes, n=14):
    """RSI14"""
    if len(closes) < n + 1:
        return None
    gains = []; losses = []
    for i in range(1, len(closes)):
        ch = closes[i] - closes[i - 1]
        gains.append(max(ch, 0)); losses.append(max(-ch, 0))
    # 首个n日均值，后续Wilder平滑
    ag = sum(gains[:n]) / n; al = sum(losses[:n]) / n
    for i in range(n, len(gains)):
        ag = (ag * (n - 1) + gains[i]) / n
        al = (al * (n - 1) + losses[i]) / n
    if al == 0:
        return 100.0
    rs = ag / al
    return round(100 - 100 / (1 + rs), 2)

def calc_boll(closes, n=20, k=2):
    """BOLL布林带: 中轨(MA20) + 上/下轨(±2倍标准差) + 带宽/开口收口"""
    if len(closes) < n:
        return {'mid': None, 'upper': None, 'lower': None, 'bandwidth': None, 'pos': None, 'state': None}
    seg = closes[-n:]
    mid = sum(seg) / n
    var = sum((c - mid) ** 2 for c in seg) / n
    std = var ** 0.5
    upper = mid + k * std
    lower = mid - k * std
    price = closes[-1]
    bandwidth = round((upper - lower) / mid * 100, 2) if mid else None  # 带宽%
    # 价格在带内位置 0(下轨)~1(上轨)
    pos = round((price - lower) / (upper - lower), 2) if upper > lower else 0.5
    # 开口/收口: 对比5日前带宽
    state = None
    if len(closes) >= n + 5:
        seg0 = closes[-n - 5:-5]
        mid0 = sum(seg0) / n
        std0 = (sum((c - mid0) ** 2 for c in seg0) / n) ** 0.5
        bw0 = (2 * k * std0) / mid0 * 100 if mid0 else 0
        if bandwidth and bw0:
            state = 'opening' if bandwidth > bw0 * 1.15 else ('closing' if bandwidth < bw0 * 0.85 else 'stable')
    return {'mid': round(mid, 3), 'upper': round(upper, 3), 'lower': round(lower, 3),
            'bandwidth': bandwidth, 'pos': pos, 'state': state}

def calc_volume_stats(klines):
    """量能：当日量、相对5日/20日均量比"""
    vols = [k['volume'] for k in klines]
    if not vols:
        return {}
    today = vols[-1]
    ma5 = sum(vols[-5:]) / min(5, len(vols))
    ma20 = sum(vols[-20:]) / min(20, len(vols))
    return {
        'volume': round(today, 1),
        'vol_ratio_5': round(today / ma5, 2) if ma5 else None,
        'vol_ratio_20': round(today / ma20, 2) if ma20 else None,
    }

def calc_high_low(klines, periods=(20, 60)):
    """近N日高低点"""
    out = {}
    for p in periods:
        seg = klines[-p:] if len(klines) >= p else klines
        if seg:
            out['high_%d' % p] = round(max(k['high'] for k in seg), 3)
            out['low_%d' % p] = round(min(k['low'] for k in seg), 3)
    return out

def calc_support_resistance(klines, ma):
    """支撑/压力位：综合近N日高低点 + 均线 + 整数关。
    返回价格上方最近压力位、下方最近支撑位，及候选列表"""
    if not klines or len(klines) < 5:
        return {}
    price = klines[-1]['close']
    cands = []  # (价格, 标签)
    # 近20/60日高低点
    for p in (20, 60):
        seg = klines[-p:] if len(klines) >= p else klines
        if seg:
            cands.append((round(max(k['high'] for k in seg), 2), '%d日高' % p))
            cands.append((round(min(k['low'] for k in seg), 2), '%d日低' % p))
    # 均线
    for key in ('MA20', 'MA60', 'MA120'):
        v = ma.get(key)
        if v:
            cands.append((round(v, 2), key))
    # 局部摆高摆低（分形高点/低点，左右2根）
    n = len(klines)
    for i in range(2, n - 2):
        h = klines[i]['high']; l = klines[i]['low']
        if h >= klines[i-1]['high'] and h >= klines[i-2]['high'] and h >= klines[i+1]['high'] and h >= klines[i+2]['high']:
            cands.append((round(h, 2), '前高'))
        if l <= klines[i-1]['low'] and l <= klines[i-2]['low'] and l <= klines[i+1]['low'] and l <= klines[i+2]['low']:
            cands.append((round(l, 2), '前低'))
    # 分上下，去重聚合(接近的1%内归一类)
    res = sorted(set(c for c in cands if c[0] > price * 1.001), key=lambda x: x[0])
    sup = sorted(set(c for c in cands if c[0] < price * 0.999), key=lambda x: -x[0])
    def dedup(lst):
        out = []
        for pv, lab in lst:
            if out and abs(pv - out[-1]['price']) / out[-1]['price'] < 0.008:
                out[-1]['labels'].append(lab)
            else:
                out.append({'price': pv, 'labels': [lab]})
        return out[:3]
    return {
        'price': price,
        'resistance': dedup(res),  # 上方压力(由近到远)
        'support': dedup(sup),     # 下方支撑(由近到远)
    }

def compute_indicators(klines):
    """聚合所有技术指标。输入 K线列表(按日期升序)"""
    if not klines or len(klines) < 2:
        return {'ok': False, 'error': 'insufficient kline'}
    closes = [k['close'] for k in klines]
    highs = [k['high'] for k in klines]
    lows = [k['low'] for k in klines]
    last = klines[-1]
    prev_close = klines[-2]['close']
    change_pct = round((last['close'] / prev_close - 1) * 100, 2) if prev_close else 0
    ma = calc_ma(closes)
    return {
        'ok': True,
        'date': last['date'],
        'close': last['close'],
        'change_pct': change_pct,
        'ma': ma,
        'macd': calc_macd(closes),
        'kdj': calc_kdj(highs, lows, closes),
        'rsi': calc_rsi(closes),
        'boll': calc_boll(closes),
        'volume': calc_volume_stats(klines),
        'high_low': calc_high_low(klines),
        'sr': calc_support_resistance(klines, ma),
    }

# ========== 大盘环境 + 板块热点（里程B2）==========
INDEX_MAP = [
    ('s_sh000001', '上证指数'),
    ('s_sz399001', '深证成指'),
    ('s_sz399006', '创业板指'),
    ('s_sh000688', '科创50'),
]

def fetch_indices():
    """四大指数当日涨跌（新浪轻量接口，curl拿原始GBK字节）"""
    codes = ','.join(c for c, _ in INDEX_MAP)
    out = []
    try:
        import subprocess
        url = 'https://hq.sinajs.cn/list=' + codes
        _throttle_url(url)
        raw = subprocess.run(['curl', '-s', '--max-time', '8',
                              '-H', 'Referer: https://finance.sina.com.cn',
                              '-H', 'User-Agent: ' + UA, url],
                             capture_output=True, timeout=11).stdout.decode('gbk', 'ignore')
        for line in raw.strip().split(';'):
            line = line.strip()
            if '="' not in line:
                continue
            code = line.split('_')[-1].split('=')[0].strip()
            payload = line.split('"')[1] if '"' in line else ''
            f = payload.split(',')
            if len(f) < 4:
                continue
            try:
                price = float(f[1]); chg = float(f[2]); pct = float(f[3])
            except ValueError:
                continue
            out.append({
                'code': code, 'name': f[0] or code,
                'price': round(price, 2), 'change_amt': round(chg, 2), 'change_pct': round(pct, 2),
            })
    except Exception:
        pass
    return out

# 全球大盘指数配置：[新浪代码, 显示名, 市场分组]
GLOBAL_INDEX_MAP = [
    ('gb_$dji', '道琼斯', 'us'),
    ('gb_ixic', '纳斯达克', 'us'),
    ('gb_inx', '标普500', 'us'),
    ('int_hangseng', '恒生指数', 'hk'),
    ('int_nikkei', '日经225', 'asia'),
    ('int_ftse', '英国FTSE', 'eu'),
    ('btc_btcbtcusd', '比特币', 'crypto'),
    ('btc_btcethusd', '以太坊', 'crypto'),
]

def fetch_global_indices():
    """全球大盘指数（新浪源，单请求拿全部，比Yahoo稳）。含美股盘中/恒生/日经/英股/加密"""
    import subprocess
    codes = ','.join(c for c, _, _ in GLOBAL_INDEX_MAP)
    meta = {c: (nm, mk) for c, nm, mk in GLOBAL_INDEX_MAP}
    out = []
    try:
        url = 'https://hq.sinajs.cn/list=' + codes
        _throttle_url(url)
        raw = subprocess.run(['curl', '-s', '--max-time', '10',
                              '-H', 'Referer: https://finance.sina.com.cn',
                              '-H', 'User-Agent: ' + UA, url],
                             capture_output=True, timeout=13).stdout.decode('gbk', 'ignore')
        for line in raw.strip().split(';'):
            line = line.strip()
            if '="' not in line:
                continue
            # 提取代码（hq_str_ 后面直到 =）
            code = line.split('hq_str_')[-1].split('=')[0].strip()
            payload = line.split('"')[1] if '"' in line else ''
            f = payload.split(',')
            if code not in meta or len(f) < 4:
                continue
            nm, mk = meta[code]
            try:
                if mk == 'crypto':
                    # 新浪加密格式: 时间,,,价格(idx3),0,买,高(idx6),低(idx7),参考价(idx8),名称(idx9),...
                    price = float(f[3])
                    prev = float(f[8]) if len(f) > 8 else 0
                    pct = round((price - prev) / prev * 100, 2) if prev else 0.0
                    out.append({'symbol': code, 'name': nm, 'market': mk,
                                'price': round(price, 2), 'change_pct': pct,
                                'change_amt': round(price - prev, 2) if prev else 0,
                                'state': 'REGULAR', 'ts': int(time.time())})
                elif code.startswith('gb_'):
                    # 美股: 名称,当前价(idx1),涨跌幅(idx2),时间,涨跌额(idx4)
                    price = float(f[1]); pct = float(f[2]); amt = float(f[4]) if len(f) > 4 else 0
                    out.append({'symbol': code, 'name': nm, 'market': mk,
                                'price': round(price, 2), 'change_pct': round(pct, 2),
                                'change_amt': round(amt, 2), 'state': 'REGULAR', 'ts': int(time.time())})
                else:
                    # int_ 国际指数: 名称,当前价(idx1),涨跌额(idx2),涨跌幅(idx3)
                    price = float(f[1]); amt = float(f[2]); pct = float(f[3])
                    out.append({'symbol': code, 'name': nm, 'market': mk,
                                'price': round(price, 2), 'change_pct': round(pct, 2),
                                'change_amt': round(amt, 2), 'state': 'REGULAR', 'ts': int(time.time())})
            except (ValueError, IndexError):
                continue
    except Exception:
        pass
    # 保持配置顺序
    order = {c: i for i, (c, _, _) in enumerate(GLOBAL_INDEX_MAP)}
    out.sort(key=lambda x: order.get(x['symbol'], 99))
    return out

_global_idx_cache = {'data': None, 'ts': 0}
def _global_index_cache_get(ttl=60):
    """全球大盘指数带缓存(默认60s)"""
    now = time.time()
    if _global_idx_cache['data'] and now - _global_idx_cache['ts'] < ttl:
        return _global_idx_cache['data']
    data = fetch_global_indices()
    if data:
        _global_idx_cache['data'] = data; _global_idx_cache['ts'] = now
        return data
    return _global_idx_cache['data'] or []

def fetch_sectors(kind='concept', limit=10):
    """板块涨幅榜+资金流。kind: concept(概念 t:3) / industry(行业 t:2)，curl拉+重试"""
    t = '3' if kind == 'concept' else '2'
    url = (f'https://push2.eastmoney.com/api/qt/clist/get?pn=1&pz={limit}&po=1&fid=f3'
           f'&fs=m:90+t:{t}&fields=f12,f14,f3,f62,f104,f105')
    for attempt in range(3):
        out = []
        try:
            import subprocess
            _throttle_url(url)
            raw = subprocess.run(['curl', '-s', '--max-time', '10', url],
                                 capture_output=True, timeout=13).stdout.decode('utf-8', 'ignore')
            d = json.loads(raw)
            diff = (d.get('data') or {}).get('diff') or {}
            rows = diff.values() if isinstance(diff, dict) else diff
            for r in rows:
                pct_raw = r.get('f3')
                out.append({
                    'code': r.get('f12'), 'name': r.get('f14'),
                    'change_pct': round(float(pct_raw) / 100.0, 2) if pct_raw not in (None, '-') else 0,
                    'main_inflow': r.get('f62'),
                    'up_count': r.get('f104'), 'down_count': r.get('f105'),
                })
            if out:
                return out
        except Exception:
            pass
        time.sleep(1.2 * (attempt + 1))  # 退避重试
    return out

def _movers_eastmoney(limit):
    """主源: 东财 clist 全市场A股涨幅榜"""
    import subprocess
    # m:0 t:6=深主板 m:0 t:80=创业 m:1 t:2=沪主板 m:1 t:23=科创
    fs = 'm:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23'
    url = ('https://push2.eastmoney.com/api/qt/clist/get?pn=1&pz=%d&po=1&fid=f3'
           '&fs=%s&fields=f12,f14,f2,f3,f8,f62' % (limit, fs))
    _throttle_url(url)
    raw = subprocess.run(['curl', '-s', '--max-time', '10',
                          '-H', 'Referer: https://quote.eastmoney.com/',
                          '-H', 'User-Agent: ' + UA, url],
                         capture_output=True, timeout=13).stdout.decode('utf-8', 'ignore')
    d = json.loads(raw)
    diff = (d.get('data') or {}).get('diff') or {}
    rows = diff.values() if isinstance(diff, dict) else diff
    out = []
    for r in rows:
        code = r.get('f12'); name = r.get('f14')
        if not code or not name:
            continue
        pct = r.get('f3')
        sym = code + ('.SS' if code.startswith('6') else '.SZ')
        out.append({
            'code': code, 'symbol': sym, 'name': name,
            'change_pct': round(float(pct) / 100.0, 2) if pct not in (None, '-') else 0,
            'price': round(float(r.get('f2')) / 100.0, 2) if r.get('f2') not in (None, '-') else None,
            'turnover_rate': round(float(r.get('f8')) / 100.0, 2) if r.get('f8') not in (None, '-') else None,
            'main_inflow': r.get('f62'),
        })
    return out

def _movers_sina(limit):
    """备源1: 新浪 Market_Center 全市场A股涨幅榜(东财push2被封时兜底)"""
    import subprocess
    url = ('https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/'
           'Market_Center.getHQNodeData?page=1&num=%d&sort=changepercent&asc=0&node=hs_a' % limit)
    _throttle_url(url)
    raw = subprocess.run(['curl', '-s', '--max-time', '10',
                          '-H', 'Referer: https://finance.sina.com.cn',
                          '-H', 'User-Agent: ' + UA, url],
                         capture_output=True, timeout=13).stdout.decode('utf-8', 'ignore')
    rows = json.loads(raw)
    out = []
    for r in rows:
        code = r.get('code'); name = r.get('name')
        if not code or not name:
            continue
        # 新浪 symbol 形如 sh600519/sz000001/bj920305; 过滤北交所(bj)避免无K线
        ssym = (r.get('symbol') or '').lower()
        if ssym.startswith('bj'):
            continue
        sym = code + ('.SS' if code.startswith('6') else '.SZ')
        try:
            pct = float(r.get('changepercent'))
        except (TypeError, ValueError):
            pct = 0
        try:
            price = float(r.get('trade'))
        except (TypeError, ValueError):
            price = None
        try:
            tr = float(r.get('turnoverratio'))
        except (TypeError, ValueError):
            tr = None
        out.append({
            'code': code, 'symbol': sym, 'name': name,
            'change_pct': round(pct, 2),
            'price': round(price, 2) if price is not None else None,
            'turnover_rate': round(tr, 2) if tr is not None else None,
            'main_inflow': None,
        })
    return out

def fetch_top_movers_cn(limit=40):
    """全市场A股涨幅榜(选股候选池)。多源自动降级: 东财 → 新浪。
    返回 [{code,symbol,name,change_pct,price,turnover_rate,main_inflow}]"""
    sources = [('eastmoney', _movers_eastmoney), ('sina', _movers_sina)]
    for name, fn in sources:
        for attempt in range(2):
            try:
                out = fn(limit)
                if out:
                    if name != 'eastmoney':
                        print('[movers] 主源东财失败, 已降级至 %s (%d条)' % (name, len(out)))
                    return out
            except Exception as e:
                print('[movers] 源 %s 第%d次异常: %s' % (name, attempt + 1, e))
            time.sleep(1.0 * (attempt + 1))
    return []

def score_pick(symbol, name, hot_pct=None):
    """对单只股技术打分(走稳定K线)。返回 {symbol,name,score,stance,reasons,close,change_pct} 或 None"""
    rows = get_kline_db(symbol, 'daily')
    if not rows or len(rows) < 30:
        fetched = fetch_kline(symbol, 'daily')
        if fetched:
            save_kline(fetched)
            rows = get_kline_db(symbol, 'daily')
    if not rows or len(rows) < 30:
        return None
    klines = [{'date': r['date'], 'open': r['open'], 'high': r['high'],
               'low': r['low'], 'close': r['close'], 'volume': r['volume']} for r in rows]
    ind = compute_indicators(klines)
    if not ind.get('ok'):
        return None
    analysis = judge_bull_bear(ind, klines)
    if not analysis.get('ok'):
        return None
    # 选股评分: 多空分差为主 + 关键信号加权
    score = (analysis.get('bull_score', 0) - analysis.get('bear_score', 0)) * 10
    reasons = list(analysis.get('bull_factors', []))[:5]
    macd = ind.get('macd') or {}
    kdj = ind.get('kdj') or {}
    boll = ind.get('boll') or {}
    vol = ind.get('volume') or {}
    # 加分项(今日可能走强的信号)
    if macd.get('gold_cross'): score += 15
    if kdj.get('signal') == 'gold_cross': score += 8
    if kdj.get('signal') == 'oversold': score += 6
    if boll.get('state') == 'opening': score += 6
    vr5 = vol.get('vol_ratio_5')
    if vr5 and vr5 >= 1.5 and (ind.get('change_pct') or 0) > 0: score += 10
    if hot_pct is not None and hot_pct >= 2: score += 5  # 所属热点加持
    return {
        'symbol': symbol, 'name': name,
        'score': round(score, 1),
        'stance': analysis.get('stance'),
        'strength': analysis.get('strength'),
        'close': ind.get('close'),
        'change_pct': ind.get('change_pct'),
        'reasons': reasons,
        'macd_gold': bool(macd.get('gold_cross')),
        'kdj_signal': kdj.get('signal'),
    }

_picks_cache = {'data': None, 'ts': 0}
def compute_daily_picks(top_n=8, scan=30):
    """每日选股: 扫涨幅榜候选池 -> 逐只技术打分 -> 取Top N。
    东财涨幅榜限流时降级用自选A股作候选池"""
    cands = fetch_top_movers_cn(limit=scan)
    source = 'hot'
    if not cands:
        # 降级: 用自选A股
        with db() as c:
            rs = c.execute("SELECT symbol,name FROM watchlist WHERE market='cn' ORDER BY sort_order").fetchall()
        cands = [{'symbol': r['symbol'], 'name': r['name'], 'change_pct': None} for r in rs]
        source = 'watchlist'
    picks = []
    for cd in cands:
        try:
            p = score_pick(cd['symbol'], cd['name'], hot_pct=cd.get('change_pct'))
            if p:
                picks.append(p)
        except Exception:
            continue
    picks.sort(key=lambda x: -x['score'])
    return {'source': source, 'scanned': len(cands), 'picks': picks[:top_n], 'date': time.strftime('%Y-%m-%d')}

_idx_cache = {'data': None, 'ts': 0}
_cyq_cache = {}  # symbol -> {data, ts}  C2 筹码缓存
_sector_cache = {}  # kind -> {data, ts}  板块缓存
def _index_cache_get(ttl=30):
    """大盘四大指数带缓存(默认30s)，避免 analyze 频繁拉新浪"""
    now = time.time()
    if _idx_cache['data'] is not None and now - _idx_cache['ts'] < ttl:
        return _idx_cache['data']
    idx = fetch_indices()
    if idx:
        _idx_cache['data'] = idx; _idx_cache['ts'] = now
    return idx or (_idx_cache['data'] or [])

def _sector_cache_get(kind='concept', ttl=60):
    """板块热点带缓存(默认60s)，避免大屏轮询频繁打东财。空结果也短缓存，避免休市时反复7秒空重试"""
    now = time.time()
    c = _sector_cache.get(kind)
    if c and now - c['ts'] < ttl:
        return c['data']
    data = fetch_sectors(kind=kind, limit=12)
    if data:
        _sector_cache[kind] = {'data': data, 'ts': now}
        return data
    # 空结果也缓存(旧值或空列表)，避免下次又花7秒重试空拉
    _sector_cache[kind] = {'data': (c['data'] if c else []), 'ts': now}
    return _sector_cache[kind]['data']

def classify_index_trend(idx):
    """大盘定性：由四大指数当日涨跌粗判"""
    if not idx:
        return 'unknown'
    avg = sum(i['change_pct'] for i in idx) / len(idx)
    if avg >= 1.5:
        return '强势上涨'
    if avg >= 0.3:
        return '温和上涨'
    if avg > -0.3:
        return '震荡整理'
    if avg > -1.5:
        return '较弱下跌'
    return '大幅下跌'

# ========== 多空分歧判断框架（里程B3 / 纯Python无依赖）==========
def judge_bull_bear(ind, klines, index_env=None, sector_env=None):
    """输入 B1指标(compute_indicators结果) + 原始K线(算影线/位置) → 输出结构化多空判断。
    index_env: fetch_indices() 后 classify_index_trend 的定性字符串(可选)。
    sector_env: 该股所属板块的涨幅/资金流字典(可选)，形如 {'name','change_pct','main_inflow'}。
    """
    if not ind or not ind.get('ok') or not klines:
        return {'ok': False, 'error': 'insufficient data'}

    last = klines[-1]
    o, h, l, c = last['open'], last['high'], last['low'], last['close']
    change_pct = ind.get('change_pct', 0) or 0
    ma = ind.get('ma') or {}
    macd = ind.get('macd') or {}
    kdj = ind.get('kdj') or {}
    rsi = ind.get('rsi')
    vol = ind.get('volume') or {}
    hl = ind.get('high_low') or {}

    bull, bear = [], []          # 多头/空头信号说明
    bull_score = bear_score = 0  # 量化力量

    # 1) 当日涨跌幅
    if change_pct >= 5:
        bull_score += 2; bull.append('大涨%.2f%%，多头强势' % change_pct)
    elif change_pct >= 1:
        bull_score += 1; bull.append('上涨%.2f%%' % change_pct)
    elif change_pct <= -5:
        bear_score += 2; bear.append('大跌%.2f%%，空头占优' % change_pct)
    elif change_pct <= -1:
        bear_score += 1; bear.append('下跌%.2f%%' % change_pct)

    # 2) K线形态：实体与上下影线
    rng = (h - l) or 1e-9
    body = c - o
    upper = h - max(o, c)   # 上影线
    lower = min(o, c) - l   # 下影线
    body_ratio = abs(body) / rng
    if body > 0 and body_ratio >= 0.6:
        bull_score += 1; bull.append('大阳线实体饱满(实体占比%.0f%%)' % (body_ratio * 100))
    elif body < 0 and body_ratio >= 0.6:
        bear_score += 1; bear.append('大阴线实体饱满(实体占比%.0f%%)' % (body_ratio * 100))
    if lower / rng >= 0.35 and lower > abs(body):
        bull_score += 1; bull.append('长下影线，下方承接有力')
    if upper / rng >= 0.35 and upper > abs(body):
        bear_score += 1; bear.append('长上影线，上方抛压明显')

    # 3) 均线位置
    ma20 = ma.get('MA20'); ma60 = ma.get('MA60')
    if ma20:
        if c > ma20:
            bull_score += 1; bull.append('站上20日线(%.2f)' % ma20)
        else:
            bear_score += 1; bear.append('跌破20日线(%.2f)' % ma20)
    if ma20 and ma60 and ma20 > ma60:
        bull_score += 1; bull.append('20日线上穿60日线，中期多头排列')
    elif ma20 and ma60 and ma20 < ma60:
        bear_score += 1; bear.append('20日线位于60日线下方，中期偏空')

    # 4) MACD
    if macd.get('gold_cross'):
        bull_score += 2; bull.append('MACD金叉')
    elif macd.get('dead_cross'):
        bear_score += 2; bear.append('MACD死叉')
    elif macd.get('trend') == 'up':
        bull_score += 1; bull.append('MACD红柱走强')
    elif macd.get('trend') == 'down':
        bear_score += 1; bear.append('MACD柱体走弱')

    # 5) KDJ
    ksig = kdj.get('signal')
    if ksig == 'gold_cross':
        bull_score += 1; bull.append('KDJ金叉')
    elif ksig == 'dead_cross':
        bear_score += 1; bear.append('KDJ死叉')
    elif ksig == 'overbought':
        bear_score += 1; bear.append('KDJ超买(J=%.0f)，短线过热' % (kdj.get('J') or 0))
    elif ksig == 'oversold':
        bull_score += 1; bull.append('KDJ超卖(J=%.0f)，短线或反弹' % (kdj.get('J') or 0))

    # 6) RSI
    if rsi is not None:
        if rsi >= 80:
            bear_score += 1; bear.append('RSI=%.0f 超买' % rsi)
        elif rsi >= 55:
            bull_score += 1; bull.append('RSI=%.0f 偏强' % rsi)
        elif rsi <= 20:
            bull_score += 1; bull.append('RSI=%.0f 超卖，或有反弹' % rsi)
        elif rsi <= 45:
            bear_score += 1; bear.append('RSI=%.0f 偏弱' % rsi)

    # 6.5) BOLL布林带
    boll = ind.get('boll') or {}
    if boll.get('upper') is not None:
        bpos = boll.get('pos')
        if c > boll['upper']:
            bull_score += 1; bull.append('突破布林上轨(%.2f)，强势' % boll['upper'])
        elif c < boll['lower']:
            bull_score += 1; bull.append('跌破布林下轨(%.2f)，超跌或反弹' % boll['lower'])
        elif bpos is not None and bpos >= 0.8:
            bear.append('贴近布林上轨，短线较高')
        elif bpos is not None and bpos <= 0.2:
            bull.append('贴近布林下轨，下方有支撑')
        if boll.get('state') == 'opening':
            bull.append('布林带开口(带宽%.1f%%)，波动放大' % (boll.get('bandwidth') or 0))
        elif boll.get('state') == 'closing':
            bull.append('布林带收口，选方向临近')

    # 7) 量能
    vr5 = vol.get('vol_ratio_5')
    if vr5:
        if vr5 >= 1.5 and change_pct > 0:
            bull_score += 1; bull.append('放量上涨(量比5日=%.2f)，资金进场' % vr5)
        elif vr5 >= 1.5 and change_pct < 0:
            bear_score += 1; bear.append('放量下跌(量比5日=%.2f)，抛压释放' % vr5)
        elif vr5 <= 0.7 and change_pct > 0:
            bear.append('缩量上涨(量比5日=%.2f)，上攻动能存疑' % vr5)

    # 8) 突破/跌破近期高低点
    hi20 = hl.get('high_20'); lo20 = hl.get('low_20')
    if hi20 and c >= hi20:
        bull_score += 2; bull.append('创近20日新高，突破有效')
    if lo20 and c <= lo20:
        bear_score += 2; bear.append('创近20日新低，跌破支撑')

    # 9) 大盘环境加成
    if index_env:
        if index_env in ('强势上涨', '温和上涨'):
            bull_score += 1; bull.append('大盘%s，环境偏暖' % index_env)
        elif index_env in ('较弱下跌', '大幅下跌'):
            bear_score += 1; bear.append('大盘%s，环境承压' % index_env)

    # 10) 所属板块强弱
    if sector_env and sector_env.get('change_pct') is not None:
        spct = sector_env['change_pct']
        sname = sector_env.get('name', '所属板块')
        if spct >= 2:
            bull_score += 1; bull.append('%s板块大涨%.2f%%，热点助攻' % (sname, spct))
        elif spct <= -2:
            bear_score += 1; bear.append('%s板块走弱%.2f%%，拖累明显' % (sname, spct))

    # ===== 综合判定 =====
    total = bull_score + bear_score
    diff = bull_score - bear_score
    if total == 0:
        stance = '多空平衡'; strength = '弱'
    elif diff >= 4:
        stance = '多头主导'; strength = '强'
    elif diff >= 2:
        stance = '偏多'; strength = '中'
    elif diff <= -4:
        stance = '空头主导'; strength = '强'
    elif diff <= -2:
        stance = '偏空'; strength = '中'
    else:
        stance = '多空分歧'; strength = '中' if total >= 4 else '弱'

    # 分歧度：双方都有相当力量 → 分歧大
    divergence = round(min(bull_score, bear_score) / (total or 1), 2)
    if divergence >= 0.4:
        divergence_desc = '分歧较大，方向不明'
    elif divergence >= 0.2:
        divergence_desc = '存在分歧'
    else:
        divergence_desc = '方向较一致'

    # 次日验证信号：给出可观察的确认/证伪价位
    signals = []
    if stance in ('多头主导', '偏多'):
        if hi20:
            signals.append('次日站稳%.2f(近20日高)并放量则确认上攻' % hi20)
        if ma20:
            signals.append('若回落跌破MA20(%.2f)则多头减弱' % ma20)
    elif stance in ('空头主导', '偏空'):
        if lo20:
            signals.append('次日跌破%.2f(近20日低)则下跌加速' % lo20)
        if ma20:
            signals.append('若反弹收复MA20(%.2f)则空头趋缓' % ma20)
    else:
        if ma20:
            signals.append('关注MA20(%.2f)争夺：上破偏多、下破偏空' % ma20)

    return {
        'ok': True,
        'stance': stance,           # 多头主导/偏多/多空分歧/偏空/空头主导/多空平衡
        'strength': strength,       # 强/中/弱
        'bull_score': bull_score,
        'bear_score': bear_score,
        'divergence': divergence,
        'divergence_desc': divergence_desc,
        'bull_factors': bull,
        'bear_factors': bear,
        'verify_signals': signals,
    }

def generate_narrative(name, ind, analysis, index_env=None):
    """B5-A: 将 B3 结构化判断拼成一段流畅中文点评（纯模板，零依赖）"""
    if not analysis or not analysis.get('ok') or not ind or not ind.get('ok'):
        return ''
    cp = ind.get('close'); pct = ind.get('change_pct', 0) or 0
    stance = analysis.get('stance', '')
    strength = analysis.get('strength', '')
    bs = analysis.get('bull_score', 0); rs = analysis.get('bear_score', 0)
    bull = analysis.get('bull_factors') or []
    bear = analysis.get('bear_factors') or []
    signals = analysis.get('verify_signals') or []

    # 开头：名称 + 涨跌 + 收价
    move = '上涨' if pct > 0 else ('下跌' if pct < 0 else '平收')
    seg = ['%s今日%s%.2f%%，收于%s。' % (name, move, abs(pct), cp)]

    # 取最多3个因子(去掉开头涨跌重复、以及大盘环境因子避免末尾重复)
    def _trim(fac):
        out = []
        for f in fac:
            if f.startswith('上涨') or f.startswith('下跌') or f.startswith('大涨') or f.startswith('大跌'):
                continue
            if f.startswith('大盘'):
                continue
            out.append(f)
        return out
    bull_r = _trim(bull)[:3]
    bear_r = _trim(bear)[:3]

    if stance in ('多头主导', '偏多'):
        lead = '多头%s占优(%d:%d)' % ('强势' if strength == '强' else '暂', bs, rs)
        if bull_r:
            seg.append('%s，%s。' % (lead, '；'.join(bull_r)))
        else:
            seg.append('%s。' % lead)
        if bear_r:
            seg.append('但%s，上方仍有制约。' % '；'.join(bear_r))
    elif stance in ('空头主导', '偏空'):
        lead = '空头%s占优(%d:%d)' % ('强势' if strength == '强' else '暂', rs, bs)
        if bear_r:
            seg.append('%s，%s。' % (lead, '；'.join(bear_r)))
        else:
            seg.append('%s。' % lead)
        if bull_r:
            seg.append('不过%s，下方尚有支撑。' % '；'.join(bull_r))
    else:
        seg.append('多空分歧明显(多%d空%d)。' % (bs, rs))
        if bull_r:
            seg.append('多方：%s。' % '；'.join(bull_r))
        if bear_r:
            seg.append('空方：%s。' % '；'.join(bear_r))

    # 大盘环境
    if index_env and index_env not in ('unknown', '震荡整理'):
        seg.append('大盘%s，需兼顾环境。' % index_env)

    # 次日验证
    if signals:
        seg.append('后市：%s。' % '；'.join(s.rstrip('。') for s in signals[:2]))

    return ''.join(seg)

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
            if path == '/api/finance/analyze':
                return self._api_analyze(q)
            if path == '/api/finance/cyq':
                return self._api_cyq(q)
            if path == '/api/finance/indices':
                return self._api_indices(q)
            if path == '/api/finance/global':
                return self._api_global(q)
            if path == '/api/finance/sectors':
                return self._api_sectors(q)
            if path == '/api/finance/picks':
                return self._api_picks(q)
            if path == '/' :
                self.path = '/public/finance.html'
        except Exception as e:
            return self._json(500, {'ok': False, 'error': str(e)})
        return super().do_GET()

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        try:
            length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(length) if length else b''
            data = json.loads(body.decode('utf-8')) if body else {}
        except Exception:
            data = {}
        try:
            if path == '/api/finance/watchlist/add':
                return self._api_watchlist_add(data)
            if path == '/api/finance/watchlist/remove':
                return self._api_watchlist_remove(data)
            if path == '/api/finance/watchlist/reorder':
                return self._api_watchlist_reorder(data)
        except Exception as e:
            return self._json(500, {'ok': False, 'error': str(e)})
        return self._json(404, {'ok': False, 'error': 'not found'})

    def _api_watchlist_add(self, data):
        """添加自选股。symbol 必填，自动补后缀后拉一次行情验证可用并拿名称"""
        symbol = _normalize_symbol(data.get('symbol') or '')
        if not symbol:
            return self._json(400, {'ok': False, 'error': 'symbol required'})
        with db() as c:
            exist = c.execute('SELECT 1 FROM watchlist WHERE symbol=?', (symbol,)).fetchone()
        if exist:
            return self._json(200, {'ok': False, 'error': '已在自选中', 'symbol': symbol})
        # 拉一次行情验证 symbol 有效
        q = fetch_quotes([symbol], fallback_db=False)
        if symbol not in q or q[symbol].get('price') is None:
            return self._json(200, {'ok': False, 'error': '无法获取行情，请检查代码（如 600519.SS / 0700.HK / AAPL / BTC-USD）', 'symbol': symbol})
        name = data.get('name') or q[symbol].get('name') or symbol
        market = q[symbol].get('market') or _guess_market(symbol)
        with _db_lock, db() as c:
            mx = c.execute('SELECT COALESCE(MAX(sort_order),0) FROM watchlist').fetchone()[0]
            c.execute('INSERT OR IGNORE INTO watchlist(symbol,name,market,sort_order,added_at) VALUES(?,?,?,?,?)',
                      (symbol, name, market, mx + 1, int(time.time())))
        save_quotes(q)
        return self._json(200, {'ok': True, 'symbol': symbol, 'name': name, 'market': market})

    def _api_watchlist_remove(self, data):
        """删除自选股"""
        symbol = (data.get('symbol') or '').strip().upper()
        if not symbol:
            return self._json(400, {'ok': False, 'error': 'symbol required'})
        with _db_lock, db() as c:
            c.execute('DELETE FROM watchlist WHERE symbol=?', (symbol,))
        return self._json(200, {'ok': True, 'symbol': symbol})

    def _api_watchlist_reorder(self, data):
        """按前端传来的 symbols 顺序重排 sort_order"""
        order = data.get('order') or []
        if not isinstance(order, list) or not order:
            return self._json(400, {'ok': False, 'error': 'order required'})
        with _db_lock, db() as c:
            for i, sym in enumerate(order):
                c.execute('UPDATE watchlist SET sort_order=? WHERE symbol=?', (i, str(sym).strip().upper()))
        return self._json(200, {'ok': True, 'count': len(order)})

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

    def _api_indices(self, q):
        """大盘四大指数实时（带缓存）"""
        idx = _index_cache_get()
        return self._json(200, {'ok': True, 'items': idx, 'trend': classify_index_trend(idx),
                                'ts': int(time.time())})

    def _api_global(self, q):
        """全球大盘指数（美股/夜盘期货/恒生/日经/欧股/加密，带缓存）"""
        data = _global_index_cache_get()
        return self._json(200, {'ok': True, 'items': data, 'ts': int(time.time())})

    def _api_sectors(self, q):
        """板块热点涨幅榜。kind: concept(概念) / industry(行业)，带缓存"""
        kind = (q.get('kind') or ['concept'])[0]
        if kind not in ('concept', 'industry'):
            kind = 'concept'
        data = _sector_cache_get(kind=kind)
        return self._json(200, {'ok': True, 'kind': kind, 'items': data, 'ts': int(time.time())})

    def _api_picks(self, q):
        """每日热点选股: 扫涨幅榜技术打分 Top N，带 5分钟缓存(扫描耗时)"""
        now = time.time()
        force = (q.get('force') or ['0'])[0] == '1'
        if not force and _picks_cache['data'] and now - _picks_cache['ts'] < 300:
            d = dict(_picks_cache['data']); d['cached'] = True
            return self._json(200, {'ok': True, **d, 'ts': int(now)})
        try:
            top_n = min(int((q.get('top') or ['8'])[0]), 20)
        except ValueError:
            top_n = 8
        result = compute_daily_picks(top_n=top_n)
        _picks_cache['data'] = result; _picks_cache['ts'] = now
        return self._json(200, {'ok': True, **result, 'cached': False, 'ts': int(now)})

    def _api_analyze(self, q):
        """B4: 聚合 B1指标 + B2大盘/板块 + B3多空判断，返回结构化技术面解读"""
        symbol = (q.get('symbol') or [''])[0]
        if not symbol:
            return self._json(400, {'ok': False, 'error': 'symbol required'})
        period = (q.get('period') or ['daily'])[0]
        # K线（优先DB，缺则拉取）
        rows = get_kline_db(symbol, period)
        if not rows or len(rows) < 30:
            save_kline(fetch_kline(symbol, period))
            rows = get_kline_db(symbol, period)
        ind = compute_indicators(rows)
        if not ind.get('ok'):
            return self._json(200, {'ok': False, 'error': 'insufficient kline', 'symbol': symbol})
        # 股票名称/市场
        name = symbol
        with db() as c:
            r = c.execute('SELECT name,market FROM watchlist WHERE symbol=?', (symbol,)).fetchone()
            if r:
                name = r['name'] or symbol
        # 大盘环境（带8秒缓存）
        idx = _index_cache_get()
        index_trend = classify_index_trend(idx)
        # 板块（仅A股参考概念涨幅榜首位作为环境，不做个股精确映射）
        sector_env = None
        analysis = judge_bull_bear(ind, rows, index_env=index_trend, sector_env=sector_env)
        narrative = generate_narrative(name, ind, analysis, index_env=index_trend)
        return self._json(200, {
            'ok': True, 'symbol': symbol, 'name': name, 'period': period,
            'indicators': ind,
            'market_env': {'trend': index_trend, 'indices': idx},
            'analysis': analysis,
            'narrative': narrative,
            'ts': int(time.time()),
        })

    def _api_cyq(self, q):
        """C2: 筹码分布。subprocess 调独立 .cyqenv 跑 cyq_report.py（akshare 重依赖隔离），带 60s 缓存。"""
        symbol = (q.get('symbol') or [''])[0]
        if not symbol:
            return self._json(400, {'ok': False, 'error': 'symbol required'})
        now = time.time()
        cached = _cyq_cache.get(symbol)
        # 筹码数据一天只变一次(收盘后), 同一天缓存到次日凌晨; 持续命中秒回
        if cached and time.strftime('%Y-%m-%d', time.localtime(cached['ts'])) == time.strftime('%Y-%m-%d', time.localtime(now)):
            return self._json(200, cached['data'])
        py = os.path.join(BASE_DIR, '.cyqenv', 'bin', 'python')
        script = os.path.join(BASE_DIR, 'scripts', 'cyq_report.py')
        if not os.path.exists(py):
            return self._json(200, {'ok': False, 'error': 'cyq env missing (未建 .cyqenv)', 'symbol': symbol})
        import subprocess
        # requests 会继承 macOS 系统代理导致连东财失败，强制直连
        env = dict(os.environ)
        env.update({'no_proxy': '*', 'NO_PROXY': '*', 'HTTP_PROXY': '', 'HTTPS_PROXY': '', 'ALL_PROXY': ''})
        try:
            out = subprocess.run([py, script, symbol], capture_output=True, timeout=30, env=env)
            raw = out.stdout.decode('utf-8', 'ignore').strip()
            # 取最后一行 JSON（避免 warning 污染）
            line = ''
            for ln in raw.splitlines():
                ln = ln.strip()
                if ln.startswith('{') and ln.endswith('}'):
                    line = ln
            if not line:
                err = out.stderr.decode('utf-8', 'ignore')[-300:] or 'no output'
                return self._json(200, {'ok': False, 'error': 'cyq subprocess: ' + err, 'symbol': symbol})
            data = json.loads(line)
            data['ts'] = int(now)
            if data.get('ok'):
                _cyq_cache[symbol] = {'data': data, 'ts': now}
            return self._json(200, data)
        except subprocess.TimeoutExpired:
            return self._json(200, {'ok': False, 'error': 'cyq timeout (30s)', 'symbol': symbol})
        except Exception as e:
            return self._json(200, {'ok': False, 'error': 'cyq error: %s' % e, 'symbol': symbol})

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

        # 全球大盘指数预热线程（独立节奏，错开自选刷新防限流，让前端首次秒开）
        def bg_global():
            time.sleep(5)  # 先让自选首轮跑完，错开
            kline_warm_ts = 0
            while True:
                try:
                    _global_index_cache_get(ttl=55)
                except Exception:
                    pass
                # 板块热点预热（概念+行业，让前端秒开，7.7s->秒回）
                try:
                    _sector_cache_get('concept', ttl=55)
                    _sector_cache_get('industry', ttl=55)
                except Exception:
                    pass
                # 自选股K线预热(每10分钟一轮, 让点开K线秒出, 3.6s->秒回)
                try:
                    if time.time() - kline_warm_ts > 600:
                        with db() as c:
                            wsyms = [r['symbol'] for r in c.execute('SELECT symbol FROM watchlist ORDER BY sort_order')]
                        for s in wsyms:
                            for period in ('daily', 'weekly', 'monthly'):
                                try:
                                    rows = fetch_kline(s, period)
                                    if rows:
                                        save_kline(rows)
                                except Exception:
                                    pass
                        kline_warm_ts = time.time()
                except Exception:
                    pass
                time.sleep(55)
        tg = threading.Thread(target=bg_global, daemon=True)
        tg.start()
        srv = ThreadingHTTPServer((HOST, PORT), Handler)
        print(f'FinSight serving at http://{HOST}:{PORT}')
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            srv.shutdown()
