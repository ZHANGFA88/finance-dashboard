#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""每日早间选股推送 —— 拉 /api/finance/picks，格式化成榜单推微信。"""
import os, json, time, subprocess, urllib.request

API_BASE = os.environ.get('API_BASE', 'http://127.0.0.1:8770')
OPENCLAW_BIN = os.environ.get('OPENCLAW_BIN', '/opt/homebrew/bin/openclaw')
WX_TARGET = os.environ.get('WX_TARGET', 'o9cq80zOXMiSDvE8M5PAioQH4PjM@im.wechat')
TOP_N = int(os.environ.get('PICKS_TOP', '6'))

def http_get(url, timeout=30):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode('utf-8'))

def wx_push(msg):
    subprocess.run([OPENCLAW_BIN, 'message', 'send', '--channel', 'wechat',
                    '--target', WX_TARGET, '--message', msg],
                   check=False, timeout=180)

def main():
    date = time.strftime('%Y-%m-%d')
    try:
        data = http_get(f'{API_BASE}/api/finance/picks?top={TOP_N}')
    except Exception as e:
        wx_push(f'⚠️ 早间选股推送失败：接口异常 {e}')
        return
    picks = data.get('picks', [])
    if not picks:
        wx_push(f'📊 {date} 早间选股\n\n今日候选池暂无满足条件的强势标的，空仓观望。')
        return
    src = {'hot': '全市场涨幅榜', 'watchlist': '自选池'}.get(data.get('source'), data.get('source', ''))
    lines = [f'📊 {date} 早间选股推荐', f'（扫描{data.get("scanned","")}只·来源:{src}）', '']
    medals = ['🥇', '🥈', '🥉', '4️⃣', '5️⃣', '6️⃣', '7️⃣', '8️⃣']
    for i, p in enumerate(picks):
        m = medals[i] if i < len(medals) else f'{i+1}.'
        chg = p.get('change_pct', 0)
        sign = '+' if chg >= 0 else ''
        lines.append(f'{m} {p.get("name","")} {p.get("symbol","")}')
        lines.append(f'   现价{p.get("close","")} {sign}{chg}% · {p.get("stance","")}·{p.get("strength","")}强度 · 评分{p.get("score","")}')
        reasons = p.get('reasons', [])[:3]
        if reasons:
            lines.append('   ▸ ' + '；'.join(reasons))
        lines.append('')
    lines.append('⚠️ 仅供参考，非投资建议，注意风险。')
    wx_push('\n'.join(lines))

if __name__ == '__main__':
    main()
