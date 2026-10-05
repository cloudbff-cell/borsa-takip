"""Alpaca ile kural tabanlı otomatik işlem.

Varsayılan: SANAL PARA (paper). Gerçek hesaba geçmek için ALPACA_PAPER=false gerekir.
Emir mantığı:
  Giriş  : Stratejik aday listesindeki hisse, fiyat alım bölgesine girdiğinde (seans içi),
           stop ve kâr al emirleriyle birlikte (bracket) piyasa emri.
  Boyut  : (sermaye x RISK_PER_TRADE) / (giriş - stop), tek pozisyon en fazla MAX_POSITION_PCT.
  Çıkış  : stop / kâr al emri, kapanışta teyitli SAT sinyali (EXIT_SIGNALS) ya da bilanço öncesi.
  Koruma : en fazla MAX_POSITIONS pozisyon, günlük kayıp limiti, hisse başına günde tek giriş.
"""
import json
import math
import os
from datetime import datetime

import requests

from . import config as C
from . import data as D

PAPER_URL = "https://paper-api.alpaca.markets"
LIVE_URL = "https://api.alpaca.markets"


class Alpaca:
    def __init__(self):
        self.paper = os.getenv("ALPACA_PAPER", "true").lower() != "false"
        self.base = PAPER_URL if self.paper else LIVE_URL
        self.h = {"APCA-API-KEY-ID": os.getenv("ALPACA_KEY_ID", ""),
                  "APCA-API-SECRET-KEY": os.getenv("ALPACA_SECRET_KEY", "")}

    def _r(self, method, path, **kw):
        r = requests.request(method, self.base + path, headers=self.h, timeout=20, **kw)
        if not r.ok:
            raise RuntimeError(f"Alpaca {method} {path}: {r.status_code} {r.text[:300]}")
        return r.json() if r.text else {}

    def account(self):
        return self._r("GET", "/v2/account")

    def positions(self):
        return self._r("GET", "/v2/positions")

    def open_orders(self):
        return self._r("GET", "/v2/orders", params={"status": "open", "nested": "true", "limit": 500})

    def bracket_buy(self, sym, qty, stop, tp, client_id):
        return self._r("POST", "/v2/orders", json={
            "symbol": sym, "qty": str(qty), "side": "buy", "type": "market", "time_in_force": "gtc",
            "order_class": "bracket", "client_order_id": client_id,
            "take_profit": {"limit_price": f"{tp:.2f}"}, "stop_loss": {"stop_price": f"{stop:.2f}"}})

    def replace_stop(self, order_id, stop):
        return self._r("PATCH", f"/v2/orders/{order_id}", json={"stop_price": f"{stop:.2f}"})

    def cancel(self, order_id):
        return self._r("DELETE", f"/v2/orders/{order_id}")

    def close_position(self, sym):
        return self._r("DELETE", f"/v2/positions/{sym}")


def _legs(orders):
    """Açık emirleri düzleştir (bracket bacakları dahil)."""
    out = []
    for o in orders:
        out.append(o)
        out += o.get("legs") or []
    return out


def _log(entry):
    p = D.CACHE / "trades.json"
    log = json.loads(p.read_text()) if p.exists() else []
    entry["ts"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    log.append(entry)
    p.write_text(json.dumps(log[-500:]))


def run(summary, by, mode, state, client=None):
    """Mesaj listesi ve portföy özeti döner. mode: intraday | post"""
    if os.getenv("TRADING_ENABLED", "").lower() != "true" and client is None:
        return [], None
    api = client or Alpaca()
    msgs = []
    acct = api.account()
    equity, last_eq = float(acct["equity"]), float(acct["last_equity"])
    bp = float(acct.get("buying_power", equity))
    day_pl = (equity - last_eq) / last_eq if last_eq else 0.0
    pos = {p["symbol"]: p for p in api.positions()}
    orders = api.open_orders()
    flat = _legs(orders)
    tag = "🧪 SANAL" if getattr(api, "paper", True) else "💵 GERÇEK"
    today = summary["lastBar"]

    # 1) Çıkışlar: kapanışta teyitli SAT sinyali veya bilanço öncesi (post modunda; emir ertesi açılışta gerçekleşir)
    if mode == "post":
        for sym, p in pos.items():
            s = by.get(sym)
            if not s:
                continue
            reason = next((g["name"] for g in s["signals"] if g["code"] in C.EXIT_SIGNALS), None)
            d2e = s["fund"]["daysToEarnings"]
            if not reason and C.EXIT_BEFORE_EARNINGS and d2e is not None and 0 <= d2e <= 1:
                reason = f"Bilanço {s['fund']['nextEarnings']} öncesi"
            if not reason:
                continue
            for o in flat:
                if o["symbol"] == sym and o["side"] == "sell":
                    try:
                        api.cancel(o["id"])
                    except RuntimeError as e:
                        print(e)
            try:
                api.close_position(sym)
                pl = float(p.get("unrealized_pl", 0))
                msgs.append(f"{tag} 🔴 <b>{sym}</b> SATIŞ emri ({reason}) · {p['qty']} adet · Gerçekleşmemiş K/Z ${pl:,.2f}")
                _log({"sym": sym, "side": "sell", "qty": p["qty"], "reason": reason})
            except RuntimeError as e:
                msgs.append(f"⚠️ {sym} kapatılamadı: {e}")

    # 2) Stopu başa baş noktasına çek (Hedef 1 görüldüyse)
    if C.BREAKEVEN_AT_T1 and mode == "intraday":
        for sym, p in pos.items():
            entry, price = float(p["avg_entry_price"]), float(p["current_price"])
            s = by.get(sym)
            if not s or not s.get("atr"):
                continue
            if price >= entry + C.T1_ATR * s["atr"]:
                for o in flat:
                    if o["symbol"] == sym and o["type"] in ("stop", "stop_limit") and o["side"] == "sell" \
                            and float(o["stop_price"]) < entry:
                        try:
                            api.replace_stop(o["id"], entry)
                            msgs.append(f"{tag} 🔒 <b>{sym}</b> Hedef 1 görüldü, stop giriş fiyatına çekildi (${entry:,.2f})")
                            _log({"sym": sym, "side": "stop_to_be", "stop": entry})
                        except RuntimeError as e:
                            print(e)

    # 3) Girişler (yalnız seans içinde)
    portfolio = {"equity": equity, "dayPL": day_pl * 100, "paper": getattr(api, "paper", True),
                 "positions": [{"sym": k, "qty": v["qty"], "entry": float(v["avg_entry_price"]),
                                "price": float(v["current_price"]), "pl": float(v["unrealized_pl"]),
                                "plpc": float(v["unrealized_plpc"]) * 100} for k, v in pos.items()]}
    if mode != "intraday":
        return msgs, portfolio
    if day_pl <= -C.DAILY_LOSS_LIMIT:
        key = f"losslimit|{today}"
        if key not in state["sent"]:
            state["sent"][key] = today
            msgs.append(f"{tag} ⛔ Günlük kayıp {day_pl*100:.2f}% – limit aşıldı, bugün yeni alım yapılmayacak.")
        return msgs, portfolio

    pending_buys = {o["symbol"] for o in orders if o["side"] == "buy"}
    slots = C.MAX_POSITIONS - len(pos) - len(pending_buys - set(pos))
    for t in summary["picks"]:
        if slots <= 0:
            break
        s = by[t]
        pl = s.get("plan")
        if not pl or t in pos or t in pending_buys:
            continue
        key = f"entry|{today}|{t}"
        if key in state["sent"]:
            continue
        price = s["price"]
        if not (pl["zoneLow"] <= price <= pl["zoneHigh"] * 1.002):
            continue
        stop = pl["stop"]
        tp = pl[C.TAKE_PROFIT]
        risk_ps = price - stop
        if risk_ps <= 0:
            continue
        qty = math.floor(min(equity * C.RISK_PER_TRADE / risk_ps,
                             equity * C.MAX_POSITION_PCT / price, bp * 0.95 / price))
        if qty < 1:
            continue
        state["sent"][key] = today
        try:
            api.bracket_buy(t, qty, stop, tp, f"bt-{today}-{t}")
            slots -= 1
            bp -= qty * price
            msgs.append(f"{tag} 🟢 <b>{t}</b> ALIŞ emri · {qty} adet ~${price:,.2f} (≈${qty*price:,.0f})\n"
                        f"   Stop ${stop:,.2f} · risk ≈${qty*risk_ps:,.0f} = sermayenin %{qty*risk_ps/equity*100:.1f} · Kâr al ${tp:,.2f}\n"
                        f"   Gerekçe: puan {s['score']:.0f}, {s['trend']} trend, fiyat alım bölgesinde")
            _log({"sym": t, "side": "buy", "qty": qty, "price": price, "stop": stop, "tp": tp})
        except RuntimeError as e:
            msgs.append(f"⚠️ {t} alış emri verilemedi: {e}")
    return msgs, portfolio
