"""Alpaca ile kural tabanlı otomatik işlem.

Varsayılan: SANAL PARA (paper). Gerçek hesaba geçmek için ALPACA_PAPER=false gerekir.
Emir mantığı:
  Giriş  : Seans içi her taramada, stratejik adaylar için alım bölgesinin üst sınırına LİMİTLİ
           alış emri (bağlı stop ve kâr al ile) konur. Fiyat bölgeye indiği an Alpaca emri
           gerçekleştirir; taramayı beklemez. Aday listesinden çıkan ya da bölgenin altına düşen
           hisselerin bekleyen emri iptal edilir, seviyesi değişenler güncellenir.
           Kapanışta gerçekleşmemiş giriş emirleri iptal edilir.
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

STATUS = {}

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

    def bracket_buy(self, sym, qty, limit, stop, tp, client_id):
        """Limitli alış + bağlı stop ve kâr al. GTC: bacaklar gece de korur; gerçekleşmeyen
        giriş emirleri kapanışta bot tarafından iptal edilir."""
        return self._r("POST", "/v2/orders", json={
            "symbol": sym, "qty": str(qty), "side": "buy", "type": "limit", "limit_price": f"{limit:.2f}",
            "time_in_force": "gtc", "order_class": "bracket", "client_order_id": client_id,
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


def decision_snapshot(s):
    """Emir anındaki puan ve gerekçeler (sonradan analiz için kalıcı kayıt)."""
    if not s:
        return {}
    fv = s.get("fund") or {}
    sea = s.get("season") or {}
    recent_buy = [r["name"] for r in s.get("recent", []) if r["side"] == "AL"]
    why = []
    kind = (s.get("plan") or {}).get("entryType")
    if kind == "sinyal" or (kind is None and recent_buy):
        why.append(f"Giriş türü: AL sinyali ({', '.join(recent_buy)}) + puan ≥ {C.ENTRY_SIGNAL_MIN_SCORE}")
    else:
        why.append(f"Giriş türü: sinyal yok, yüksek puan (≥ {C.ENTRY_TREND_MIN_SCORE}) + 20 EMA'ya geri çekilme")
    why.append(f"Trend {s.get('trend')}, RSI {s.get('rsi')}, SPY'a göre 3A {s.get('rs3m')}%")
    why += (fv.get("notes") or [])[:3]
    if sea.get("cur"):
        why.append(f"{sea.get('curName')} mevsimselliği: %{sea['cur']['win']:.0f} pozitif, ort. {sea['cur']['avg']:+.1f}%")
    return {"score": s.get("score"), "parts": s.get("parts"), "trend": s.get("trend"), "rsi": s.get("rsi"),
            "price": s.get("price"), "ema20": s.get("ema20"), "ema50": s.get("ema50"), "atr": s.get("atr"),
            "signals": recent_buy, "entryType": kind, "daysToEarnings": fv.get("daysToEarnings"), "why": why}


def _parts_text(snap):
    p = snap.get("parts") or {}
    return (f"Puan <b>{snap.get('score', 0):.0f}</b>/100 (teknik {p.get('teknik', 0)}/40 · göreceli {p.get('goreceli', 0):.0f}/20 · "
            f"temel {p.get('temel', 0)}/25 · mevsim {p.get('mevsim', 0):.0f}/15)")


def _log(entry):
    p = D.CACHE / "trades.json"
    log = json.loads(p.read_text()) if p.exists() else []
    entry["ts"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    log.append(entry)
    p.write_text(json.dumps(log[-500:]))


def run(summary, by, mode, state, client=None):
    """Mesaj listesi ve portföy özeti döner. mode: intraday | post"""
    STATUS["enabled"] = os.getenv("TRADING_ENABLED", "").lower() == "true"
    STATUS["keysSet"] = bool(os.getenv("ALPACA_KEY_ID")) and bool(os.getenv("ALPACA_SECRET_KEY"))
    if not STATUS["enabled"] and client is None:
        STATUS["result"] = "kapalı (TRADING_ENABLED=true değil)"
        return [], None
    api = client or Alpaca()
    msgs = []
    acct = api.account()
    STATUS.update({"result": "bağlandı", "paper": getattr(api, "paper", True), "accountStatus": acct.get("status")})
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
                _log({"sym": sym, "side": "sell", "qty": p["qty"], "reason": reason,
                      "pl": float(p.get("unrealized_pl", 0)), "decision": decision_snapshot(s)})
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
    def _lvl(sym, kind):
        for o in flat:
            if o["symbol"] == sym and o["side"] == "sell":
                if kind == "stop" and o.get("type") in ("stop", "stop_limit") and o.get("stop_price"):
                    return float(o["stop_price"])
                if kind == "tp" and o.get("type") == "limit" and o.get("limit_price"):
                    return float(o["limit_price"])
        return None

    portfolio = {"equity": equity, "lastEquity": last_eq, "dayPL": day_pl * 100, "dayPLusd": equity - last_eq,
                 "cash": float(acct.get("cash", 0) or 0), "buyingPower": float(acct.get("buying_power", 0) or 0),
                 "paper": getattr(api, "paper", True),
                 "pendingBuys": [{"sym": o["symbol"], "qty": o.get("qty"), "limit": float(o.get("limit_price") or 0)}
                                 for o in orders if o["side"] == "buy"],
                 "positions": [{"sym": k, "qty": v["qty"], "entry": float(v["avg_entry_price"]),
                                "price": float(v["current_price"]), "pl": float(v["unrealized_pl"]),
                                "plpc": float(v["unrealized_plpc"]) * 100,
                                "value": float(v.get("market_value") or 0),
                                "stop": _lvl(k, "stop"), "tp": _lvl(k, "tp")} for k, v in pos.items()]}
    if mode == "post":
        msgs += cancel_stale_entries(api, orders, tag)
    if mode != "intraday":
        return msgs, portfolio
    if day_pl <= -C.DAILY_LOSS_LIMIT:
        key = f"losslimit|{today}"
        if key not in state["sent"]:
            state["sent"][key] = today
            msgs.append(f"{tag} ⛔ Günlük kayıp {day_pl*100:.2f}% – limit aşıldı, bugün yeni alım yapılmayacak.")
            msgs += cancel_stale_entries(api, orders, tag)
        return msgs, portfolio

    return msgs + _manage_entries(api, summary, by, state, pos, orders, equity, bp, tag, today), portfolio


def _bot_entries(orders):
    return {o["symbol"]: o for o in orders
            if o["side"] == "buy" and str(o.get("client_order_id", "")).startswith("bt-")
            and o.get("status") in ("new", "accepted", "pending_new", "held", None)}


def cancel_stale_entries(api, orders, tag):
    """Kapanışta gerçekleşmemiş giriş emirlerini iptal et."""
    msgs = []
    for sym, o in _bot_entries(orders).items():
        try:
            api.cancel(o["id"])
            msgs.append(f"{tag} ⏹ {sym} gerçekleşmeyen limitli alış emri gün sonunda iptal edildi")
        except RuntimeError as e:
            print(e)
    return msgs


def _size(equity, bp, limit, stop):
    risk_ps = limit - stop
    if risk_ps <= 0:
        return 0, 0
    qty = math.floor(min(equity * C.RISK_PER_TRADE / risk_ps, equity * C.MAX_POSITION_PCT / limit, bp * 0.95 / limit))
    return max(qty, 0), risk_ps


def _manage_entries(api, summary, by, state, pos, orders, equity, bp, tag, today):
    msgs = []
    entries = _bot_entries(orders)
    picks = summary["picks"]
    # 1) Bekleyen emirleri gözden geçir: aday değilse / bölge altına düştüyse iptal, seviye değiştiyse güncelle
    for sym, o in list(entries.items()):
        s, pl = by.get(sym), (by.get(sym) or {}).get("plan")
        why = None
        if sym not in picks or not pl:
            why = "artık aday değil"
        elif s["price"] < pl["zoneLow"]:
            why = "fiyat alım bölgesinin altına indi"
        if why:
            try:
                api.cancel(o["id"])
                del entries[sym]
                msgs.append(f"{tag} ⏹ <b>{sym}</b> bekleyen alış emri iptal ({why})")
                _log({"sym": sym, "side": "cancel", "reason": why})
            except RuntimeError as e:
                print(e)
            continue
        old = float(o.get("limit_price") or 0)
        if old and abs(pl["zoneHigh"] - old) / old > 0.01:
            try:
                api.cancel(o["id"])
                del entries[sym]
                state["sent"].pop(f"entry|{today}|{sym}", None)   # aşağıda yeni seviyeyle yeniden konur
                _log({"sym": sym, "side": "reprice", "old": old, "new": pl["zoneHigh"]})
            except RuntimeError as e:
                print(e)

    # 2) Yeni limitli emirler
    reserved = sum(float(o.get("qty", 0)) * float(o.get("limit_price") or 0) for o in entries.values())
    bp -= reserved
    slots = C.MAX_POSITIONS - len(pos) - len(set(entries) - set(pos))
    for t in picks:
        if slots <= 0:
            break
        s = by[t]
        pl = s.get("plan")
        key = f"entry|{today}|{t}"
        if not pl or t in pos or t in entries or key in state["sent"]:
            continue
        price = s["price"]
        if price < pl["zoneLow"]:
            continue   # bölgenin altına düşmüş: düşen bıçağı tutma
        limit = min(price, pl["zoneHigh"])
        stop, tp = pl["stop"], pl[C.TAKE_PROFIT]
        qty, risk_ps = _size(equity, bp, limit, stop)
        if qty < 1 or tp <= limit:
            continue
        snap = decision_snapshot(s)
        try:
            api.bracket_buy(t, qty, limit, stop, tp, f"bt-{today}-{t}-{datetime.now().strftime('%H%M')}")
            state["sent"][key] = today
            slots -= 1
            bp -= qty * limit
            now = "fiyat bölgede, hemen gerçekleşebilir" if price <= pl["zoneHigh"] * 1.002 else f"şu an ${price:,.2f}, geri çekilme bekleniyor"
            msgs.append(f"{tag} 🟢 <b>{t}</b> LİMİTLİ ALIŞ emri · {qty} adet @ ${limit:,.2f} (≈${qty*limit:,.0f}) – {now}\n"
                        f"   Stop ${stop:,.2f} · risk ≈${qty*risk_ps:,.0f} = sermayenin %{qty*risk_ps/equity*100:.1f} · Kâr al ${tp:,.2f}\n"
                        f"   {_parts_text(snap)}\n"
                        + "\n".join(f"   • {w}" for w in snap["why"]))
            _log({"sym": t, "side": "buy_limit", "qty": qty, "limit": limit, "stop": stop, "tp": tp, "decision": snap})
        except RuntimeError as e:
            msgs.append(f"⚠️ {t} alış emri verilemedi: {e}")
    return msgs
