#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""筹码分布分析（里程碑C，独立环境运行，依赖 akshare）。
用法: cyq_report.py <symbol>    symbol 可为 600206 / 600519.SS / 000001.SZ
输出: 一行 JSON，含最新筹码指标 + 近30日成本区间序列（供前端画图）。
主后端通过 subprocess 调用本脚本，绝不把 akshare 引入主服务。
"""
import sys
import json
import re


def norm_code(symbol):
    """归一化到6位纯数字代码。600519.SS -> 600519, 000001.SZ -> 000001"""
    s = str(symbol).strip().upper()
    m = re.search(r'(\d{6})', s)
    return m.group(1) if m else s


def main():
    if len(sys.argv) < 2:
        print(json.dumps({'ok': False, 'error': 'symbol required'}, ensure_ascii=False))
        return
    code = norm_code(sys.argv[1])
    try:
        import akshare as ak
    except Exception as e:
        print(json.dumps({'ok': False, 'error': 'akshare import failed: %s' % e}, ensure_ascii=False))
        return
    try:
        df = ak.stock_cyq_em(symbol=code, adjust="")
    except Exception as e:
        print(json.dumps({'ok': False, 'error': 'stock_cyq_em failed: %s' % e, 'code': code}, ensure_ascii=False))
        return
    if df is None or df.empty:
        print(json.dumps({'ok': False, 'error': 'empty data', 'code': code}, ensure_ascii=False))
        return

    # 列名（东财接口）：日期 获利比例 平均成本 90成本-低 90成本-高 90集中度 70成本-低 70成本-高 70集中度
    cols = list(df.columns)
    last = df.iloc[-1]

    def g(name):
        return last[name] if name in cols else None

    def fnum(v, nd=3):
        try:
            return round(float(v), nd)
        except Exception:
            return None

    profit_ratio = fnum(g('获利比例'), 4)
    avg_cost = fnum(g('平均成本'))
    result = {
        'ok': True,
        'code': code,
        'date': str(g('日期')),
        'profit_ratio': profit_ratio,                       # 获利盘比例 0~1
        'profit_ratio_pct': round(profit_ratio * 100, 2) if profit_ratio is not None else None,
        'avg_cost': avg_cost,                               # 平均成本
        'cost_90_low': fnum(g('90成本-低')),
        'cost_90_high': fnum(g('90成本-高')),
        'concentration_90': fnum(g('90集中度'), 4),          # 90%筹码集中度(越小越集中)
        'cost_70_low': fnum(g('70成本-低')),
        'cost_70_high': fnum(g('70成本-高')),
        'concentration_70': fnum(g('70集中度'), 4),
    }

    # 近30日序列（供前端画成本区间带状图）
    tail = df.tail(30)
    series = []
    for _, row in tail.iterrows():
        series.append({
            'date': str(row['日期']) if '日期' in cols else None,
            'avg_cost': fnum(row['平均成本']) if '平均成本' in cols else None,
            'low_90': fnum(row['90成本-低']) if '90成本-低' in cols else None,
            'high_90': fnum(row['90成本-高']) if '90成本-高' in cols else None,
            'profit_ratio': fnum(row['获利比例'], 4) if '获利比例' in cols else None,
        })
    result['series'] = series
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
