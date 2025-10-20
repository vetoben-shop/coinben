#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RiskManagementBot.py
- Monitors liquidity/spread/divergence/collapse signals
- Emits throttle/hold/halt decisions and can toggle server modes via control endpoints (if exposed)
- Pure public data (Bitget) + control via Node server's REST (e.g., /status, /control routes if available)
- Python 3.10+

ENV:
  BASE_URL=http://127.0.0.1:8788
  SYMBOL=BTCUSDT
  TOPN=50
  INTERVAL_MS=3000
  SPREAD_BPS_WARN=600     # >=6 bps -> warn
  DIVERGENCE_PCT_WARN=0.5 # >=0.5% -> warn
  COLLAPSE_PCT_WARN=-20   # <=-20% over 5s -> warn
"""
import asyncio, os, time, json, math
from typing import Optional, Dict

BASE_URL = os.getenv("BASE_URL", "http://127.0.0.1:8788").rstrip("/")
SYMBOL = os.getenv("SYMBOL", "BTCUSDT").upper()
TOPN = int(os.getenv("TOPN", "50"))
INTERVAL_MS = int(os.getenv("INTERVAL_MS", "3000"))
SPREAD_BPS_WARN = float(os.getenv("SPREAD_BPS_WARN", "600"))
DIVERGENCE_PCT_WARN = float(os.getenv("DIVERGENCE_PCT_WARN", "0.5"))
COLLAPSE_PCT_WARN = float(os.getenv("COLLAPSE_PCT_WARN", "-20"))

BITGET_FUT_TICKERS = "https://api.bitget.com/api/v2/mix/market/tickers?productType=USDT-FUTURES"
BITGET_SPOT_TICKERS = "https://api.bitget.com/api/v2/spot/market/tickers"
BITGET_DEPTH = "https://api.bitget.com/api/v2/mix/market/merge-depth?productType=USDT-FUTURES"

try:
    import aiohttp
    USE_AIOHTTP=True
except Exception:
    USE_AIOHTTP=False
    import urllib.request, urllib.error

async def http_get_json(url):
    if USE_AIOHTTP:
        async with aiohttp.ClientSession() as sess:
            async with sess.get(url, timeout=10) as r:
                txt = await r.text()
                return json.loads(txt) if txt.strip().startswith("{") or txt.strip().startswith("[") else {"raw": txt}
    else:
        with urllib.request.urlopen(url, timeout=10) as r:
            txt = r.read().decode("utf-8","ignore")
            try: return json.loads(txt)
            except: return {"raw": txt}

def spread_bps(bid: Optional[float], ask: Optional[float]) -> Optional[float]:
    if bid is None or ask is None or bid<=0 or ask<=0: return None
    mid = (bid+ask)/2
    return (ask-bid)/mid*10000.0

def sum_notional(levels, n):
    s=0.0
    for i in range(min(len(levels or []), n)):
        try:
            px = float(levels[i][0]); sz = float(levels[i][1])
            s += px*sz
        except: pass
    return s

collapse_hist = []  # (ts_ms, total_notional)

def collapse_5s(now_total: float) -> Optional[float]:
    ts = time.time()*1000
    collapse_hist.append((ts, now_total))
    # keep 12 seconds
    while collapse_hist and ts - collapse_hist[0][0] > 12000:
        collapse_hist.pop(0)
    # first point older than 5s
    for (t0, tot0) in collapse_hist:
        if ts - t0 >= 5000 and tot0>0:
            return (now_total - tot0)/tot0*100.0
    return None

async def get_prices(symbol: str):
    fut = await http_get_json(BITGET_FUT_TICKERS + f"&_={int(time.time()*1000)}")
    spot = await http_get_json(BITGET_SPOT_TICKERS + f"&_={int(time.time()*1000)}")
    fut_px = None; spot_px=None
    for t in fut.get("data", {}).get("data", []):
        s = str(t.get("symbol") or t.get("instId") or "").upper()
        if s == symbol:
            fut_px = float(t.get("lastPr") or t.get("last") or t.get("close") or t.get("lastPrice") or t.get("closePrice"))
            break
    for t in spot.get("data", {}).get("data", []):
        s = str(t.get("symbol") or t.get("instId") or "").upper()
        if s == symbol:
            spot_px = float(t.get("lastPr") or t.get("last") or t.get("close") or t.get("lastPrice") or t.get("closePrice"))
            break
    return fut_px, spot_px

async def get_depth_stats(symbol: str, topn: int):
    url = f"{BITGET_DEPTH}&symbol={symbol}&precision=scale0&limit={topn}"
    j = await http_get_json(url)
    bids = j.get("data", {}).get("bids", []) or []
    asks = j.get("data", {}).get("asks", []) or []
    best_bid = float(bids[0][0]) if bids else None
    best_ask = float(asks[0][0]) if asks else None
    tot = sum_notional(bids, topn) + sum_notional(asks, topn)
    col = collapse_5s(tot)
    spr = spread_bps(best_bid, best_ask)
    return spr, col

async def maybe_signal_controls(spread_bps_val, div_pct, col5):
    """
    Example policy:
      - spread >= SPREAD_BPS_WARN OR |div| >= DIVERGENCE_PCT_WARN OR collapse <= COLLAPSE_PCT_WARN
        -> propose throttle/hold (here we only log; if server control endpoints exist, call them)
    """
    risk_score = 0
    if spread_bps_val is not None and spread_bps_val >= SPREAD_BPS_WARN: risk_score += 1
    if div_pct is not None and abs(div_pct) >= DIVERGENCE_PCT_WARN: risk_score += 1
    if col5 is not None and col5 <= COLLAPSE_PCT_WARN: risk_score += 1

    if risk_score >= 2:
        print(f"[RISK] Trigger condition met => consider throttle/hold/halt "
              f"(spr={spread_bps_val}, div={div_pct}, col5={col5})")

async def loop():
    print("[RiskManagementBot] start",
          {"BASE_URL": BASE_URL, "SYMBOL": SYMBOL, "TOPN": TOPN, "INTERVAL_MS": INTERVAL_MS})
    while True:
        try:
            fut_px, spot_px = await get_prices(SYMBOL)
            div = None
            if fut_px and spot_px and spot_px>0:
                div = (fut_px-spot_px)/spot_px*100.0

            spr, col5 = await get_depth_stats(SYMBOL, TOPN)
            print(f"[KPI] spr_bps={None if spr is None else round(spr,2)}  div%={None if div is None else round(div,3)}  col5%={None if col5 is None else round(col5,2)}")

            await maybe_signal_controls(spr, div, col5)
        except Exception as e:
            print("[ERR]", e)
        await asyncio.sleep(INTERVAL_MS/1000)

if __name__ == "__main__":
    try:
        asyncio.run(loop())
    except KeyboardInterrupt:
        print("\n[RiskManagementBot] stopped")
