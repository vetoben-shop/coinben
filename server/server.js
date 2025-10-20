// =====================================================
// gate.js — Policy Gate (COMMONJS)
// =====================================================
// [MOD] 강화: 실제 정책/속도제한/수익자보호 로직 복원
// [MOD] trade/server 양쪽에서 동일 구현 사용
/* eslint-disable no-unused-vars */

const crypto = require("crypto");

// -------------------- RateLimiter --------------------
class RateLimiter {
  constructor(maxPerSec = 5) {
    this.maxPerSec = Number(maxPerSec) || 5;
    this.bucket = 0;
    this.ts = 0;
  }
  allow() {
    const now = Math.floor(Date.now() / 1000);
    if (this.ts !== now) {
      this.ts = now;
      this.bucket = 0;
    }
    if (this.bucket >= this.maxPerSec) return false;
    this.bucket++;
    return true;
  }
}

// -------------------- PolicyGate ---------------------
class PolicyGate {
  constructor(config) {
    this.config = config || {};
    this.version = `pg-${new Date().toISOString()}`;
    this.symbolHalt = new Set();
    this.globalHalt = false;
    this.safeMode = false;
    this.rateLimiter = new RateLimiter(this.config?.limits?.rate_per_sec || 5);
    this.lastSnapshotAt = Date.now();
    this.startedAt = Date.now(); // [MOD] uptime 계산용
  }

  updateConfig(newCfg) {
    this.config = newCfg || {};
    this.version = `pg-${new Date().toISOString()}`;
    this.rateLimiter = new RateLimiter(this.config?.limits?.rate_per_sec || 5);
  }

  setGlobalHalt(on) {
    this.globalHalt = !!on;
  }
  setSymbolHalt(sym, on) {
    const S = String(sym || "").toUpperCase();
    if (!S) return;
    if (on) this.symbolHalt.add(S);
    else this.symbolHalt.delete(S);
  }
  setSafeMode(on) {
    this.safeMode = !!on;
  }

  policySnapshot() {
    return {
      version: this.version,
      safeMode: this.safeMode,
      globalHalt: this.globalHalt,
      haltedSymbols: Array.from(this.symbolHalt),
      limits: this.config?.limits || {},
      guards: this.config?.guards || {},
      ttl_ms: 3000,
      asof: Date.now(),
    };
  }

  // ---------------- Core Decision ----------------
  async canExecute(order = {}) {
    this.lastSnapshotAt = Date.now();

    /* [MOD] Empty decision PASS fix */
    if (!order || Object.keys(order).length === 0) {
      return this.allow("EMPTY_ORDER_PASS");
    }

    // [MOD] Kill-switch
    if (this.config?.kill_switch) {
      if (this._isCloseOnly(order)) return this.allow("KILL_SWITCH_CLOSE_ONLY");
      return this.deny("KILL_SWITCH", "Global kill switch enabled");
    }

    // [MOD] Global / symbol halts
    if (this.globalHalt && !this._isCloseOnly(order)) {
      return this.deny("GLOBAL_HALT", "Trading globally halted");
    }
    const sym = String(order.symbol || "").toUpperCase();
    if (sym && this.symbolHalt.has(sym) && !this._isCloseOnly(order)) {
      return this.deny("SYMBOL_HALT", `Symbol ${sym} halted`);
    }

    // [MOD] Rate limiting
    if (!this.rateLimiter.allow()) {
      return this.deny("RATE_LIMIT", "Too many requests per second");
    }

    // [MOD] Safe mode → close only
    if (this.safeMode && !this._isCloseOnly(order)) {
      return this.deny("SAFE_MODE", "Safe mode: close-only");
    }

    // [MOD] Staleness guard
    const maxStale = Number(this.config?.guards?.max_staleness_ms || 0);
    if (maxStale > 0) {
      const age = Date.now() - (this.lastSnapshotAt || 0);
      if (age > maxStale && !this._isCloseOnly(order)) {
        return this.deny("DATA_STALE", `Snapshot stale: ${age}ms`);
      }
    }

    // [MOD] Notional limit (rough)
    const notional = Number(order.notional || order.quote || 0);
    const maxOrder = Number(this.config?.limits?.max_order_usdt || 0);
    if (
      maxOrder > 0 &&
      notional > 0 &&
      notional > maxOrder &&
      !this._isCloseOnly(order)
    ) {
      return this.deny("MAX_ORDER", `Notional ${notional} > ${maxOrder}`);
    }

    // [MOD] If reduce-only or intent=close → always permit
    if (this._isCloseOnly(order)) {
      return this.allow("CLOSE_ONLY");
    }

    // [MOD] Default allow
    return this.allow("PASS");
  }

  allow(reason = "PASS") {
    return {
      allow: true,
      reason_code: reason,
      policy_version: this.version,
      expires_at: Date.now() + 3000,
    };
  }

  deny(code, detail) {
    return {
      allow: false,
      reason_code: code,
      reason_detail: detail,
      policy_version: this.version,
      denied_at: Date.now(),
    };
  }

  _isCloseOnly(order) {
    return (
      !!order.reduceOnly || order.closeOnly === true || order.intent === "close"
    );
  }
}

module.exports = { PolicyGate };
