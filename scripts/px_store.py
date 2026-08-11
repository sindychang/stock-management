#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
全市場行情滾動倉庫
==================
保存「近 N 個交易日 × 全市場」的開高低收量，供計算技術指標。

這份檔案刻意**不進 git**（會讓 repo 快速膨脹），而是靠 GitHub Actions 的 cache 保存。
cache 萬一失效也沒關係：backfill_px.py 會用 MI_INDEX 自動回補，
因為那個端點「一個請求就回傳一整天的全市場行情」，130 天只要幾分鐘。

結構（gzip JSON）：
{
  "dates": ["2026-02-10", ..., "2026-08-07"],        # 由舊到新
  "s": {"2330": {"c":[...], "h":[...], "l":[...], "v":[...]}}   # 與 dates 等長，缺值 null
}
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib_tw import CACHE, log, read_json, write_json

PX_PATH = os.path.join(CACHE, "px.json.gz")
KEEP_DAYS = 150          # 留 150 天，MA120 與 KD 都夠用


class PxStore:
    def __init__(self, path=PX_PATH):
        self.path = path
        d = read_json(path, None) or {}
        self.dates = d.get("dates", [])
        self.s = d.get("s", {})

    # ── 查詢
    def has_date(self, date):
        return date in self.dates

    def series(self, code, field):
        row = self.s.get(code)
        return row.get(field, []) if row else []

    def codes(self):
        return list(self.s.keys())

    def __len__(self):
        return len(self.dates)

    # ── 寫入
    def put_day(self, date, quotes):
        """quotes = {code: {"close":..,"high":..,"low":..,"volume":..}}；同一天重複寫會覆蓋"""
        if not date or not quotes:
            return
        if date in self.dates:
            idx = self.dates.index(date)
        else:
            # 保持日期由舊到新
            self.dates.append(date)
            self.dates.sort()
            idx = self.dates.index(date)
            for row in self.s.values():
                for f in ("c", "h", "l", "v"):
                    row[f].insert(idx, None)
        n = len(self.dates)
        for code, q in quotes.items():
            row = self.s.get(code)
            if row is None:
                row = {f: [None] * n for f in ("c", "h", "l", "v")}
                self.s[code] = row
            for f in ("c", "h", "l", "v"):
                if len(row[f]) < n:
                    row[f] += [None] * (n - len(row[f]))
            row["c"][idx] = q.get("close")
            row["h"][idx] = q.get("high")
            row["l"][idx] = q.get("low")
            row["v"][idx] = q.get("volume")

    def trim(self, keep=KEEP_DAYS):
        if len(self.dates) <= keep:
            return
        cut = len(self.dates) - keep
        self.dates = self.dates[cut:]
        for row in self.s.values():
            for f in ("c", "h", "l", "v"):
                row[f] = row[f][cut:]

    def drop_dead(self, min_points=3):
        """清掉長期沒資料的代號（下市、改代號）"""
        dead = [c for c, row in self.s.items()
                if sum(1 for x in row["c"] if x is not None) < min_points]
        for c in dead:
            del self.s[c]
        return len(dead)

    def save(self):
        self.trim()
        write_json(self.path, {"dates": self.dates, "s": self.s})
        log(f"→ 行情倉庫：{len(self.dates)} 個交易日 × {len(self.s)} 檔"
            f"（{os.path.getsize(self.path) / 1e6:.1f} MB，不進 git）")
