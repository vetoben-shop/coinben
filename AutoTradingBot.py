#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AutoTradingBot.py
- Goal: Aggressive, quantity-maximizing spread scalper with hedge
- Integrates with existing Node server endpoints (no direct exchange signing here)
- 24/7 capable (run under pm2/systemd/screen/etc.)
- Python 3.10+
- Optional dependency: aiohttp (recommended). If not installed, fall back to urllib.

Environment variables (override defaults):
  BASE_URL=http://127.0.0.1:8788
  SYMBOL=BTCUSDT
  SIZE_TYPE=USDT                # USDT or COIN
  ORDER_SIZE=20                 # if USDT -> notional; if COIN -> qty
  SPREAD_PCT=0.5                # % target profit per round trip (net of fees target proxy)
  HEDGE_TRIGGER_PCT=0.8         # % adverse move from entry to trigger futures hedge
  REBUY_DROP_PCT=10             # % drop from last sell price to trigger re-buy
  LEVERAGE=3
  POS_MODE=isolated             # cross or isolated
  SAFE_MODE=off                 # on/off (soft gate inside the bot; Node server still enforces PolicyGate)
  SYMBOL_SCAN_INTERVAL_MS=1500  # polling interval
"""
import asyncio, os, json, time, random, math, sys
from typing import Optional, Dict

# ---- Config ----
BASE_URL = os.getenv("BASE_URL", "http://127.0.0.1:8788").rstrip("/")
SYMBOL = os.getenv("SYMBOL", "BTCUSDT").upper()
SIZE_TYPE = os.getenv("SIZE_TYPE", "USDT").upper()  # USDT or COIN
ORDER_SIZE = float(os.getenv("ORDER_SIZE", "20"))
SPREAD_PCT = float(os.getenv("SPREAD_PCT", "0.5"))         # % (e.g., 0.5 => 0.5%)
HEDGE_TRIGGER_PCT = float(os.getenv("HEDGE_TRIGGER_PCT", "0.8"))
REBUY_DROP_PCT = float(os.getenv("REBUY_DROP_PCT", "10"))
LEVERAGE = int(os.getenv("LEVERAGE", "3"))
POS_MODE = os.getenv("POS_MODE", "isolated").lower()       # isolated|cross
SAFE_MODE = os.getenv("SAFE_MODE", "off").lower()          # on|off
SCAN_MS = int(os.getenv("SYMBOL_SCAN_INTERVAL_MS", "1500"))

PRODUCT_TYPE = "USDT-FUTURES"  # v2 Bitget naming
HTTP_TIMEOUT = 10
MAX_RETRIES = 4

# ---- HTTP helpers (aiohttp preferred) ----
USE_AIOHTTP = True
try:
    import aiohttp
except Exception:
    USE_AIOHTTP = False
    import urllib.request, urllib.error

async def http_post_json(url: str, body: Dict) -> Dict:
    print(f"Sending request to {url} with body: {json.dumps(body)}")  # 요청 내용 출력
    
    if USE_AIOHTTP:
        backoff = 0.4
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                async with aiohttp.ClientSession() as sess:
                    async with sess.post(url, json=body, timeout=HTTP_TIMEOUT) as resp:
                        txt = await resp.text()
                        try:
                            data = json.loads(txt)
                        except Exception:
                            data = {"raw": txt}
                        print(f"Response from server: {json.dumps(data)}")  # 응답 내용 출력
                        return {"ok": (200 <= resp.status < 300), "status": resp.status, "data": data}
            except Exception as e:
                if attempt == MAX_RETRIES:
                    return {"ok": False, "error": str(e)}
                await asyncio.sleep(backoff)
                backoff *= 2.0
    else:
        # sync fallback wrapped for asyncio
        def _sync():
            req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"),
                                         headers={"Content-Type": "application/json"}, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
                    txt = r.read().decode("utf-8", "ignore")
                    try:
                        data = json.loads(txt)
                    except Exception:
                        data = {"raw": txt}
                    print(f"Response from server: {json.dumps(data)}")  # 응답 내용 출력
                    return {"ok": True, "status": r.status, "data": data}
            except urllib.error.HTTPError as e:
                return {"ok": False, "status": e.code, "error": e.read().decode("utf-8", "ignore")}
            except Exception as e:
                return {"ok": False, "error": str(e)}
        backoff = 0.4
        for attempt in range(1, MAX_RETRIES + 1):
            res = await asyncio.to_thread(_sync)
            if res.get("ok"): return res
            if attempt == MAX_RETRIES: return res
            await asyncio.sleep(backoff); backoff *= 2.0

async def http_get_json(url: str) -> Dict:
    if USE_AIOHTTP:
        backoff = 0.4
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                async with aiohttp.ClientSession() as sess:
                    async with sess.get(url, timeout=HTTP_TIMEOUT) as resp:
                        txt = await resp.text()
                        data = json.loads(txt) if txt.strip().startswith("{") or txt.strip().startswith("[") else {"raw": txt}
                        return {"ok": (200 <= resp.status < 300), "status": resp.status, "data": data}
            except Exception as e:
                if attempt == MAX_RETRIES:
                    return {"ok": False, "error": str(e)}
                await asyncio.sleep(backoff)
                backoff *= 2.0
    else:
        def _sync():
            try:
                with urllib.request.urlopen(url, timeout=HTTP_TIMEOUT) as r:
                    txt = r.read().decode("utf-8", "ignore")
                    try:
                        data = json.loads(txt)
                    except Exception:
                        data = {"raw": txt}
                    return {"ok": True, "status": r.status, "data": data}
            except urllib.error.HTTPError as e:
                return {"ok": False, "status": e.code, "error": e.read().decode("utf-8", "ignore")}
            except Exception as e:
                return {"ok": False, "error": str(e)}
        backoff = 0.4
        for attempt in range(1, MAX_RETRIES + 1):
            res = await asyncio.to_thread(_sync)
            if res.get("ok"): return res
            if attempt == MAX_RETRIES: return res
            await asyncio.sleep(backoff); backoff *= 2.0

# ---- Market data (Bitget public) ----
BITGET_FUT_TICKERS = "https://api.bitget.com/api/v2/mix/market/tickers?productType=USDT-FUTURES"
BITGET_SPOT_TICKERS = "https://api.bitget.com/api/v2/spot/market/tickers"

async def get_spot_price(sym: str) -> Optional[float]:
    res = await http_get_json(BITGET_SPOT_TICKERS + f"&_={int(time.time()*1000)}")
    if not res.get("ok"): return None
    for t in (res["data"].get("data") or []):
        s = str(t.get("symbol") or t.get("instId") or "").upper()
        if s == sym.upper():
            px = t.get("lastPr") or t.get("last") or t.get("close") or t.get("lastPrice") or t.get("closePrice")
            try:
                return float(px)
            except Exception:
                return None
    return None

async def get_fut_price(sym: str) -> Optional[float]:
    res = await http_get_json(BITGET_FUT_TICKERS + f"&_={int(time.time()*1000)}")
    if not res.get("ok"): return None
    for t in (res["data"].get("data") or []):
        s = str(t.get("symbol") or t.get("instId") or "").upper()
        if s == sym.upper():
            px = t.get("lastPr") or t.get("last") or t.get("close") or t.get("lastPrice") or t.get("closePrice")
            try:
                return float(px)
            except Exception:
                return None
    return None

# ---- State ----
class State:
    def __init__(self):
        self.last_buy_px: Optional[float] = None
        self.last_sell_px: Optional[float] = None
        self.hedge_side: Optional[str] = None  # 'long' or 'short' in futures context
        self.hedge_entry_px: Optional[float] = None
        self.hedge_open_ts: Optional[float] = None
        self.total_accumulated_coin: float = 0.0  # for display/log purpose

S = State()

# ---- Orders via Node server ----
async def place_spot_order(side: str, symbol: str, size_type: str, size: float):
    url = f"{BASE_URL}/spot/market-order"
    body = {"symbol": symbol, "side": side, "sizeType": size_type, "size": size}
    return await http_post_json(url, body)

async def place_futures_order(side: str, symbol: str, size_type: str, size: float, lev: int, margin_mode: str,
                              tpPct: Optional[float]=None, slPct: Optional[float]=None):
    url = f"{BASE_URL}/mix/market-order"
    body = {
        "productType": PRODUCT_TYPE,
        "symbol": symbol,
        "side": side,                      # 'buy' -> long, 'sell' -> short
        "sizeType": size_type,             # USDT|COIN
        "size": size,
        "leverage": lev,
        "marginMode": margin_mode,         # isolated|cross
        "tpPct": tpPct if tpPct is not None else None,
        "slPct": slPct if slPct is not None else None
    }
    return await http_post_json(url, body)

# ---- Strategy core ----
def pct_move(from_px: float, to_px: float) -> float:
    if from_px is None or to_px is None or from_px <= 0: return 0.0
    return (to_px - from_px) / from_px * 100.0

async def try_entry_spread_buy(current_px: float):
    # If we have a last_sell_px and price is 10% below it -> rebuy rule
    if S.last_sell_px is not None:
        drop = pct_move(S.last_sell_px, current_px)  # negative if dropped
        if drop <= -REBUY_DROP_PCT:
            res = await place_spot_order("buy", SYMBOL, SIZE_TYPE, ORDER_SIZE)
            ok = res.get("ok", False)
            print(f"[REB UY] px={current_px:.4f} drop={drop:.2f}% -> spot BUY -> {ok}")
            if ok:
                S.last_buy_px = current_px
                S.total_accumulated_coin += ORDER_SIZE if SIZE_TYPE == "COIN" else (ORDER_SIZE/current_px)
            return

    # Normal spread entry: if no position context, place a buy to start a cycle
    if S.last_buy_px is None and S.last_sell_px is None:
        res = await place_spot_order("buy", SYMBOL, SIZE_TYPE, ORDER_SIZE)
        ok = res.get("ok", False)
        print(f"[ENTRY] px={current_px:.4f} -> spot BUY -> {ok}")
        if ok:
            S.last_buy_px = current_px
            S.total_accumulated_coin += ORDER_SIZE if SIZE_TYPE == "COIN" else (ORDER_SIZE/current_px)

async def try_exit_spread_sell(current_px: float):
    # If we have a last_buy_px and profit target met -> sell
    if S.last_buy_px is not None:
        gain = pct_move(S.last_buy_px, current_px)
        if gain >= SPREAD_PCT:
            res = await place_spot_order("sell", SYMBOL, SIZE_TYPE, ORDER_SIZE)
            ok = res.get("ok", False)
            print(f"[TAKE] buy={S.last_buy_px:.4f} px={current_px:.4f} gain={gain:.2f}% -> spot SELL -> {ok}")
            if ok:
                S.last_sell_px = current_px
                S.last_buy_px = None

async def try_hedge(current_px: float, fut_px: Optional[float]):
    if SAFE_MODE == "on": 
        return
    if S.last_buy_px is not None:
        # Spot long exposure; price adverse? -> open SHORT hedge
        drop = pct_move(S.last_buy_px, current_px)
        if drop <= -HEDGE_TRIGGER_PCT and S.hedge_side is None:
            # Open hedge short
            res = await place_futures_order("sell", SYMBOL, SIZE_TYPE, ORDER_SIZE, LEVERAGE, POS_MODE)
            ok = res.get("ok", False)
            print(f"[HEDGE OPEN] SpotLong adverse {drop:.2f}% -> FUT SHORT -> {ok}")
            if ok:
                S.hedge_side = "short"
                S.hedge_entry_px = fut_px if fut_px else current_px
                S.hedge_open_ts = time.time()

    # close hedge: if recovered to <= 0.3% below buy OR time > 15 min OR hedge pnl positive
    if S.hedge_side == "short" and S.last_buy_px is not None:
        recov = pct_move(S.last_buy_px, current_px)  # negative -> still adverse
        elapsed = (time.time() - (S.hedge_open_ts or time.time()))
        should_close = (recov >= -0.3) or (elapsed >= 900)  # simplistic
        if should_close:
            # Close hedge by opening opposite (market) or using close-all endpoint if provided
            # Here: send "buy" to close short side
            res = await place_futures_order("buy", SYMBOL, SIZE_TYPE, ORDER_SIZE, LEVERAGE, POS_MODE)
            ok = res.get("ok", False)
            print(f"[HEDGE CLOSE] recov={recov:.2f}% elapsed={elapsed:.0f}s -> FUT LONG (close short) -> {ok}")
            if ok:
                S.hedge_side = None
                S.hedge_entry_px = None
                S.hedge_open_ts = None

async def main_loop():
    print("[AutoTradingBot] starting with config:")
    print(json.dumps({
        "BASE_URL": BASE_URL, "SYMBOL": SYMBOL, "SIZE_TYPE": SIZE_TYPE, "ORDER_SIZE": ORDER_SIZE,
        "SPREAD_PCT": SPREAD_PCT, "HEDGE_TRIGGER_PCT": HEDGE_TRIGGER_PCT,
        "REBUY_DROP_PCT": REBUY_DROP_PCT, "LEVERAGE": LEVERAGE, "POS_MODE": POS_MODE, "SAFE_MODE": SAFE_MODE
    }, indent=2))

    while True:
        try:
            spot_px = await get_spot_price(SYMBOL)
            fut_px  = await get_fut_price(SYMBOL)
            if spot_px is None:
                await asyncio.sleep(SCAN_MS/1000); continue

            # 1) Entry (first buy or rebuy rule from last sell -10%)
            await try_entry_spread_buy(spot_px)

            # 2) Exit (take spread profit)
            await try_exit_spread_sell(spot_px)

            # 3) Hedge management
            await try_hedge(spot_px, fut_px)

            # 4) heartbeat log (throttled)
            if random.random() < 0.1:
                print(f"[HB] spot={spot_px:.4f} fut={fut_px if fut_px else '-'} "
                      f"buy={S.last_buy_px} sell={S.last_sell_px} hedge={S.hedge_side} "
                      f"acc_coin≈{S.total_accumulated_coin:.6f}")
        except Exception as e:
            print("[ERR]", e)
        await asyncio.sleep(SCAN_MS/1000)

if __name__ == "__main__":
    try:
        if USE_AIOHTTP:
            asyncio.run(main_loop())
        else:
            # No aiohttp, still run (urllib fallback)
            asyncio.run(main_loop())
    except KeyboardInterrupt:
        print("\n[AutoTradingBot] stopped")
