#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
回補全市場歷史行情
==================
用證交所「每日收盤行情（全部）」端點，**一個請求拿一整天的全市場**，
所以回補 150 個交易日只要 150 次請求（約 5 分鐘），不是 1900 檔各抓一次。

    python scripts/backfill_px.py --days 150

上櫃（TPEx）沒有等價的逐日端點，所以歷史只含上市；
上櫃股票的技術指標會在每天累積之後慢慢補齊。
"""

import argparse
import os
import sys
import time
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib_tw import RWD, get_json, log, num, read_json, today_tw
from px_store import PxStore

STOCK_TABLE_HINT = "每日收盤行情"


def parse_mi_index(j):
    """從 MI_INDEX 的 tables 裡找出個股行情那張表"""
    if not isinstance(j, dict):
        return {}
    tables = j.get("tables") or []
    target = None
    for t in tables:
        if STOCK_TABLE_HINT in str(t.get("title", "")):
            target = t
            break
    if target is None:
        return {}
    fields = [str(f).strip() for f in (target.get("fields") or [])]
    try:
        i_code = fields.index("證券代號")
        i_open = fields.index("開盤價")
        i_high = fields.index("最高價")
        i_low = fields.index("最低價")
        i_close = fields.index("收盤價")
        i_vol = fields.index("成交股數")
    except ValueError:
        log(f"⚠ 欄位對不上：{fields}")
        return {}
    out = {}
    for row in (target.get("data") or []):
        try:
            code = str(row[i_code]).strip()
        except (IndexError, TypeError):
            continue
        if not code:
            continue
        c = num(row[i_close])
        if c is None:
            continue                       # 當天沒成交，跳過
        out[code] = {"close": c, "high": num(row[i_high]), "low": num(row[i_low]),
                     "open": num(row[i_open]), "volume": num(row[i_vol])}
    return out


def fetch_day(date):
    """date 為 datetime.date；回傳 {code: quote} 或 {}（假日／無資料）"""
    j, err = get_json(f"{RWD}/afterTrading/MI_INDEX",
                      params={"date": date.strftime("%Y%m%d"),
                              "type": "ALLBUT0999", "response": "json"},
                      tries=2, sleep=3)
    if err:
        log(f"  ⚠ {date} 請求失敗：{err}")
        return {}
    return parse_mi_index(j)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=150, help="往回找幾個「日曆日」內的交易日")
    ap.add_argument("--sleep", type=float, default=2.0, help="每次請求間隔秒數")
    ap.add_argument("--offline", metavar="DIR", help="讀 DIR/mi_index_YYYYMMDD.json 測試")
    args = ap.parse_args()

    store = PxStore()
    log(f"目前倉庫有 {len(store)} 個交易日")

    # 由舊到新回補，這樣中途失敗也不會留下空洞
    start = today_tw() - timedelta(days=int(args.days * 1.5))   # 日曆日要多抓，因為有假日
    days = [start + timedelta(days=i) for i in range((today_tw() - start).days + 1)]
    days = [d for d in days if d.weekday() < 5]                  # 跳過週末

    got = skipped = 0
    for d in days:
        iso = d.isoformat()
        if store.has_date(iso):
            skipped += 1
            continue
        if args.offline:
            j = read_json(os.path.join(args.offline, f"mi_index_{d:%Y%m%d}.json"))
            q = parse_mi_index(j) if j else {}
        else:
            q = fetch_day(d)
            time.sleep(args.sleep)
        if q:
            store.put_day(iso, q)
            got += 1
            log(f"  ✓ {iso} {len(q)} 檔")
        else:
            log(f"  · {iso} 無資料（假日或休市）")

    dead = store.drop_dead()
    store.save()
    log(f"回補完成：新增 {got} 天，略過已有 {skipped} 天，清掉 {dead} 個無資料代號")


if __name__ == "__main__":
    main()
