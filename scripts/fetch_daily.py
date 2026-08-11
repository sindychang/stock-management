#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
每日盤後主程式
==============
把當日行情 + 估值 + 籌碼，跟每週抓的基本面合併，算好所有指標，產出前端要用的檔案。

產出
----
  data/screen.json      全市場選股主檔（一列一檔，含技術指標＋財報指標＋籌碼）
  data/industry.json    產業多空比
  data/latest.json      自選股（首頁用，欄位跟 screen 相同）
  data/stock/<代號>.json 觀察清單個股檔（歷史股價＋季財報＋月營收＋股利＋籌碼）
  data/meta.json        抓取狀態

用法
----
  python scripts/fetch_daily.py
  python scripts/fetch_daily.py --offline tests
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib_tw import (CACHE, DATA, INDUSTRY, OPENAPI, RWD, STOCKDIR, TAIPEI, TPEX, datetime,
                    get_json, kd, load_codes, log, macd, num, pct_change, r,
                    read_json, roc_to_iso, rsi, sma, write_json)
from px_store import PxStore

STOCK_PX_DAYS = 260          # 個股檔保留幾天股價
CHIP_DAYS = 60               # 個股檔保留幾天籌碼


# ════════════════════════════════════════════════ 抓取

def src(name, offline, url, params=None):
    if offline:
        d = read_json(os.path.join(offline, name + ".json"))
        return (d, None) if d is not None else (None, f"缺少樣本檔 {name}.json")
    return get_json(url, params=params)


def parse_twse_daily(rows):
    out, date = {}, None
    for row in rows or []:
        code = str(row.get("Code", "")).strip()
        if not code:
            continue
        date = date or roc_to_iso(row.get("Date"))
        close = num(row.get("ClosingPrice"))
        chg = num(row.get("Change"))
        out[code] = {"code": code, "name": str(row.get("Name", "")).strip(), "market": "上市",
                     "close": close, "change": chg, "changePct": pct_change(chg, close),
                     "open": num(row.get("OpeningPrice")), "high": num(row.get("HighestPrice")),
                     "low": num(row.get("LowestPrice")), "volume": num(row.get("TradeVolume")),
                     "amount": num(row.get("TradeValue"))}
    return out, date


def parse_tpex_daily(rows):
    def pk(row, *ks):
        for k in ks:
            if k in row and str(row[k]).strip() not in ("", "--"):
                return row[k]
        return None
    out, date = {}, None
    for row in rows or []:
        code = str(pk(row, "SecuritiesCompanyCode", "Code", "code") or "").strip()
        if not code:
            continue
        date = date or roc_to_iso(pk(row, "Date", "date"))
        close = num(pk(row, "Close", "ClosingPrice"))
        chg = num(pk(row, "Change", "change"))
        out[code] = {"code": code, "name": str(pk(row, "CompanyName", "Name") or "").strip(),
                     "market": "上櫃", "close": close, "change": chg,
                     "changePct": pct_change(chg, close),
                     "open": num(pk(row, "Open", "OpeningPrice")),
                     "high": num(pk(row, "High", "HighestPrice")),
                     "low": num(pk(row, "Low", "LowestPrice")),
                     "volume": num(pk(row, "TradingShares", "TradeVolume")),
                     "amount": num(pk(row, "TradeAmount", "TradeValue"))}
    return out, date


def parse_valuation(rows):
    out = {}
    for row in rows or []:
        code = str(row.get("Code", "")).strip()
        if code:
            out[code] = {"pe": num(row.get("PEratio")), "pb": num(row.get("PBratio")),
                         "dy": num(row.get("DividendYield"))}
    return out


def parse_chips(j):
    """T86 三大法人買賣超（股數 → 轉成張）"""
    if not isinstance(j, dict):
        return {}
    fields = [str(f) for f in (j.get("fields") or [])]

    def idx(*keys):
        for i, f in enumerate(fields):
            for k in keys:
                if k in f:
                    return i
        return None

    i_code = idx("證券代號")
    i_fi = idx("外陸資買賣超股數", "外資買賣超股數")
    i_it = idx("投信買賣超股數")
    i_dl = idx("自營商買賣超股數(合計)", "自營商買賣超股數")
    i_all = idx("三大法人買賣超股數")
    if i_code is None:
        return {}
    out = {}
    for row in (j.get("data") or []):
        try:
            code = str(row[i_code]).strip()
        except (IndexError, TypeError):
            continue
        if not code:
            continue

        def g(i):
            v = num(row[i]) if (i is not None and i < len(row)) else None
            return None if v is None else round(v / 1000)      # 股 → 張
        out[code] = {"fi": g(i_fi), "it": g(i_it), "dl": g(i_dl), "f3": g(i_all)}
    return out


# ════════════════════════════════════════════════ 指標

def tech(store, code, close):
    """回傳技術指標 dict"""
    c = store.series(code, "c")
    h = store.series(code, "h")
    l = store.series(code, "l")
    v = store.series(code, "v")
    if not c:
        return {}
    t = {}
    for n in (5, 20, 60, 120):
        t[f"ma{n}"] = sma(c, n)
    if t.get("ma20") and close:
        t["bias20"] = r((close - t["ma20"]) / t["ma20"] * 100)
    t["rsi"] = rsi(c, 14)
    t["k"], t["d"] = kd(h, l, c, 9, 3)
    t["dif"], t["dea"], t["osc"] = macd(c)
    v20 = sma(v, 20)
    if v20 and v and v[-1]:
        t["vr"] = r(v[-1] / v20)
    cc = [x for x in c if x is not None]
    if cc:
        t["hiN"] = max(cc)
        t["loN"] = min(cc)
        t["nDays"] = len(cc)
        if close and t["hiN"]:
            t["fromHi"] = r((close - t["hiN"]) / t["hiN"] * 100)
    # 均線多頭排列
    ms = [t.get("ma5"), t.get("ma20"), t.get("ma60")]
    if all(ms):
        t["trend"] = 1 if ms[0] > ms[1] > ms[2] else (-1 if ms[0] < ms[1] < ms[2] else 0)
    return t


def fund_metrics(f, close, pb):
    """從基本面資料算出季報／月營收／估值衍生指標"""
    o = {}
    if not f:
        return o
    if f.get("indName"):
        o["ind"] = f["indName"]
    qs = f.get("q") or []
    if qs:
        last = qs[-1]
        o["q"] = last.get("q")
        for k in ("rev", "gm", "opm", "npm", "eps"):
            if last.get(k) is not None:
                o[k] = last[k]
        # 單季營收 YoY（跟去年同一季比）
        try:
            y, qn = last["q"].split("Q")
            prev_key = f"{int(y) - 1}Q{qn}"
            prev = next((x for x in qs if x.get("q") == prev_key), None)
            if prev and prev.get("rev") and last.get("rev"):
                o["revYoy"] = r((last["rev"] - prev["rev"]) / abs(prev["rev"]) * 100)
        except (ValueError, KeyError, TypeError):
            pass
        eps4 = [x.get("eps") for x in qs[-4:] if x.get("eps") is not None]
        if len(eps4) == 4:
            o["eps4"] = r(sum(eps4))
    ms = f.get("m") or []
    if ms:
        last = ms[-1]
        o["mMonth"] = last.get("m")
        o["mRev"] = last.get("rev")
        o["mYoy"] = last.get("yoy")
        o["mMom"] = last.get("mom")
        o["mCumYoy"] = last.get("cumYoy")
    divs = f.get("div") or []
    if divs:
        o["divY"] = divs[-1].get("y")
        o["divCash"] = divs[-1].get("cash")
        cash5 = [x.get("cash") or 0 for x in divs[-5:]]
        if cash5:
            o["divAvg5"] = r(sum(cash5) / len(cash5), 3)
    # 每股淨值、ROE、盈餘殖利率
    if close and pb:
        bv = close / pb
        o["bv"] = r(bv)
        if o.get("eps4") is not None and bv:
            o["roe"] = r(o["eps4"] / bv * 100)
    if close and o.get("eps4") is not None:
        o["ey"] = r(o["eps4"] / close * 100)
    return o


def magic_formula(rows):
    """
    葛林布雷神奇公式（台股常用的簡化版，並在畫面上註明是簡化版）
      盈餘殖利率 ey  = 近四季 EPS / 股價
      資本報酬率 roc = 近四季 EPS / 每股淨值   （即 ROE）
    兩者各自排名，名次相加越小越好。
    """
    elig = [x for x in rows if x.get("ey") and x["ey"] > 0
            and x.get("roe") and x["roe"] > 0
            and x.get("close") and (x.get("volume") or 0) > 100_000]
    if not elig:
        return
    for key, rank_key in (("ey", "_rey"), ("roe", "_rroe")):
        for i, x in enumerate(sorted(elig, key=lambda z: -z[key]), 1):
            x[rank_key] = i
    ranked = sorted(elig, key=lambda x: x["_rey"] + x["_rroe"])
    for i, x in enumerate(ranked, 1):
        x["mf"] = i
    for x in elig:
        x.pop("_rey", None)
        x.pop("_rroe", None)
    log(f"✓ 神奇公式排名 {len(ranked)} 檔（有近四季 EPS 且 EPS、淨值皆為正）")


# ════════════════════════════════════════════════ 主流程

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", metavar="DIR", help="讀本機樣本檔測試")
    args = ap.parse_args()
    off = args.offline
    status = {}
    now = datetime.now(TAIPEI).isoformat(timespec="seconds")

    # ── 行情
    quotes, trade_date = {}, None
    rows, err = src("twse_daily", off, f"{OPENAPI}/exchangeReport/STOCK_DAY_ALL")
    if err:
        status["上市收盤"] = "失敗：" + err
        log(f"✗ 上市收盤失敗：{err}")
    else:
        q, d = parse_twse_daily(rows)
        quotes.update(q); trade_date = trade_date or d
        status["上市收盤"] = f"成功 {len(q)} 檔"
        log(f"✓ 上市 {len(q)} 檔，交易日 {d}")

    rows, err = src("tpex_daily", off, f"{TPEX}/tpex_mainboard_daily_close_quotes")
    if err:
        status["上櫃收盤"] = "失敗：" + err
        log(f"⚠ 上櫃收盤失敗（不影響上市）：{err}")
    else:
        q, d = parse_tpex_daily(rows)
        quotes.update(q); trade_date = trade_date or d
        status["上櫃收盤"] = f"成功 {len(q)} 檔"
        log(f"✓ 上櫃 {len(q)} 檔")

    if not quotes or not trade_date:
        sys.exit("❌ 沒有行情資料，中止（不覆寫既有檔案）")

    # ── 估值
    val = {}
    rows, err = src("twse_valuation", off, f"{OPENAPI}/exchangeReport/BWIBBU_ALL")
    if err:
        status["估值"] = "失敗：" + err
        log(f"⚠ 估值失敗：{err}")
    else:
        val = parse_valuation(rows)
        status["估值"] = f"成功 {len(val)} 檔"
        log(f"✓ 估值 {len(val)} 檔")

    # ── 籌碼
    chips = {}
    j, err = src("twse_chips", off, f"{RWD}/fund/T86",
                 params={"date": trade_date.replace("-", ""),
                         "selectType": "ALLBUT0999", "response": "json"})
    if err:
        status["三大法人"] = "失敗：" + err
        log(f"⚠ 三大法人失敗：{err}")
    else:
        chips = parse_chips(j)
        status["三大法人"] = f"成功 {len(chips)} 檔"
        log(f"✓ 三大法人 {len(chips)} 檔")

    # ── 基本面（由 fetch_fundamentals.py 每週產生）
    fund = (read_json(os.path.join(CACHE, "fundamentals.json"), {}) or {}).get("stocks", {})
    status["基本面"] = f"{len(fund)} 檔" if fund else "無（fetch_fundamentals.py 沒跑成功）"
    log(f"{'✓' if fund else '⚠'} 基本面 {len(fund)} 檔")

    # ── 行情倉庫：寫入今天，並算技術指標
    store = PxStore()
    store.put_day(trade_date, {c: q for c, q in quotes.items() if q["close"] is not None})
    log(f"行情倉庫：{len(store)} 個交易日")
    if len(store) < 25:
        log("⚠ 倉庫天數不足 25 天，技術指標大多會是空的。"
            "請執行一次「回補全市場歷史行情」workflow。")

    # ── 組出 screen 主檔
    rows_out = []
    for code, q in quotes.items():
        if q["close"] is None:
            continue
        v = val.get(code, {})
        f = fund.get(code, {})
        row = {
            "c": code, "n": q["name"] or f.get("name") or code, "mk": q["market"],
            "close": q["close"], "chg": q["change"], "pc": q["changePct"],
            "volume": q["volume"], "amt": q["amount"],
            "pe": v.get("pe"), "pb": v.get("pb"), "dy": v.get("dy"),
        }
        row.update(fund_metrics(f, q["close"], v.get("pb")))
        row.update(tech(store, code, q["close"]))
        ch = chips.get(code)
        if ch:
            row.update(ch)
        rows_out.append({k: val_ for k, val_ in row.items() if val_ is not None})

    magic_formula(rows_out)
    rows_out.sort(key=lambda x: x["c"])

    write_json(os.path.join(DATA, "screen.json"),
               {"tradeDate": trade_date, "updatedAt": now, "count": len(rows_out),
                "rows": rows_out})
    log(f"→ data/screen.json（{len(rows_out)} 檔）")

    # ── 自選股（首頁用）
    watch = load_codes("watchlist.txt")
    idx = {x["c"]: x for x in rows_out}
    write_json(os.path.join(DATA, "latest.json"), {
        "tradeDate": trade_date, "updatedAt": now,
        "quotes": [dict(idx[c], **{"code": c, "name": idx[c]["n"]}) if c in idx
                   else {"code": c, "missing": True} for c in watch],
    })
    log(f"→ data/latest.json（{len(watch)} 檔）")

    # ── 產業多空比
    ind = {}
    for x in rows_out:
        name = x.get("ind") or "未分類"
        b = ind.setdefault(name, {"ind": name, "up": 0, "down": 0, "flat": 0,
                                  "n": 0, "amt": 0, "sumPc": 0})
        b["n"] += 1
        b["amt"] += x.get("amt") or 0
        pc = x.get("pc")
        if pc is None or pc == 0:
            b["flat"] += 1
        elif pc > 0:
            b["up"] += 1
        else:
            b["down"] += 1
        if pc is not None:
            b["sumPc"] += pc
    for b in ind.values():
        tradable = b["up"] + b["down"]
        b["ratio"] = r(b["up"] / tradable * 100, 1) if tradable else None
        b["avgPc"] = r(b["sumPc"] / b["n"]) if b["n"] else None
        b["amt"] = round(b["amt"])
        b.pop("sumPc")
    write_json(os.path.join(DATA, "industry.json"), {
        "tradeDate": trade_date, "updatedAt": now,
        "rows": sorted(ind.values(), key=lambda x: -(x["ratio"] or 0)),
    })
    log(f"→ data/industry.json（{len(ind)} 個產業）")

    # ── 觀察清單個股檔
    detail_codes = []
    for c in watch + load_codes("watch_extra.txt"):
        if c not in detail_codes:
            detail_codes.append(c)
    made = 0
    for code in detail_codes:
        f = fund.get(code, {})
        row = idx.get(code, {})
        dates = store.dates[-STOCK_PX_DAYS:]
        c_ser = store.series(code, "c")[-STOCK_PX_DAYS:]
        h_ser = store.series(code, "h")[-STOCK_PX_DAYS:]
        l_ser = store.series(code, "l")[-STOCK_PX_DAYS:]
        v_ser = store.series(code, "v")[-STOCK_PX_DAYS:]
        path = os.path.join(STOCKDIR, f"{code}.json")
        old = read_json(path, {}) or {}
        chip_hist = old.get("chips", [])
        ch = chips.get(code)
        if ch:
            chip_hist = [x for x in chip_hist if x.get("d") != trade_date]
            chip_hist.append(dict(ch, d=trade_date))
            chip_hist = sorted(chip_hist, key=lambda x: x["d"])[-CHIP_DAYS:]
        write_json(path, {
            "code": code, "name": row.get("n") or f.get("name") or code,
            "ind": f.get("indName"), "tradeDate": trade_date, "updatedAt": now,
            "px": {"d": dates, "c": c_ser, "h": h_ser, "l": l_ser, "v": v_ser},
            "q": f.get("q") or [], "m": f.get("m") or [], "div": f.get("div") or [],
            "chips": chip_hist,
            "snap": row,
        })
        made += 1
    write_json(os.path.join(DATA, "watch.json"), {"codes": detail_codes})
    log(f"→ data/stock/*.json（{made} 檔）")

    store.drop_dead()
    store.save()

    write_json(os.path.join(DATA, "meta.json"), {
        "tradeDate": trade_date, "updatedAt": now, "sources": status,
        "counts": {"market": len(rows_out), "industry": len(ind),
                   "watch": len(watch), "detail": made,
                   "pxDays": len(store), "fundamentals": len(fund)},
    })
    log(f"完成。交易日 {trade_date}")


if __name__ == "__main__":
    main()
