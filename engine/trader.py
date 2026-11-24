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
import re
import math
import os
from datetime import datetime

import requests
from pathlib import Path
from zoneinfo import ZoneInfo

from . import config as C
from . import data as D
from . import dip
from . import ortavade
from . import kayit

STATUS = {}

PAPER_URL = "https://paper-api.alpaca.markets"
LIVE_URL = "https://api.alpaca.markets"
DATA_URL = "https://data.alpaca.markets"


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

    def clock(self):
        return self._r("GET", "/v2/clock")

    def calendar(self, start, end):
        return self._r("GET", "/v2/calendar", params={"start": start, "end": end})

    def orders(self, status="all", after=None, limit=500):
        p = {"status": status, "nested": "false", "limit": limit, "direction": "desc"}
        if after:
            p["after"] = after
        return self._r("GET", "/v2/orders", params=p)

    def configure(self, **cfg):
        """Hesap ayarı (ör. no_shorting=True, max_margin_multiplier="1")."""
        return self._r("PATCH", "/v2/account/configurations", json=cfg)

    def bars(self, symbols, start, end=None, timeframe="5Min", feed=None, adjustment="raw"):
        """Alpaca piyasa verisi: {sembol: [{t,o,h,l,c,v,vw}, ...]} (sayfalama dahil)."""
        feed = feed or os.getenv("ALPACA_DATA_FEED", "iex")
        out, token = {}, None
        while True:
            p = {"symbols": ",".join(symbols), "timeframe": timeframe, "start": start, "limit": 10000,
                 "feed": feed, "adjustment": adjustment}
            if end:
                p["end"] = end
            if token:
                p["page_token"] = token
            for wait in (0, 2, 5, 15, 30, 60):      # 429 (istek sınırı) gelirse bekleyip yeniden dene
                if wait:
                    import time
                    time.sleep(wait)
                r = requests.get(DATA_URL + "/v2/stocks/bars", headers=self.h, params=p, timeout=60)
                if r.status_code != 429:
                    break
            if not r.ok:
                raise RuntimeError(f"Alpaca veri: {r.status_code} {r.text[:300]}")
            j = r.json()
            for sym, rows in (j.get("bars") or {}).items():
                out.setdefault(sym, []).extend(rows)
            token = j.get("next_page_token")
            if not token:
                return out

    def day_buy(self, sym, qty, limit, stop, tp, client_id):
        """Günlük işlem: limitli alış + bağlı stop ve kâr al (bracket), yalnız bugün geçerli (day)."""
        return self._r("POST", "/v2/orders", json={
            "symbol": sym, "qty": str(qty), "side": "buy", "type": "limit", "limit_price": f"{limit:.2f}",
            "time_in_force": "day", "order_class": "bracket", "client_order_id": client_id,
            "take_profit": {"limit_price": f"{tp:.2f}"}, "stop_loss": {"stop_price": f"{stop:.2f}"}})

    def positions(self):
        return self._r("GET", "/v2/positions")

    def open_orders(self):
        """Açık emirler, bracket/OCO bacakları (stop, kâr al) dahil, düz liste.
        nested=false ile Alpaca 'held' durumdaki stop bacağını döndürmüyor; bot stop yok sanıp yeni koruma
        emri kurmaya çalışıyor ve hisse kâr al emrinde tutulduğu için 403 'insufficient qty' alıyordu.
        Bu yüzden iki liste (nested=false ve nested=true) birleştirilir, bacaklar açılır, tekrarlar atılır."""
        flat = self._r("GET", "/v2/orders", params={"status": "open", "nested": "false", "limit": 500}) or []
        nest = self._r("GET", "/v2/orders", params={"status": "open", "nested": "true", "limit": 500}) or []
        raw = list(flat) + list(nest)
        out, seen = [], set()
        for o in raw:
            for x in [o] + list(o.get("legs") or []):
                if x.get("id") in seen or x.get("status") in ("filled", "canceled", "expired", "rejected", "replaced"):
                    continue
                seen.add(x.get("id"))
                out.append(x)
        return out

    def bracket_buy(self, sym, qty, limit, stop, tp, client_id):
        """Limitli alış + bağlı stop (tp verilirse kâr al da). GTC: stop gece de korur; gerçekleşmeyen
        giriş emirleri kapanışta bot tarafından iptal edilir. tp yoksa 'oto' (yalnız stop) emri."""
        body = {"symbol": sym, "qty": str(qty), "side": "buy", "type": "limit", "limit_price": f"{limit:.2f}",
                "time_in_force": "gtc", "client_order_id": client_id, "stop_loss": {"stop_price": f"{stop:.2f}"}}
        if tp:
            body.update({"order_class": "bracket", "take_profit": {"limit_price": f"{tp:.2f}"}})
        else:
            body["order_class"] = "oto"
        return self._r("POST", "/v2/orders", json=body)

    def oco_sell(self, sym, qty, tp, stop, client_id):
        """Eldeki adet için kâr al + stop (biri olunca diğeri iptal), GTC."""
        return self._r("POST", "/v2/orders", json={
            "symbol": sym, "qty": str(qty), "side": "sell", "type": "limit", "time_in_force": "gtc",
            "order_class": "oco", "client_order_id": client_id, "take_profit": {"limit_price": f"{tp:.2f}"},
            "stop_loss": {"stop_price": f"{stop:.2f}"}})

    def stop_sell(self, sym, qty, stop, client_id):
        """Bağımsız koruyucu stop satış emri (GTC)."""
        return self._r("POST", "/v2/orders", json={
            "symbol": sym, "qty": str(qty), "side": "sell", "type": "stop", "stop_price": f"{stop:.2f}",
            "time_in_force": "gtc", "client_order_id": client_id})

    # ---- kripto ----
    def asset(self, sym):
        return self._r("GET", f"/v2/assets/{sym}")

    def crypto_buy(self, sym, qty, limit, client_id):
        return self._r("POST", "/v2/orders", json={
            "symbol": sym, "qty": str(qty), "side": "buy", "type": "limit", "limit_price": str(limit),
            "time_in_force": "gtc", "client_order_id": client_id})

    def crypto_stop(self, sym, qty, stop, limit, client_id):
        return self._r("POST", "/v2/orders", json={
            "symbol": sym, "qty": str(qty), "side": "sell", "type": "stop_limit", "stop_price": str(stop),
            "limit_price": str(limit), "time_in_force": "gtc", "client_order_id": client_id})

    def replace_stop_limit(self, order_id, stop, limit):
        return self._r("PATCH", f"/v2/orders/{order_id}", json={"stop_price": str(stop), "limit_price": str(limit)})

    def replace_limit(self, order_id, limit):
        return self._r("PATCH", f"/v2/orders/{order_id}", json={"limit_price": f"{limit:.2f}"})

    def replace_stop(self, order_id, stop):
        return self._r("PATCH", f"/v2/orders/{order_id}", json={"stop_price": f"{stop:.2f}"})

    def cancel(self, order_id):
        return self._r("DELETE", f"/v2/orders/{order_id}")

    def close_all(self):
        """Tüm emirleri iptal et ve tüm pozisyonları kapat (piyasa kapalıysa açılışta gerçekleşir)."""
        # cancel_orders=true önce açık emirleri iptal eder, sonra satış emirlerini verir.
        # (Ayrıca DELETE /v2/orders çağrılmaz: o, yeni verilen satış emirlerini de iptal ediyordu.)
        return self._r("DELETE", "/v2/positions", params={"cancel_orders": "true"})

    def close_position(self, sym):
        return self._r("DELETE", f"/v2/positions/{sym}")


def close_legacy(api, pos, orders, tag, wait=1.5, tries=8):
    """Eski stratejiden kalan pozisyonları piyasa emriyle kapat (piyasa kapalıysa açılışta). Zaten kapatma
    emri bekleyenlere tekrar emir verilmez.

    Not: Alpaca'da emir iptali eşzamansızdır. Stop/kâr al emri iptal edilir edilmez pozisyon kapatılmaya
    çalışılırsa hisseler hâlâ o emirlere bağlı göründüğü için 403 "insufficient qty available" hatası
    döner. Bu yüzden önce ilgili tüm emirler iptal edilir, iptal tamamlanana kadar beklenir, kapatma
    403/422 hatasında birkaç kez yeniden denenir."""
    import time
    todo = {s: p for s, p in pos.items()
            if not any(o["symbol"] == s and o["side"] == "sell" and o.get("type") == "market" for o in orders)}
    if not todo:
        return None
    # 1) Bu sembollerin bütün açık emirlerini iptal et
    for o in orders:
        if o["symbol"] in todo:
            try:
                api.cancel(o["id"])
            except RuntimeError as e:
                print(e)
    # 2) İptaller tamamlanana kadar bekle (en fazla tries x wait sn)
    for _ in range(tries):
        try:
            left = [o for o in api.open_orders() if o["symbol"] in todo]
        except RuntimeError as e:
            print(e)
            left = []
        if not left:
            break
        time.sleep(wait)
    # 3) Pozisyonları kapat; hisse hâlâ emre bağlıysa (403/422) bekleyip yeniden dene
    done, failed = [], []
    for s, p in todo.items():
        err = None
        for i in range(tries):
            try:
                api.close_position(s)
                err = None
                break
            except RuntimeError as e:
                err = e
                if " 403 " not in str(e) and " 422 " not in str(e):
                    break
                time.sleep(wait)
        if err:
            print("Eski pozisyon kapatılamadı:", s, err)
            failed.append(f"⚠️ {s} kapatılamadı: {err}")
            continue
        today = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
        kayit.add_event(sembol=s, islem="SATIŞ", adet=p["qty"], fiyat=round(float(p["current_price"]), 2),
                        neden="eski strateji pozisyonu kapatıldı (piyasa emri)", strateji="eski",
                        anahtar=f"legacy-{today}-{s}")
        print("Eski pozisyon kapatma emri verildi:", s, p["qty"])
        done.append(f"• <b>{s}</b> {p['qty']} adet (≈${float(p.get('market_value') or 0):,.0f}, "
                    f"K/Z {float(p.get('unrealized_pl') or 0):+,.2f}$)")
    if not done and not failed:
        return None
    return (f"🧹 <b>Eski strateji pozisyonlarını kapatıyorum</b> ({tag}) – piyasa emri"
            " (piyasa kapalıysa açılışta gerçekleşir):\n" + "\n".join(done + failed))


def reset_command(api, state, equity):
    """komutlar/sifirla.json: [{"tarih": "YYYY-MM-DD"}] – o gün (New York) tüm pozisyonları kapat, emirleri
    iptal et ve işlem günlüğünü yeniden başlat. Bir kez uygulanır."""
    p = Path("komutlar/sifirla.json")
    if not p.exists() or not hasattr(api, "close_all"):
        return None
    try:
        cmds = json.loads(p.read_text())
    except ValueError:
        return None
    today = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
    key = f"reset|{today}"
    if today not in {str(c.get("tarih")) for c in cmds} or key in state.get("done", {}):
        return None
    pos = [x for x in api.positions() if x.get("asset_class", "us_equity") != "crypto"]
    n_orders = len(api.open_orders())
    try:
        api.close_all()
    except RuntimeError as e:
        return f"⚠️ Sıfırlama yapılamadı: {e}"
    state.setdefault("done", {})[key] = today          # 10 günlük temizliğe takılmasın diye ayrı yerde
    kayit.reset(equity)
    for x in pos:
        kayit.add_event(sembol=x["symbol"], islem="SIFIRLAMA", adet=x["qty"], fiyat=round(float(x["current_price"]), 2),
                        neden="eski strateji pozisyonu, sıfırlama ile piyasa emriyle kapatıldı",
                        strateji="eski", anahtar=f"reset-{today}-{x['symbol']}")
    for k in ("dip", "trail", "held"):
        state.pop(k, None)
    state["startEquity"] = equity
    tag = "🧪 SANAL" if getattr(api, "paper", True) else "💵 GERÇEK"
    return (f"🔄 <b>Sıfırlama</b> ({tag}) – {len(pos)} eski pozisyon kapatılıyor"
            + (f" ({', '.join(x['symbol'] for x in pos)})" if pos else "")
            + f", {n_orders} açık emir iptal edildi. İşlem günlüğü yeniden başladı · başlangıç sermayesi ${equity:,.2f}")


def daytrade_enabled():
    return os.getenv("DAYTRADE_ENABLED", "").lower() == "true"


def ny_midnight_iso():
    n = datetime.now(ZoneInfo("America/New_York"))
    return n.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()


def day_symbols(api):
    """Bugün günlük işlem modülünün (client_order_id 'dt-') işlem yaptığı semboller."""
    if not hasattr(api, "orders"):
        return set()
    try:
        return {o["symbol"] for o in api.orders("all", after=ny_midnight_iso())
                if str(o.get("client_order_id", "")).startswith("dt-")}
    except RuntimeError as e:
        print("Günlük işlem emirleri okunamadı:", e)
        return set()


def ensure_no_margin(api, state):
    """Hesabı açığa satışsız ve kaldıraçsız ayarla (günde bir deneme; hata işlemi durdurmaz)."""
    key = f"nomargin|{datetime.utcnow():%Y-%m-%d}"
    if key in state.get("sent", {}) or not hasattr(api, "configure"):
        return
    state.setdefault("sent", {})[key] = f"{datetime.utcnow():%Y-%m-%d}"
    try:
        cfg = api.configure(no_shorting=True, max_margin_multiplier="1")
        STATUS["accountConfig"] = {k: cfg.get(k) for k in ("no_shorting", "max_margin_multiplier")}
    except RuntimeError as e:
        STATUS["accountConfig"] = f"ayarlanamadı: {str(e)[:150]}"


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
    return {"score": s.get("score"), "base": s.get("base"), "parts": s.get("parts"), "trend": s.get("trend"), "rsi": s.get("rsi"),
            "price": s.get("price"), "ema20": s.get("ema20"), "ema50": s.get("ema50"), "atr": s.get("atr"),
            "signals": recent_buy, "entryType": kind, "daysToEarnings": fv.get("daysToEarnings"), "why": why}


def _parts_text(snap):
    p = snap.get("parts") or {}
    base = snap.get("base", snap.get("score", 0) - (p.get("mevsim") or 0))
    return (f"Puan <b>{snap.get('score', 0):.0f}</b> = ana {base:.0f}/100 + mevsim bonusu {p.get('mevsim', 0):.0f}/15\n"
            f"   (teknik {p.get('teknik', 0)}/40 · göreceli {p.get('goreceli', 0):.0f}/20 · temel {p.get('temel', 0)}/25 · "
            f"revizyon {p.get('revizyon', 0):.0f}/10 · hacim {p.get('hacim', 0):.0f}/5)")


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
    if C.NO_MARGIN:   # faizli kredi (margin) kullanılmaz: alım gücü en fazla nakit kadar
        bp = min(bp, max(float(acct.get("cash", bp) or 0), 0.0))
        ensure_no_margin(api, state)
    day_pl = (equity - last_eq) / last_eq if last_eq else 0.0
    reset_msg = reset_command(api, state, equity)
    # Kripto pozisyon/emirlerini kripto modülü, günlük işlem (dt-) pozisyonlarını daytrade modülü yönetir
    dt_syms = day_symbols(api)
    # Yeni ana strateji (dip.py) kendi 'dip-' pozisyonlarını yönetir; buradaki eski kurallar onlara dokunmaz
    dip_syms = dip.dip_symbols(api) if C.STRATEGY == "dip" else set()
    # Orta vade (ov-) pozisyonlarını ortavade.py yönetir
    ov_syms = ortavade.ov_symbols(api) if C.OV_ENABLED else set()
    dip_syms -= ov_syms
    skip = dt_syms | dip_syms | ov_syms
    all_pos = {p["symbol"]: p for p in api.positions()
               if p.get("asset_class", "us_equity") != "crypto" and p["symbol"] not in dt_syms}
    all_orders = [o for o in api.open_orders()
                  if o.get("asset_class", "us_equity") != "crypto" and o["symbol"] not in dt_syms]
    pos = {k: v for k, v in all_pos.items() if k not in skip}
    print("Pozisyonlar:", ", ".join(f"{k} {v['qty']}{' (dip)' if k in dip_syms else ''}" for k, v in all_pos.items()) or "yok")
    print("Açık emirler:", ", ".join(f"{o['symbol']} {o['side']} {o.get('type')} {o.get('status')}"
                                     for o in all_orders) or "yok")
    orders = [o for o in all_orders if o["symbol"] not in skip]
    flat = _legs(orders)
    tag = "🧪 SANAL" if getattr(api, "paper", True) else "💵 GERÇEK"
    today = summary["lastBar"]
    if getattr(C, "CRYPTO_ENABLED", False):
        equity *= (1 - C.CRYPTO_ALLOC)        # hisse bölümü sermayenin %90'ı
    if daytrade_enabled():
        equity *= (1 - C.DAYTRADE_ALLOC)      # günlük işleme ayrılan pay swing bota dahil edilmez

    # Ana strateji 'dip' iken eski stratejiden kalan pozisyonlar yönetilmez, kapatılır
    legacy_on = C.STRATEGY != "dip"
    legacy_alert = None
    if not legacy_on and C.DIP_CLOSE_LEGACY and mode in ("intraday", "post") and pos:
        legacy_alert = close_legacy(api, pos, orders, tag)
        # Yalnız hata içeren uyarı günde bir kez gönderilir (her 15 dk'da tekrar etmesin)
        if legacy_alert and "• <b>" not in legacy_alert:
            day = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
            if state.get("legacy_warned") == day:
                legacy_alert = None
            else:
                state["legacy_warned"] = day

    # 1) Çıkışlar: kapanışta teyitli SAT sinyali veya bilanço öncesi (post modunda; emir ertesi açılışta gerçekleşir)
    m_msgs, closed = manual_sells(api, pos, flat, by, state, tag)
    msgs += m_msgs
    if legacy_on and mode == "post":
        for sym, p in pos.items():
            s = by.get(sym)
            if not s or sym in closed:
                continue
            reason = next((g["name"] for g in s["signals"] if g["code"] in C.EXIT_SIGNALS), None)
            d2e = s["fund"]["daysToEarnings"]
            shr = s.get("sharia") or {}
            if not reason and C.SHARIA_FILTER and C.SHARIA_SELL_IF_FAILS and not shr.get("ok", True) and shr.get("hard"):
                reason = f"İslami filtreden çıktı ({shr.get('reason')})"
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
                closed.add(sym)
                pl = float(p.get("unrealized_pl", 0))
                msgs.append(f"{tag} 🔴 <b>{sym}</b> SATIŞ emri ({reason}) · {p['qty']} adet · Gerçekleşmemiş K/Z ${pl:,.2f}")
                _log({"sym": sym, "side": "sell", "qty": p["qty"], "reason": reason,
                      "pl": float(p.get("unrealized_pl", 0)), "decision": decision_snapshot(s)})
            except RuntimeError as e:
                msgs.append(f"⚠️ {sym} kapatılamadı: {e}")

    # 2) İz süren stop: girişten beri en yüksek fiyat - TRAIL_ATR x ATR; stop yalnız yukarı taşınır
    if legacy_on and C.TRAIL_ATR and mode in ("intraday", "post"):
        msgs += _trail_stops(api, {k: v for k, v in pos.items() if k not in closed}, flat, by, state, tag)
    state["held"] = sorted(pos)
    state["trail"] = {k: v for k, v in state.get("trail", {}).items() if k in pos}

    # 3) Girişler (yalnız seans içinde)
    def _lvl(sym, kind):
        for o in _legs(all_orders):
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
                                 for o in all_orders if o["side"] == "buy"],
                 "positions": [{"sym": k, "qty": v["qty"], "entry": float(v["avg_entry_price"]),
                                "price": float(v["current_price"]), "pl": float(v["unrealized_pl"]),
                                "plpc": float(v["unrealized_plpc"]) * 100,
                                "value": float(v.get("market_value") or 0),
                                "stop": _lvl(k, "stop"), "tp": _lvl(k, "tp"),
                                "strategy": "orta vade" if k in ov_syms else "3 gün düşüş" if k in dip_syms else "eski"}
                               for k, v in all_pos.items()]}
    if C.STRATEGY == "dip":
        # Eski giriş kuralı kapalı: eldeki eski pozisyonlar kendi stoplarıyla kapanana kadar yönetilir.
        # Anlık bildirim yalnız emir girerken (ve sıfırlamada); diğer her şey gün sonu raporuna not olarak gider.
        if mode == "post":
            msgs += cancel_stale_entries(api, orders, tag)
        alerts = [m for m in (reset_msg, legacy_alert) if m]
        if mode in ("intraday", "post") and not reset_msg:
            cash = min(float(acct.get("cash", 0) or 0), bp) if C.NO_MARGIN else bp
            ov_pocket = equity * C.OV_ALLOC if C.OV_ENABLED else 0.0
            a, _ = dip.run(summary, by, mode, state, api, equity - ov_pocket, cash, tag)
            alerts += a
            if C.OV_ENABLED:
                try:
                    acct2 = api.account()       # kısa vade emirlerinden sonra kalan nakit
                    cash2 = min(float(acct2.get("cash", 0) or 0), float(acct2.get("buying_power", 0) or 0))
                except (RuntimeError, KeyError, TypeError, ValueError):
                    cash2 = cash
                a, _ = ortavade.run(summary, by, mode, state, api, ov_pocket, cash2, tag)
                alerts += a
        if msgs:
            state.setdefault("dip", {}).setdefault("notes", {}).setdefault(today, []).extend(
                re.sub("<[^>]+>", "", m) for m in msgs)
        return alerts, portfolio
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

    return msgs + _manage_entries(api, summary, by, state, pos, orders, equity, bp, tag, today, dt_syms), portfolio


def _num(q):
    q = abs(float(q))
    return int(q) if q.is_integer() else q


def _protect(api, sym, p, s, sells, stops, t, tag):
    """Stop emri yoksa ya da adedi pozisyonla uyuşmuyorsa koruyucu stopu yeniden kur.
    Fiyat zaten stop seviyesinin altındaysa pozisyonu kapat. Mesaj (ya da None) döner."""
    entry, price, qty = float(p["avg_entry_price"]), float(p["current_price"]), _num(p["qty"])
    if any(o.get("type") == "market" for o in sells):
        return None                                   # kapanış emri zaten yolda; çift satış yapma
    atr = (s or {}).get("atr")
    if atr:
        level = max(entry - 2 * atr, t["hi"] - C.TRAIL_ATR * atr)
    else:
        level = entry * 0.92                          # veri yoksa: girişin %8 altı
    old_stop = max([float(o["stop_price"]) for o in stops if o.get("stop_price")] or [0])
    level = round(max(level, old_stop), 2)            # mevcut stop daha yüksekse onu koru (asla aşağı çekme)
    for o in sells:                                   # Alpaca adedi bağlı tutmasın: eski satış emirlerini kaldır
        try:
            api.cancel(o["id"])
        except RuntimeError as e:
            print(e)
    try:
        if price <= level:
            api.close_position(sym)
            _log({"sym": sym, "side": "sell", "qty": qty, "reason": "stop emri yoktu, fiyat stop seviyesinin altında"})
            return (f"{tag} 🛡 <b>{sym}</b> stop emri bulunamadı ve fiyat (${price:,.2f}) stop seviyesinin "
                    f"(${level:,.2f}) altında – pozisyon kapatıldı")
        api.stop_sell(sym, qty, level, f"bt-stop-{sym}-{datetime.now().strftime('%m%d%H%M')}")
        _log({"sym": sym, "side": "stop_restore", "qty": qty, "stop": level})
        return f"{tag} 🛡 <b>{sym}</b> koruyucu stop yeniden kuruldu: {qty} adet @ ${level:,.2f}"
    except RuntimeError as e:
        return f"⚠️ <b>{sym}</b> koruyucu stop kurulamadı: {e} – Alpaca'dan kontrol et"


def _trail_stops(api, pos, flat, by, state, tag):
    msgs = []
    trail = state.setdefault("trail", {})
    for sym, p in pos.items():
        s = by.get(sym)
        entry, price = float(p["avg_entry_price"]), float(p["current_price"])
        sells = [o for o in flat if o["symbol"] == sym and o["side"] == "sell"]
        stops = [o for o in sells if o.get("type") in ("stop", "stop_limit") and o.get("stop_price")]
        t = trail.setdefault(sym, {"hi": max(entry, price),
                                   "msg": float(stops[0]["stop_price"]) if stops else 0.0})
        t["hi"] = max(t["hi"], price, (s or {}).get("high") or price)
        qty = _num(p["qty"])
        qty_ok = all(o.get("qty") is None or _num(o["qty"]) == qty for o in stops)
        if not stops or not qty_ok:
            if any(o["symbol"] == sym and o["side"] == "buy" for o in flat):
                continue          # giriş emri hâlâ kısmen açık: bacaklar bekleme durumunda olabilir, bu tur dokunma
            m = _protect(api, sym, p, s, sells, stops, t, tag)
            if m:
                msgs.append(m)
            continue
        leg = stops[0]
        if not s or not s.get("atr"):
            continue
        cur = float(leg["stop_price"])
        new = round(t["hi"] - C.TRAIL_ATR * s["atr"], 2)
        if new >= price and new > cur:
            # Taramalar arasında fiyat tepeden 3 ATR'den fazla düştü: stop zaten tetiklenmiş olmalıydı -> kapat
            for o in sells:
                try:
                    api.cancel(o["id"])
                except RuntimeError as e:
                    print(e)
            try:
                api.close_position(sym)
                _log({"sym": sym, "side": "sell", "qty": p["qty"], "reason": "iz süren stop seviyesinin altında"})
                msgs.append(f"{tag} 🔴 <b>{sym}</b> SATIŞ: fiyat (${price:,.2f}) iz süren stop seviyesinin (${new:,.2f}) altında")
            except RuntimeError as e:
                msgs.append(f"⚠️ {sym} kapatılamadı: {e}")
            continue
        if new > cur * 1.002 and new < price:
            try:
                api.replace_stop(leg["id"], new)
                _log({"sym": sym, "side": "trail", "stop": new, "old": cur, "hi": t["hi"]})
                # Bildirim: stop girişin üstüne ilk çıktığında ya da son bildirimden 0.5 ATR yükseldiğinde
                crossed = cur < entry <= new
                if crossed or new - t.get("msg", cur) >= 0.5 * s["atr"]:
                    t["msg"] = new
                    lock = (new / entry - 1) * 100
                    msgs.append(f"{tag} 🔒 <b>{sym}</b> iz süren stop yükseltildi: ${cur:,.2f} → ${new:,.2f} "
                                f"(en yüksek ${t['hi']:,.2f}; stop girişe göre {lock:+.1f}%)")
            except RuntimeError as e:
                print(e)
    return msgs


def manual_sells(api, pos, flat, by, state, tag):
    """komutlar/sat.json: [{"sembol": "MU", "tarih": "YYYY-MM-DD"}] – o gün (New York tarihi) pozisyonu kapat.
    Piyasa kapalıyken verilen kapanış emri Alpaca'da sıraya girer ve açılışta gerçekleşir.
    Aynı gün o hisseye yeniden alım yapılmaz."""
    p = Path("komutlar/sat.json")
    if not p.exists():
        return [], set()
    try:
        cmds = json.loads(p.read_text())
    except ValueError as e:
        return [f"⚠️ komutlar/sat.json okunamadı: {e}"], set()
    today = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
    msgs, closed = [], set()
    block = state.setdefault("blocked", {})
    for c in cmds:
        sym, day = str(c.get("sembol", "")).upper(), str(c.get("tarih", ""))
        if day != today or not sym:
            continue
        block[sym] = today
        key = f"manual|{today}|{sym}"
        if key in state["sent"]:
            continue
        for o in flat:                      # bekleyen alışları ve bağlı stop/kâr al emirlerini kaldır
            if o["symbol"] == sym and o.get("type") != "market":
                try:
                    api.cancel(o["id"])
                except RuntimeError as e:
                    print(e)
        if sym not in pos:
            state["sent"][key] = today
            msgs.append(f"{tag} ℹ️ <b>{sym}</b> satış komutu: açık pozisyon yok, bekleyen emirler iptal edildi")
            continue
        p0 = pos[sym]
        try:
            api.close_position(sym)
            state["sent"][key] = today
            closed.add(sym)
            pl = float(p0.get("unrealized_pl", 0))
            msgs.append(f"{tag} 🔴 <b>{sym}</b> SATIŞ emri (senin komutun) · {p0['qty']} adet · "
                        f"son K/Z ${pl:,.2f} – piyasa kapalıysa açılışta gerçekleşir")
            _log({"sym": sym, "side": "sell", "qty": p0["qty"], "reason": "kullanıcı komutu", "pl": pl,
                  "decision": decision_snapshot(by.get(sym))})
        except RuntimeError as e:
            msgs.append(f"⚠️ <b>{sym}</b> satış komutu uygulanamadı: {e}")
    if msgs:
        STATUS["manuel"] = [re.sub("<[^>]+>", "", m) for m in msgs]
    return msgs, closed


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


def _manage_entries(api, summary, by, state, pos, orders, equity, bp, tag, today, dt_syms=()):
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
        if state.get("blocked", {}).get(t) == datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d"):
            continue
        if not pl or t in pos or t in entries or key in state["sent"] or t in dt_syms:
            continue
        price = s["price"]
        if price < pl["zoneLow"]:
            continue   # bölgenin altına düşmüş: düşen bıçağı tutma
        limit = min(price, pl["zoneHigh"])
        stop, tp = pl["stop"], (pl[C.TAKE_PROFIT] if C.TAKE_PROFIT else None)
        qty, risk_ps = _size(equity, bp, limit, stop)
        if qty < 1 or (tp is not None and tp <= limit):
            continue
        snap = decision_snapshot(s)
        try:
            api.bracket_buy(t, qty, limit, stop, tp, f"bt-{today}-{t}-{datetime.now().strftime('%H%M')}")
            state["sent"][key] = today
            slots -= 1
            bp -= qty * limit
            now = "fiyat bölgede, hemen gerçekleşebilir" if price <= pl["zoneHigh"] * 1.002 else f"şu an ${price:,.2f}, geri çekilme bekleniyor"
            exit_txt = f"Kâr al ${tp:,.2f}" if tp else f"Kâr al yok, iz süren stop {C.TRAIL_ATR} ATR"
            msgs.append(f"{tag} 🟢 <b>{t}</b> LİMİTLİ ALIŞ emri · {qty} adet @ ${limit:,.2f} (≈${qty*limit:,.0f}) – {now}\n"
                        f"   Stop ${stop:,.2f} · risk ≈${qty*risk_ps:,.0f} = sermayenin %{qty*risk_ps/equity*100:.1f} · {exit_txt}\n"
                        f"   {_parts_text(snap)}\n"
                        + "\n".join(f"   • {w}" for w in snap["why"]))
            _log({"sym": t, "side": "buy_limit", "qty": qty, "limit": limit, "stop": stop, "tp": tp, "decision": snap})
        except RuntimeError as e:
            msgs.append(f"⚠️ {t} alış emri verilemedi: {e}")
    return msgs
