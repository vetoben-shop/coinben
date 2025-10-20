/********************************************************************
 * trade.js  —  Bitget AutoTrade Server (COMMONJS)
 * --------------------------------------------------------------
 * [MOD] productType/marginMode 패스스루 유지
 * [MOD] quantity 별칭 지원 (quantity || size || quote → Bitget size) 유지
 * [MOD] Bitget v2 서명(ACCESS-SIGN) 유지
 * [FIX] reduceOnly를 Bitget 요청 본문에서 **항상 제외**  ← 문제 원인 해결
 * [MOD] mix/spot 라우트 await/에러 처리 보강 유지
 ********************************************************************/

require("dotenv").config(); /* [MOD] 환경변수 지원 */
const fs = require("fs");
const path = require("path");
const express = require("express");
const cors = require("cors");
const crypto = require("crypto");
const fetch = global.fetch || require("node-fetch");
const { PolicyGate } = require("./gate");

/* ======================= Config Loader ======================= */
const CONFIG_PATH = path.join(__dirname, "config.json");
function loadConfig() {
  const base = JSON.parse(fs.readFileSync(CONFIG_PATH, "utf-8"));
  if (process.env.TRADE_PORT) base.trade_port = Number(process.env.TRADE_PORT);
  if (process.env.DRY_RUN)
    base.dry_run = String(process.env.DRY_RUN).toLowerCase() === "true";
  if (process.env.KILL_SWITCH)
    base.kill_switch = String(process.env.KILL_SWITCH).toLowerCase() === "true";
  if (process.env.WHITELIST)
    base.whitelist = process.env.WHITELIST.split(",").map((s) => s.trim());
  if (process.env.API_KEY) base.api_key = process.env.API_KEY;
  if (process.env.API_SECRET) base.api_secret = process.env.API_SECRET;
  if (process.env.PASSPHRASE) base.passphrase = process.env.PASSPHRASE;
  return base;
}
let config = loadConfig();

/* ======================= App Bootstrap ======================= */
const app = express();
app.use(express.json());
app.use(
  cors({
    origin: (origin, cb) => {
      const wl = config?.whitelist || [];
      if (!origin || wl.includes("*") || wl.includes(origin))
        return cb(null, true);
      return cb(new Error("FORBIDDEN_ORIGIN"), false);
    },
  }),
);

const gate = new PolicyGate(config);
let lastActivityAt = Date.now();

/* ======================= Bitget Sign v2 ======================= */
function signV2({ timestamp, method, pathOnly, bodyStr, secret }) {
  const prehash =
    String(timestamp) + method.toUpperCase() + pathOnly + (bodyStr || "");
  return crypto.createHmac("sha256", secret).update(prehash).digest("base64");
}
async function bitgetPrivateCall({ method = "POST", pathOnly, bodyObj }) {
  const baseUrl = "https://api.bitget.com";
  const url = baseUrl + pathOnly;
  const bodyStr = bodyObj ? JSON.stringify(bodyObj) : "";
  const t = Date.now().toString();
  const headers = {
    "Content-Type": "application/json",
    "ACCESS-KEY": config.api_key,
    "ACCESS-PASSPHRASE": config.passphrase,
    "ACCESS-TIMESTAMP": t,
    "ACCESS-SIGN": signV2({
      timestamp: t,
      method,
      pathOnly,
      bodyStr,
      secret: config.api_secret,
    }),
  };
  const r = await fetch(url, { method, headers, body: bodyStr });
  const text = await r.text();
  let json;
  try {
    json = JSON.parse(text);
  } catch {
    json = { raw: text };
  }
  if (!r.ok) {
    const err = new Error(`Bitget HTTP ${r.status}`);
    err.status = r.status;
    err.payload = json;
    throw err;
  }
  return json;
}

/* ======================= Health ======================= */
app.get("/health", (req, res) => {
  const now = Date.now();
  const uptime = ((now - (gate.startedAt || now)) / 1000).toFixed(
    1,
  ); /* [MOD] NaN 방지 */
  const snapshotAge = now - (gate.lastSnapshotAt || 0);
  res.json({
    ok: true,
    type: "trade",
    uptime_s: uptime,
    safeMode: gate.safeMode,
    lastSnapshotAge_ms: snapshotAge,
    lastActivityAt,
  });
});

/* ======================= MIX: Market Order ======================= */
app.post("/mix/market-order", async (req, res) => {
  try {
    const {
      symbol,
      side,
      /* [MOD] Bitget 필드 패스스루 */
      productType /* e.g., "USDT-FUTURES" */,
      marginMode /* "crossed" | "isolated" */,
      /* [MOD] 수량 별칭 지원 */
      quantity /* 우선순위 1 */,
      size /* 우선순위 2 */,
      quote /* 우선순위 3 (기존 호환) */,
      /* 기존 플래그 */
      intent /* "open" | "close" */,
      reduceOnly /* boolean - [FIX] 본문 전송에서 항상 제외 */,
      dryRun,
    } = req.body || {};

    if (!symbol || !side) {
      return res.status(400).json({ ok: false, error: "symbol/side required" });
    }

    /* [MOD] 수량 통일: Bitget v2는 size 필드 사용 */
    const normalizedSize = quantity ?? size ?? quote;
    if (
      normalizedSize === undefined ||
      normalizedSize === null ||
      Number(normalizedSize) <= 0
    ) {
      return res
        .status(400)
        .json({ ok: false, error: "quantity/size/quote required (>0)" });
    }

    /* [MOD] Bitget 필수 필드 기본값 */
    const _productType = productType || "USDT-FUTURES";
    const _marginMode = marginMode || "crossed"; /* 기본: 교차 */

    gate.lastSnapshotAt = Date.now();
    lastActivityAt = Date.now();

    /* 정책 검사 */
    const decision = await gate.canExecute({
      symbol,
      side,
      notional: normalizedSize,
      reduceOnly,
      intent,
    });
    console.log("[MIX] decision =", decision);
    if (!decision.allow) {
      return res
        .status(400)
        .json({ ok: false, decision, message: "Order blocked by PolicyGate" });
    }

    /* 드라이런 */
    if (dryRun || config.dry_run) {
      return res.json({
        ok: true,
        dryRun: true,
        gate_result: decision.reason_code || "pass",
        symbol,
        side,
        productType: _productType,
        marginMode: _marginMode,
        size: Number(normalizedSize),
        reduceOnly: !!reduceOnly,
        intent: intent || "open",
      });
    }

    /* [FIX] Bitget place-order 본문 — reduceOnly **항상 제외** */
    const bodyObj = {
      symbol,
      productType: _productType,
      marginMode: _marginMode,
      marginCoin: "USDT",
      size: Number(normalizedSize),
      side, // "buy" | "sell"
      orderType: "market",
      tradeSide: intent === "close" ? "close" : "open",
      // reduceOnly: (전송 금지) ← Bitget 40017 방지
    };

    const apiResp = await bitgetPrivateCall({
      method: "POST",
      pathOnly: "/api/v2/mix/order/place-order",
      bodyObj,
    });

    res.json({ ok: true, dryRun: false, result: apiResp });
  } catch (err) {
    console.error("[mix/market-order] error:", err);
    res
      .status(err.status || 500)
      .json({ ok: false, error: err.message, payload: err.payload });
  }
});

/* ======================= SPOT: Market Order ======================= */
app.post("/spot/market-order", async (req, res) => {
  try {
    const { symbol, side, quote, quantity, size, dryRun } = req.body || {};
    if (!symbol || !side)
      return res.status(400).json({ ok: false, error: "symbol/side required" });

    gate.lastSnapshotAt = Date.now();
    lastActivityAt = Date.now();

    /* [MOD] 수량 통일 */
    const normalizedSize = quantity ?? size ?? quote;
    if (
      normalizedSize === undefined ||
      normalizedSize === null ||
      Number(normalizedSize) <= 0
    ) {
      return res
        .status(400)
        .json({ ok: false, error: "quantity/size/quote required (>0)" });
    }

    const decision = await gate.canExecute({
      symbol,
      side,
      notional: normalizedSize,
    });
    if (!decision.allow) {
      return res
        .status(400)
        .json({ ok: false, decision, message: "Order blocked by PolicyGate" });
    }

    if (dryRun || config.dry_run) {
      return res.json({
        ok: true,
        dryRun: true,
        gate_result: decision.reason_code || "pass",
        symbol,
        side,
        size: Number(normalizedSize),
      });
    }

    const bodyObj = {
      symbol,
      side,
      size: Number(normalizedSize),
      orderType: "market",
    };

    const apiResp = await bitgetPrivateCall({
      method: "POST",
      pathOnly: "/api/v2/spot/order/place-order",
      bodyObj,
    });

    res.json({ ok: true, dryRun: false, result: apiResp });
  } catch (err) {
    console.error("[spot/market-order] error:", err);
    res
      .status(err.status || 500)
      .json({ ok: false, error: err.message, payload: err.payload });
  }
});

/* ======================= Listen ======================= */
const PORT = config?.trade_port || 8789;
app.listen(PORT, () => console.log(`[trade] Trade server running on :${PORT}`));
