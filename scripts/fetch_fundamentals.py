#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
基本面資料抓取（每週跑一次就夠，財報是每季公告的）
==================================================
來源全部是證交所 OpenAPI 的公開資料，不需要金鑰。

  t187ap03_L      公司基本資料（含產業別、股本、發行股數）
  t187ap06_L_ci   綜合損益表－一般業（營收、毛利、營業利益、稅前淨利、EPS）
  t187ap17_L      營益分析（毛利率、營業利益率、稅前純益率、稅後純益率）
  t187ap05_P      每月營業收入（含去年同期、累計）
  t187ap45_L      股利分派情形

產出 data/fundamentals.json：
{
  "updatedAt": "...",
  "stocks": {
     "2330": {
        "name":"台積電", "ind":"24", "indName":"半導體", "shares":25930380458,
        "q":[{"q":"115Q2","rev":...,"gm":...,"opm":...,"npm":...,"eps":...}, ...],   # 由舊到新
        "m":[{"m":"2026-06","rev":...,"yoy":...,"mom":...,"cum":...,"cumYoy":...}],  # 月營收
        "div":[{"y":"114","cash":...,"stock":...}]
     }, ...
  }
}
歷史會累積：每次抓到的新季別／新月份會併進既有檔案，不會覆蓋掉舊的。
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib_tw import (CACHE, INDUSTRY, OPENAPI, get_json, log, num, r, read_json,
                    write_json, datetime, TAIPEI)

FUND_PATH = os.path.join(CACHE, "fundamentals.json")   # 不進 git，靠 Actions cache 保存

MAX_QUARTERS = 16        # 保留近 4 年季度
MAX_MONTHS = 30          # 保留近 2.5 年月營收
MAX_DIV = 10             # 保留近 10 年股利


def pick(row, *names):
    """OpenAPI 的中文欄位名偶爾會微調，所以逐一嘗試"""
    for n in names:
        if n in row and str(row[n]).strip() not in ("", "--"):
            return row[n]
    return None


def fetch(path, offline):
    if offline:
        d = read_json(os.path.join(offline, path + ".json"))
        return (d, None) if d is not None else (None, f"缺少樣本檔 {path}.json")
    return get_json(f"{OPENAPI}/opendata/{path}")


def merge(existing, new, key, cap):
    """把 new 併進 existing，同 key 以 new 為準，依 key 排序後保留最後 cap 筆"""
    m = {x[key]: x for x in (existing or [])}
    for x in new:
        m[x[key]] = x
    out = sorted(m.values(), key=lambda x: x[key])
    return out[-cap:]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", metavar="DIR", help="讀本機樣本檔測試")
    args = ap.parse_args()
    off = args.offline

    prev = read_json(FUND_PATH, {}) or {}
    stocks = prev.get("stocks", {})
    status = {}

    def S(code):
        return stocks.setdefault(code, {})

    # ── 1. 公司基本資料（產業別）
    rows, err = fetch("t187ap03_L", off)
    if err:
        status["profile"] = "失敗：" + err
        log(f"⚠ 公司基本資料失敗：{err}")
    else:
        n = 0
        for row in rows:
            code = str(pick(row, "公司代號", "Code") or "").strip()
            if not code:
                continue
            s = S(code)
            s["name"] = str(pick(row, "公司簡稱", "Name") or s.get("name", "")).strip()
            ind = str(pick(row, "產業別") or "").strip()
            if ind:
                s["ind"] = ind
                s["indName"] = INDUSTRY.get(ind, ind)
            sh = num(pick(row, "已發行普通股數或TDR原股發行股數", "已發行普通股數"))
            if sh:
                s["shares"] = sh
            n += 1
        status["profile"] = f"成功 {n} 檔"
        log(f"✓ 公司基本資料 {n} 檔")

    # ── 2. 綜合損益表（季）
    inc = {}
    rows, err = fetch("t187ap06_L_ci", off)
    if err:
        status["income"] = "失敗：" + err
        log(f"⚠ 綜合損益表失敗：{err}")
    else:
        for row in rows:
            code = str(pick(row, "公司代號") or "").strip()
            y, q = pick(row, "年度"), pick(row, "季別")
            if not code or not y or not q:
                continue
            key = f"{str(y).strip()}Q{str(q).strip()}"
            inc[(code, key)] = {
                "q": key,
                "rev": r(num(pick(row, "營業收入")), 0),
                "gross": r(num(pick(row, "營業毛利（毛損）淨額", "營業毛利（毛損）")), 0),
                "op": r(num(pick(row, "營業利益（損失）")), 0),
                "pre": r(num(pick(row, "稅前淨利（淨損）")), 0),
                "net": r(num(pick(row, "本期淨利（淨損）")), 0),
                "eps": num(pick(row, "基本每股盈餘（元）")),
            }
        log(f"✓ 綜合損益表 {len(inc)} 筆")
        status["income"] = f"成功 {len(inc)} 筆"

    # ── 3. 營益分析（季，直接給比率）
    rows, err = fetch("t187ap17_L", off)
    if err:
        status["margin"] = "失敗：" + err
        log(f"⚠ 營益分析失敗：{err}")
    else:
        n = 0
        for row in rows:
            code = str(pick(row, "公司代號") or "").strip()
            y, q = pick(row, "年度"), pick(row, "季別")
            if not code or not y or not q:
                continue
            key = f"{str(y).strip()}Q{str(q).strip()}"
            d = inc.setdefault((code, key), {"q": key})
            d["gm"] = num(pick(row, "毛利率(%)"))
            d["opm"] = num(pick(row, "營業利益率(%)"))
            d["prm"] = num(pick(row, "稅前純益率(%)"))
            d["npm"] = num(pick(row, "稅後純益率(%)"))
            n += 1
        log(f"✓ 營益分析 {n} 筆")
        status["margin"] = f"成功 {n} 筆"

    # 併進各檔的季度序列
    byq = {}
    for (code, _), d in inc.items():
        byq.setdefault(code, []).append(d)
    for code, lst in byq.items():
        S(code)["q"] = merge(S(code).get("q"), lst, "q", MAX_QUARTERS)

    # ── 4. 月營收
    rows, err = fetch("t187ap05_P", off)
    if err:
        status["revenue"] = "失敗：" + err
        log(f"⚠ 月營收失敗：{err}")
    else:
        bym = {}
        for row in rows:
            code = str(pick(row, "公司代號") or "").strip()
            y, m = pick(row, "資料年月", "年度"), pick(row, "月份", "月")
            if not code:
                continue
            ym = str(pick(row, "資料年月") or "").strip()
            if len(ym) == 5 and ym.isdigit():                 # 11506
                key = f"{int(ym[:3]) + 1911}-{ym[3:]}"
            elif y and m:
                yy = int(str(y).strip())
                yy = yy + 1911 if yy < 1000 else yy
                key = f"{yy}-{int(str(m).strip()):02d}"
            else:
                continue
            bym.setdefault(code, []).append({
                "m": key,
                "rev": r(num(pick(row, "營業收入-當月營收")), 0),
                "yoy": num(pick(row, "營業收入-去年同月增減(%)")),
                "mom": num(pick(row, "營業收入-上月比較增減(%)")),
                "cum": r(num(pick(row, "累計營業收入-當月累計營收")), 0),
                "cumYoy": num(pick(row, "累計營業收入-前期比較增減(%)")),
            })
        for code, lst in bym.items():
            S(code)["m"] = merge(S(code).get("m"), lst, "m", MAX_MONTHS)
        log(f"✓ 月營收 {sum(len(v) for v in bym.values())} 筆")
        status["revenue"] = f"成功 {len(bym)} 檔"

    # ── 5. 股利
    rows, err = fetch("t187ap45_L", off)
    if err:
        status["dividend"] = "失敗：" + err
        log(f"⚠ 股利失敗：{err}")
    else:
        byd = {}
        for row in rows:
            code = str(pick(row, "公司代號") or "").strip()
            if not code:
                continue
            y = str(pick(row, "股利所屬年度", "股利所屬年(季)度", "所屬年度", "年度") or "").strip()
            if not y:
                continue
            cash = num(pick(row, "盈餘分配之現金股利(元/股)", "現金股利(元/股)"))
            cap = num(pick(row, "法定盈餘公積、資本公積發放之現金(元/股)")) or 0
            stk = num(pick(row, "盈餘轉增資配股(元/股)", "股票股利(元/股)")) or 0
            stk2 = num(pick(row, "法定盈餘公積、資本公積轉增資配股(元/股)")) or 0
            byd.setdefault(code, []).append({
                "y": y,
                "cash": r((cash or 0) + cap, 4),
                "stock": r(stk + stk2, 4),
            })
        for code, lst in byd.items():
            S(code)["div"] = merge(S(code).get("div"), lst, "y", MAX_DIV)
        log(f"✓ 股利 {len(byd)} 檔")
        status["dividend"] = f"成功 {len(byd)} 檔"

    if not stocks:
        sys.exit("❌ 一筆都沒抓到，不覆寫既有資料")

    write_json(FUND_PATH, {
        "updatedAt": datetime.now(TAIPEI).isoformat(timespec="seconds"),
        "sources": status,
        "stocks": stocks,
    })
    log(f"→ px-cache/fundamentals.json（{len(stocks)} 檔，不進 git）")


if __name__ == "__main__":
    main()
