#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
盤後資料抓取（證交所 TWSE + 櫃買 TPEx 公開資料）
================================================
每個交易日盤後跑一次，把公開資料整理成前端好讀的 JSON。
跑在 GitHub Actions 上，所以「不需要開你的電腦」，也不需要任何金鑰。

產出（都在 data/ 底下）
----------------------
  latest.json          自選股最新收盤 + 估值（檔案很小，前端首頁只讀這個）
  market.json          全市場精簡收盤（代號/收盤/漲跌/量），給排行與選股用
  valuation.json       全市場 本益比 / 股價淨值比 / 殖利率
  history/<代號>.json   自選股的歷史收盤（每天累積一筆，畫走勢圖用）
  meta.json            這次抓了哪一天、每個來源成功還是失敗

用法
----
    python scripts/fetch_twse.py                 # 正常抓
    python scripts/fetch_twse.py --offline tests # 用本機樣本檔測試解析邏輯
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone, timedelta

try:
    import requests
except ImportError:
    sys.exit("缺少 requests：pip install requests")

TAIPEI = timezone(timedelta(hours=8))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
HIST = os.path.join(DATA, "history")

# 歷史收盤最多保留幾筆（約 5 年交易日）
HISTORY_KEEP = 1300

SOURCES = {
    # 上市：每日收盤行情（全部證券）
    "twse_daily": "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL",
    # 上市：本益比、殖利率、股價淨值比
    "twse_valuation": "https://openapi.twse.com.tw/v1/exchangeReport/BWIBBU_ALL",
    # 上櫃：主板每日收盤（TPEx 會擋掉沒有 User-Agent 的請求）
    "tpex_daily": "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes",
}

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")


def log(msg):
    print(f"[{datetime.now(TAIPEI).strftime('%m-%d %H:%M:%S')}] {msg}", flush=True)


# ------------------------------------------------------------------ 小工具

def num(v):
    """把 '1,234.5' / '--' / 'X0.00' / '+0.35' 這類值轉成 float，轉不動就回 None"""
    if v is None:
        return None
    s = str(v).strip().replace(",", "").replace("+", "")
    if s in ("", "--", "---", "N/A", "null", "X", "除息", "除權", "除權息"):
        return None
    # TWSE 偶爾會在漲跌前面加 X（表示該日除權息）
    if s.startswith("X"):
        s = s[1:]
    try:
        return float(s)
    except ValueError:
        return None


def roc_to_iso(v):
    """民國日期 1150807 → '2026-08-07'"""
    s = str(v or "").strip()
    if len(s) == 7 and s.isdigit():
        return f"{int(s[:3]) + 1911:04d}-{s[3:5]}-{s[5:7]}"
    if len(s) == 8 and s.isdigit():          # 有些來源直接給西元 20260807
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    return s


def pct(change, close):
    """用收盤價與漲跌額反推漲跌幅(%)"""
    if change is None or close is None:
        return None
    prev = close - change
    if not prev:
        return None
    return round(change / prev * 100, 2)


def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default


def write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))


def load_watchlist():
    """跟 quote-bot 用同一種格式：一行一檔，代號在前，# 是註解"""
    import re
    path = os.path.join(ROOT, "watchlist.txt")
    out = []
    for line in open(path, encoding="utf-8-sig"):
        line = line.split("#")[0].strip()
        if not line:
            continue
        parts = re.split(r"[\s,\t]+", line, maxsplit=1)
        code = parts[0]
        if re.fullmatch(r"\d{4,6}[A-Z]?", code) and code not in [o["code"] for o in out]:
            out.append({"code": code, "name": parts[1].strip() if len(parts) > 1 else ""})
    return out


# ------------------------------------------------------------------ 抓資料

def fetch(name, offline_dir=None):
    """回傳 (資料, 錯誤訊息)。offline_dir 有值時改讀本機樣本檔，方便測試。"""
    if offline_dir:
        path = os.path.join(offline_dir, name + ".json")
        d = read_json(path)
        return (d, None) if d is not None else (None, f"找不到樣本檔 {path}")
    try:
        r = requests.get(SOURCES[name], timeout=60,
                         headers={"User-Agent": UA, "Accept": "application/json"})
        r.raise_for_status()
        return r.json(), None
    except Exception as e:
        return None, f"{type(e).__name__}: {str(e)[:160]}"


# ------------------------------------------------------------------ 解析

def parse_twse_daily(rows):
    """證交所每日收盤行情 → {代號: {...}}"""
    out, date = {}, None
    for r in rows or []:
        code = str(r.get("Code", "")).strip()
        if not code:
            continue
        close = num(r.get("ClosingPrice"))
        change = num(r.get("Change"))
        date = date or roc_to_iso(r.get("Date"))
        out[code] = {
            "code": code,
            "name": str(r.get("Name", "")).strip(),
            "market": "上市",
            "close": close,
            "change": change,
            "changePct": pct(change, close),
            "open": num(r.get("OpeningPrice")),
            "high": num(r.get("HighestPrice")),
            "low": num(r.get("LowestPrice")),
            "volume": num(r.get("TradeVolume")),
        }
    return out, date


def parse_tpex_daily(rows):
    """櫃買主板每日收盤 → {代號: {...}}。TPEx 欄位名稱不太穩，所以逐一猜。"""
    def pick(r, *keys):
        for k in keys:
            if k in r and str(r[k]).strip() not in ("", "--"):
                return r[k]
        return None

    out, date = {}, None
    for r in rows or []:
        code = str(pick(r, "SecuritiesCompanyCode", "Code", "code", "股票代號") or "").strip()
        if not code:
            continue
        close = num(pick(r, "Close", "ClosingPrice", "close", "收盤"))
        change = num(pick(r, "Change", "change", "漲跌"))
        date = date or roc_to_iso(pick(r, "Date", "date", "資料日期"))
        out[code] = {
            "code": code,
            "name": str(pick(r, "CompanyName", "Name", "name", "名稱") or "").strip(),
            "market": "上櫃",
            "close": close,
            "change": change,
            "changePct": pct(change, close),
            "open": num(pick(r, "Open", "OpeningPrice", "開盤")),
            "high": num(pick(r, "High", "HighestPrice", "最高")),
            "low": num(pick(r, "Low", "LowestPrice", "最低")),
            "volume": num(pick(r, "TradingShares", "TradeVolume", "成交股數")),
        }
    return out, date


def parse_valuation(rows):
    """本益比 / 殖利率 / 股價淨值比 → {代號: {...}}"""
    out = {}
    for r in rows or []:
        code = str(r.get("Code", "")).strip()
        if not code:
            continue
        out[code] = {
            "pe": num(r.get("PEratio")),
            "pb": num(r.get("PBratio")),
            "yield": num(r.get("DividendYield")),
        }
    return out


# ------------------------------------------------------------------ 主流程

def backfill(months):
    """
    回補自選股的歷史收盤。
    用證交所「個股月成交資訊」端點，一次拿一檔一個月，所以會呼叫很多次，
    中間刻意 sleep 3.5 秒避免被當成攻擊。平常不用跑，只在第一次建立時跑一次。
    """
    import time
    url = "https://www.twse.com.tw/exchangeReport/STOCK_DAY"
    watchlist = load_watchlist()
    today = datetime.now(TAIPEI).date()

    # 產生要抓的年月清單（由舊到新）
    ym = []
    y, m = today.year, today.month
    for _ in range(months):
        ym.append((y, m))
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    ym.reverse()

    total = len(watchlist) * len(ym)
    done = 0
    for w in watchlist:
        code = w["code"]
        path = os.path.join(HIST, f"{code}.json")
        hist = read_json(path, []) or []
        seen = {h["d"] for h in hist}
        for (yy, mm) in ym:
            done += 1
            try:
                r = requests.get(url, timeout=45,
                                 params={"response": "json", "date": f"{yy}{mm:02d}01", "stockNo": code},
                                 headers={"User-Agent": UA})
                j = r.json()
            except Exception as e:
                log(f"  ⚠ {code} {yy}-{mm:02d} 失敗：{type(e).__name__}")
                time.sleep(3.5)
                continue
            added = 0
            for row in (j.get("data") or []):
                # row = [日期, 成交股數, 成交金額, 開盤, 最高, 最低, 收盤, 漲跌價差, 成交筆數]
                try:
                    d = roc_to_iso(row[0].replace("/", ""))
                    close = num(row[6])
                    vol = num(row[1])
                except (IndexError, AttributeError):
                    continue
                if not d or close is None or d in seen:
                    continue
                hist.append({"d": d, "c": close, "v": vol})
                seen.add(d)
                added += 1
            log(f"  [{done}/{total}] {code} {yy}-{mm:02d} +{added} 筆")
            time.sleep(3.5)
        hist.sort(key=lambda h: h["d"])
        write_json(path, hist[-HISTORY_KEEP:])
        log(f"✓ {code} 歷史共 {len(hist)} 筆")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", metavar="DIR",
                    help="改讀本機樣本檔（DIR/twse_daily.json 等），用來測試解析邏輯")
    ap.add_argument("--backfill", type=int, metavar="月數",
                    help="回補自選股過去 N 個月的歷史收盤後結束（第一次建立時跑一次就好）")
    args = ap.parse_args()

    if args.backfill:
        log(f"開始回補過去 {args.backfill} 個月的歷史收盤（會慢，請耐心等）")
        backfill(args.backfill)
        log("回補完成")
        return

    watchlist = load_watchlist()
    wl_codes = [w["code"] for w in watchlist]
    wl_names = {w["code"]: w["name"] for w in watchlist}
    log(f"自選股 {len(wl_codes)} 檔：{', '.join(wl_codes)}")

    status, quotes, trade_date = {}, {}, None

    # ── 上市（必要）
    rows, err = fetch("twse_daily", args.offline)
    if err:
        status["twse_daily"] = "失敗：" + err
        log(f"✗ 上市收盤抓取失敗：{err}")
    else:
        parsed, d = parse_twse_daily(rows)
        quotes.update(parsed)
        trade_date = trade_date or d
        status["twse_daily"] = f"成功 {len(parsed)} 檔"
        log(f"✓ 上市 {len(parsed)} 檔，交易日 {d}")

    # ── 上櫃（可有可無，TPEx 有時會擋）
    rows, err = fetch("tpex_daily", args.offline)
    if err:
        status["tpex_daily"] = "失敗：" + err
        log(f"⚠ 上櫃收盤抓取失敗（不影響上市資料）：{err}")
    else:
        parsed, d = parse_tpex_daily(rows)
        quotes.update(parsed)
        trade_date = trade_date or d
        status["tpex_daily"] = f"成功 {len(parsed)} 檔"
        log(f"✓ 上櫃 {len(parsed)} 檔")

    if not quotes:
        sys.exit("❌ 一檔都沒抓到，中止（不覆蓋既有資料）")

    # ── 估值
    val = {}
    rows, err = fetch("twse_valuation", args.offline)
    if err:
        status["twse_valuation"] = "失敗：" + err
        log(f"⚠ 估值資料抓取失敗：{err}")
    else:
        val = parse_valuation(rows)
        status["twse_valuation"] = f"成功 {len(val)} 檔"
        log(f"✓ 估值 {len(val)} 檔")

    # ── 如果交易日跟上次一樣，代表今天沒開盤 / 資料還沒更新
    prev_meta = read_json(os.path.join(DATA, "meta.json"), {}) or {}
    if trade_date and prev_meta.get("tradeDate") == trade_date:
        log(f"交易日 {trade_date} 與上次相同（今天可能沒開盤），仍會覆寫一次以更新時間戳")

    now = datetime.now(TAIPEI).isoformat(timespec="seconds")

    # ── latest.json：只有自選股，前端首頁讀這個（很小、很快）
    latest = []
    for code in wl_codes:
        q = quotes.get(code)
        if not q:
            log(f"⚠ 自選股 {code} 在今日行情中找不到（可能停牌或代號有誤）")
            latest.append({"code": code, "name": wl_names.get(code, ""), "missing": True})
            continue
        row = dict(q)
        if wl_names.get(code):
            row["name"] = wl_names[code]
        row.update(val.get(code, {}))
        latest.append(row)
    write_json(os.path.join(DATA, "latest.json"),
               {"tradeDate": trade_date, "updatedAt": now, "quotes": latest})
    log(f"→ data/latest.json（{len(latest)} 檔）")

    # ── market.json：全市場精簡，給排行/選股用
    market = [{"c": q["code"], "n": q["name"], "m": q["market"],
               "p": q["close"], "ch": q["change"], "pc": q["changePct"], "v": q["volume"]}
              for q in quotes.values() if q["close"] is not None]
    market.sort(key=lambda x: x["c"])
    write_json(os.path.join(DATA, "market.json"),
               {"tradeDate": trade_date, "updatedAt": now, "rows": market})
    log(f"→ data/market.json（{len(market)} 檔）")

    # ── valuation.json：全市場估值
    if val:
        write_json(os.path.join(DATA, "valuation.json"),
                   {"tradeDate": trade_date, "updatedAt": now, "rows": val})
        log(f"→ data/valuation.json（{len(val)} 檔）")

    # ── history/<代號>.json：自選股歷史收盤，每天累積一筆
    for code in wl_codes:
        q = quotes.get(code)
        if not q or q["close"] is None or not trade_date:
            continue
        path = os.path.join(HIST, f"{code}.json")
        hist = read_json(path, []) or []
        hist = [h for h in hist if h.get("d") != trade_date]      # 同一天只留一筆
        hist.append({"d": trade_date, "c": q["close"], "v": q["volume"]})
        hist.sort(key=lambda h: h["d"])
        write_json(path, hist[-HISTORY_KEEP:])
    log(f"→ data/history/*.json（{len(wl_codes)} 檔）")

    # ── meta.json
    write_json(os.path.join(DATA, "meta.json"), {
        "tradeDate": trade_date,
        "updatedAt": now,
        "sources": status,
        "counts": {"market": len(market), "valuation": len(val), "watchlist": len(wl_codes)},
    })
    log(f"完成。交易日 {trade_date}")


if __name__ == "__main__":
    main()
