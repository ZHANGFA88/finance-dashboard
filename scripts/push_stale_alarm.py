#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""数据陈旧哨兵 —— 检测行情源是否"该更新却没更新"，异常时推微信。
关键: 按市场交易时段判断，休市不报警(美股半夜不动是正常的)。
只在交易时段内、数据 age 超阈值时告警；用状态文件防重复刷屏。"""
import os, json, time, subprocess, urllib.request
from datetime import datetime

API_BASE = os.environ.get('API_BASE', 'http://127.0.0.1:8770')
OPENCLAW_BIN = os.environ.get('OPENCLAW_BIN', '/opt/homebrew/bin/openclaw')
WX_TARGET = os.environ.get('WX_TARGET', 'o9cq80zOXMiSDvE8M5PAioQH4PjM@im.wechat')
STATE_FILE = os.environ.get('STALE_STATE', os.path.expanduser('~/finance-dashboard/data/stale_alarm_state.json'))
# 交易时段内数据超过多少秒没更新算陈旧
STALE_SEC = int(os.environ.get('STALE_SEC', '600'))       # 10分钟
# 同一告警冷却，防刷屏
COOLDOWN_SEC = int(os.environ.get('STALE_COOLDOWN', '3600'))  # 1小时
# 监控的自选股(可留空=自动拉watchlist)
SYMBOLS = os.environ.get('STALE_SYMBOLS', '')

def http_get(url, timeout=30):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode('utf-8'))

def wx_push(msg):
    subprocess.run([OPENCLAW_BIN, 'message', 'send', '--channel', 'wechat',
                    '--target', WX_TARGET, '--message', msg], check=False, timeout=180)

def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}

def save_state(st):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, 'w') as f:
        json.dump(st, f)

def market_of(sym):
    if sym.endswith('.SS') or sym.endswith('.SZ'):
        return 'cn'
    if sym.endswith('.HK'):
        return 'hk'
    if sym.endswith('-USD'):
        return 'crypto'
    if sym.endswith('=X'):
        return 'fx'
    return 'us'

def is_trading(market, now):
    """粗略判断该市场此刻是否交易时段(北京时间)。加密/外汇7x24。"""
    wd = now.weekday()  # 0=周一
    h, m = now.hour, now.minute
    hm = h * 60 + m
    if market in ('crypto', 'fx'):
        return True  # 7x24(外汇周末休，但容忍)
    if market == 'cn':
        if wd >= 5: return False
        return (9*60+30 <= hm <= 11*60+30) or (13*60 <= hm <= 15*60)
    if market == 'hk':
        if wd >= 5: return False
        return (9*60+30 <= hm <= 12*60) or (13*60 <= hm <= 16*60)
    if market == 'us':
        # 美东9:30-16:00 → 北京时间约21:30-次日04:00(夏令时)
        if hm >= 21*60+30 or hm <= 4*60:
            # 周末判断(按美东): 简化为北京周六早晨前、周一晚后
            return not (wd == 5 and hm <= 4*60) and not (wd == 6)
        return False
    return False

def main():
    now = datetime.now()
    st = load_state()
    # 拉自选股
    if SYMBOLS:
        syms = [s.strip() for s in SYMBOLS.split(',') if s.strip()]
    else:
        try:
            wl = http_get(f'{API_BASE}/api/finance/watchlist')
            syms = [it['symbol'] for it in wl.get('items', [])]
        except Exception:
            return
    if not syms:
        return
    try:
        data = http_get(f'{API_BASE}/api/finance/quotes?symbols=' + ','.join(syms))
    except Exception:
        return
    now_ts = int(time.time())
    stale_list = []
    for it in data.get('items', []):
        sym = it.get('symbol', '')
        mkt = market_of(sym)
        # 只盯 A股/港股/美股(有多活源, 真被封才值得报警);
        # 外汇/加密只有Yahoo单源, 长期陈旧属预期, 不纳入告警避免噪音
        if mkt in ('fx', 'crypto'):
            continue
        if not is_trading(mkt, now):
            continue  # 休市不算陈旧
        age = now_ts - it.get('ts', now_ts)
        if it.get('stale') or age > STALE_SEC:
            stale_list.append((sym, it.get('name', ''), mkt, age))
    if not stale_list:
        return
    # 冷却: 距上次告警不足COOLDOWN则跳过
    last_ts = st.get('last_alarm_ts', 0)
    if now_ts - last_ts < COOLDOWN_SEC:
        return
    lines = ['⚠️ 行情源异常提醒', '', '以下自选股在交易时段内数据长时间未更新，源可能被封或故障：', '']
    for sym, name, mkt, age in stale_list[:10]:
        lines.append(f'• {name} {sym}（{mkt}）已 {age//60} 分钟未更新')
    lines.append('')
    lines.append('建议：检查 serve.py 日志 / 三源(腾讯·新浪·Yahoo)是否被限流。')
    wx_push('\n'.join(lines))
    st['last_alarm_ts'] = now_ts
    save_state(st)

if __name__ == '__main__':
    main()
