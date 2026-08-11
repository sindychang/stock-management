#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""台股資料抓取共用工具"""

import gzip
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

try:
    import requests
except ImportError:
    sys.exit("缺少 requests：pip install requests")

TAIPEI = timezone(timedelta(hours=8))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
STOCKDIR = os.path.join(DATA, "stock")
CACHE = os.path.join(ROOT, "px-cache")          # 不進 git，靠 Actions cache 保存

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

OPENAPI = "https://openapi.twse.com.tw/v1"
RWD = "https://www.twse.com.tw/rwd/zh"
TPEX = "https://www.tpex.org.tw/openapi/v1"

# 證交所產業別代碼
INDUSTRY = {
    "01": "水泥", "02": "食品", "03": "塑膠", "04": "紡織纖維", "05": "電機機械",
    "06": "電器電纜", "07": "化學生技醫療", "08": "玻璃陶瓷", "09": "造紙",
    "10": "鋼鐵", "11": "橡膠", "12": "汽車", "13": "電子", "14": "建材營造",
    "15": "航運", "16": "觀光餐旅", "17": "金融保險", "18": "貿易百貨",
    "19": "綜合", "20": "其他", "21": "化學工業", "22": "生技醫療",
    "23": "油電燃氣", "24": "半導體", "25": "電腦及週邊設備", "26": "光電",
    "27": "通信網路", "28": "電子零組件", "29": "電子通路", "30": "資訊服務",
    "31": "其他電子", "32": "文化創意", "33": "農業科技", "34": "電子商務",
    "35": "綠能環保", "36": "數位雲端", "37": "運動休閒", "38": "居家生活",
    "80": "管理股票", "91": "存託憑證", "CM": "ETF",
}


def log(msg):
    print(f"[{datetime.now(TAIPEI).strftime('%m-%d %H:%M:%S')}] {msg}", flush=True)


def today_tw():
    return datetime.now(TAIPEI).date()


# ─────────────────────────────────────────────── 數值 / 日期

def num(v):
    """'1,234.5' / '--' / 'X0.00' / '+0.35' → float 或 None"""
    if v is None:
        return None
    s = re.sub(r"<[^>]*>", "", str(v)).strip().replace(",", "").replace("+", "")
    if s in ("", "--", "---", "N/A", "null", "X", "除息", "除權", "除權息", "nan"):
        return None
    if s.startswith("X"):
        s = s[1:]
    try:
        f = float(s)
    except ValueError:
        return None
    return None if f != f else f          # 濾掉 nan


def roc_to_iso(v):
    """1150807 或 115/08/07 或 20260807 → '2026-08-07'"""
    s = re.sub(r"[^0-9]", "", str(v or ""))
    if len(s) == 7:
        return f"{int(s[:3]) + 1911:04d}-{s[3:5]}-{s[5:7]}"
    if len(s) == 8:
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    return ""


def pct_change(change, close):
    if change is None or close is None:
        return None
    prev = close - change
    return round(change / prev * 100, 2) if prev else None


def r(v, n=2):
    return None if v is None else round(v, n)


# ─────────────────────────────────────────────── HTTP

_session = requests.Session()


def get_json(url, params=None, tries=3, timeout=60, sleep=2.0):
    last = None
    for i in range(tries):
        try:
            resp = _session.get(url, params=params, timeout=timeout,
                                headers={"User-Agent": UA, "Accept": "application/json"})
            resp.raise_for_status()
            return resp.json(), None
        except Exception as e:
            last = f"{type(e).__name__}: {str(e)[:140]}"
            if i < tries - 1:
                time.sleep(sleep * (i + 1))
    return None, last


# ─────────────────────────────────────────────── 檔案

def read_json(path, default=None):
    try:
        if path.endswith(".gz"):
            with gzip.open(path, "rt", encoding="utf-8") as f:
                return json.load(f)
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError, EOFError):
        return default


def write_json(path, obj, indent=None):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    if path.endswith(".gz"):
        with gzip.open(path, "wt", encoding="utf-8", compresslevel=6) as f:
            json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
    else:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, separators=(",", ":"), indent=indent)


def load_codes(filename):
    """讀 watchlist.txt / watch_extra.txt：一行一檔，代號在前，# 註解"""
    path = os.path.join(ROOT, filename)
    out = []
    if not os.path.exists(path):
        return out
    for line in open(path, encoding="utf-8-sig"):
        line = line.split("#")[0].strip()
        if not line:
            continue
        code = re.split(r"[\s,\t]+", line)[0]
        if re.fullmatch(r"\d{4,6}[A-Z]?", code) and code not in out:
            out.append(code)
    return out


# ─────────────────────────────────────────────── 技術指標
# 全部吃「由舊到新」的序列，回傳最新值

def sma(vals, n):
    v = [x for x in vals[-n:] if x is not None]
    return round(sum(v) / len(v), 2) if len(v) == n else None


def ema_series(vals, n):
    out, k = [], 2 / (n + 1)
    prev = None
    for v in vals:
        if v is None:
            out.append(prev)
            continue
        prev = v if prev is None else (v - prev) * k + prev
        out.append(prev)
    return out


def rsi(closes, n=14):
    c = [x for x in closes if x is not None]
    if len(c) < n + 1:
        return None
    gains = losses = 0.0
    for i in range(1, n + 1):
        d = c[i] - c[i - 1]
        gains += max(d, 0); losses += max(-d, 0)
    ag, al = gains / n, losses / n
    for i in range(n + 1, len(c)):
        d = c[i] - c[i - 1]
        ag = (ag * (n - 1) + max(d, 0)) / n
        al = (al * (n - 1) + max(-d, 0)) / n
    if al == 0:
        return 100.0 if ag > 0 else 50.0
    return round(100 - 100 / (1 + ag / al), 1)


def kd(highs, lows, closes, n=9, a=3):
    """標準 9 日 KD（RSV 平滑 1/3）。資料不足或整段都是空值時回傳 (None, None)。"""
    if len(closes) < n:
        return None, None
    k = d = 50.0
    seen = 0
    for i in range(n - 1, len(closes)):
        win_h = [x for x in highs[i - n + 1:i + 1] if x is not None]
        win_l = [x for x in lows[i - n + 1:i + 1] if x is not None]
        c = closes[i]
        if c is None or not win_h or not win_l:
            continue
        hh, ll = max(win_h), min(win_l)
        rsv = 50.0 if hh == ll else (c - ll) / (hh - ll) * 100
        k = k * (a - 1) / a + rsv / a
        d = d * (a - 1) / a + k / a
        seen += 1
    if seen < n:                       # 有效天數太少，KD 沒有意義
        return None, None
    return round(k, 1), round(d, 1)


def macd(closes, fast=12, slow=26, sig=9):
    c = [x for x in closes if x is not None]
    if len(c) < slow + sig:
        return None, None, None
    ef, es = ema_series(c, fast), ema_series(c, slow)
    dif = [a - b for a, b in zip(ef, es)]
    dea = ema_series(dif, sig)
    return round(dif[-1], 2), round(dea[-1], 2), round((dif[-1] - dea[-1]) * 2, 2)
