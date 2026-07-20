#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""自选股异动哨兵 —— 定期捞 alerts 库新记录，去重后推微信。
后台 serve.py 已实时检测并写入 alerts 表，本脚本只负责"新增->推送"。
只推自选股 + 关注市场的异动；用状态文件记录已推送的最大 alert id 防重复。"""
import os, json, subprocess, urllib.request

API_BASE = os.environ.get('API_BASE', 'http://127.0.0.1:8770')
OPENCLAW_BIN = os.environ.get('OPENCLAW_BIN', '/opt/homebrew/bin/openclaw')
WX_TARGET = os.environ.get('WX_TARGET', 'o9cq80zOXMiSDvE8M5PAioQH4PjM@im.wechat')
STATE_FILE = os.environ.get('ALERT_STATE', os.path.expanduser('~/finance-dashboard/data/alert_push_state.json'))
# 最低推送阈值：单窗涨跌幅绝对值(%)，过滤噪音
MIN_PCT = float(os.environ.get('ALERT_MIN_PCT', '0.8'))
MAX_PER_RUN = int(os.environ.get('ALERT_MAX_PER_RUN', '8'))

def http_get(url, timeout=30):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode('utf-8'))

def wx_push(msg):
    subprocess.run([OPENCLAW_BIN, 'message', 'send', '--channel', 'wechat',
                    '--target', WX_TARGET, '--message', msg],
                   check=False, timeout=180)

def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {'last_id': 0}

def save_state(st):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, 'w') as f:
        json.dump(st, f)

def fmt_alert(a):
    arrow = '📈' if a.get('direction') == 'up' else '📉'
    pct = a.get('pct', 0)
    sign = '+' if pct >= 0 else ''
    today = a.get('pct_today')
    today_s = f' · 今日{"+" if (today or 0)>=0 else ""}{today}%' if today is not None else ''
    return (f'{arrow} {a.get("name","")} {a.get("symbol","")}\n'
            f'   {a.get("window","")}内 {sign}{pct}% · 现价{a.get("price","")}{today_s}')

def main():
    st = load_state()
    last_id = int(st.get('last_id', 0))
    try:
        data = http_get(f'{API_BASE}/api/finance/alerts?limit=50')
    except Exception:
        return  # 静默，接口偶发失败不打扰
    items = data.get('items', [])
    # 只要 id 比上次大、且幅度达阈值
    fresh = [a for a in items
             if int(a.get('id', 0)) > last_id and abs(a.get('pct', 0)) >= MIN_PCT]
    if not fresh:
        return
    fresh.sort(key=lambda a: int(a.get('id', 0)))  # 时间正序
    max_id = max(int(a['id']) for a in fresh)
    to_push = fresh[-MAX_PER_RUN:]  # 太多只推最新的几条
    header = f'🚨 自选股异动提醒（{len(fresh)}条）' if len(fresh) > 1 else '🚨 自选股异动提醒'
    body = [header, ''] + [fmt_alert(a) for a in to_push]
    if len(fresh) > len(to_push):
        body.append(f'\n…另有{len(fresh)-len(to_push)}条较早异动已略。')
    wx_push('\n'.join(body))
    st['last_id'] = max_id
    save_state(st)

if __name__ == '__main__':
    main()
