"""Gün aşırı strateji: '3 gün düşüş' + kapanışın %2 altından limitli alış (ana strateji).

Kural (tools/down3_analiz.py ile 2017-2026 testi):
  Sinyal : İslami filtreden geçen ilk 50 hissede, fiyat 200 günlük ortalamanın üstündeyken 3 gün üst üste
           düşük kapanış. Aynı gün birden çok sinyal varsa 3 günlük düşüşü en büyük olanlar önce.
  Alış   : sinyal günü kapanıştan sonra, kapanışın %2 altına limitli alış emri (GTC). Ertesi 2 seans içinde
           dolmazsa iptal.
  Satış  : emre bağlı stop (giriş -%2) ve kâr al (giriş +%2). Dolum fiyatı limitten farklıysa seviyeler
           gerçek alış fiyatına göre güncellenir. 10 seans içinde ikisi de olmazsa pozisyon kapatılır.
  Boyut  : sermaye / DIP_MAX_POSITIONS (kaldıraçsız, yalnız nakit). Emir kimliği 'dip-' ile başlar.
"""
import math
from datetime import datetime, timedelta

from . import config as C

TAG_PREFIX = "dip-"


def signal_view(df):
    """Günlük veri (add_indicators çıktısı) -> sinyal bilgisi."""
    c = df["Close"]
    if len(c) < 205:
        return {"signal": False}
    sma200 = float(c.tail(200).mean())
    last4 = c.tail(4).to_list()
    down3 = last4[3] < last4[2] < last4[1] < last4[0]
    drop3 = last4[3] / last4[0] - 1
    return {"signal": bool(down3 and last4[3] > sma200), "drop3": round(drop3 * 100, 2),
            "close": round(last4[3], 2), "sma200": round(sma200, 2),
            "limit": round(last4[3] * (1 - C.DIP_LIMIT_BELOW), 2)}


def dip_symbols(api):
    """Son 40 günde 'dip-' emriyle alınmış ve hâlâ elde olan semboller (bekleyen 'dip-' alışlar dahil)."""
    if not hasattr(api, "orders"):
        return set()
    after = (datetime.utcnow() - timedelta(days=40)).strftime("%Y-%m-%dT00:00:00Z")
    try:
        return {o["symbol"] for o in api.orders("all", after=after)
                if str(o.get("client_order_id", "")).startswith(TAG_PREFIX)
                and (float(o.get("filled_qty") or 0) > 0 or o.get("status") in ("new", "accepted", "held",
                                                                                 "partially_filled", "pending_new"))}
    except RuntimeError as e:
        print("dip emirleri okunamadı:", e)
        return set()


def _num(x):
    return float(x or 0)


def run(summary, by, mode, state, api, equity, cash, tag):
    """Mesaj listesi döner. post: yeni sinyal emirleri + süre sayaçları; intraday/post: seviye düzeltme."""
    ds = state.setdefault("dip", {"pending": {}, "held": {}})
    msgs = []
    day = summary["lastBar"]
    mine = dip_symbols(api)
    positions = {p["symbol"]: p for p in api.positions() if p.get("asset_class", "us_equity") != "crypto"}
    orders = api.open_orders()
    pend_orders = {o["symbol"]: o for o in orders if o["side"] == "buy"
                   and str(o.get("client_order_id", "")).startswith(TAG_PREFIX)}
    held = {s: p for s, p in positions.items() if s in mine}

    # 1) Yeni dolumlar ve kapanan pozisyonlar
    for s, p in held.items():
        if s not in ds["held"]:
            ds["held"][s] = {"since": day, "sessions": 0, "entry": _num(p["avg_entry_price"])}
            ds["pending"].pop(s, None)
            msgs.append(f"{tag} 🟢 <b>{s}</b> alış gerçekleşti · {p['qty']} adet @ ${_num(p['avg_entry_price']):,.2f} · "
                        f"stop ${_num(p['avg_entry_price'])*(1-C.DIP_STOP):,.2f} · kâr al ${_num(p['avg_entry_price'])*(1+C.DIP_TP):,.2f}")
    for s in list(ds["held"]):
        if s not in held:
            info = ds["held"].pop(s)
            msgs.append(f"{tag} ⚪ <b>{s}</b> pozisyon kapandı (stop, kâr al ya da süre) · giriş ${info.get('entry', 0):,.2f}")

    # 2) Stop / kâr al seviyelerini gerçek alış fiyatına göre düzelt
    for s, p in held.items():
        e = _num(p["avg_entry_price"])
        want = {"stop": round(e * (1 - C.DIP_STOP), 2), "limit": round(e * (1 + C.DIP_TP), 2)}
        for o in orders:
            if o["symbol"] != s or o["side"] != "sell":
                continue
            try:
                if o.get("type") == "stop" and abs(_num(o.get("stop_price")) - want["stop"]) > 0.011:
                    api.replace_stop(o["id"], want["stop"])
                elif o.get("type") == "limit" and abs(_num(o.get("limit_price")) - want["limit"]) > 0.011:
                    api.replace_limit(o["id"], want["limit"])
            except RuntimeError as ex:
                print(ex)
        if not any(o["symbol"] == s and o["side"] == "sell" and o.get("type") == "stop" for o in orders):
            try:   # koruma emri yoksa yeniden kur
                api.stop_sell(s, abs(int(float(p["qty"]))), want["stop"], f"dip-stop-{s}-{datetime.now():%m%d%H%M}")
                msgs.append(f"{tag} 🛡 <b>{s}</b> stop emri yoktu, yeniden kuruldu @ ${want['stop']:,.2f}")
            except RuntimeError as ex:
                msgs.append(f"⚠️ {s} stop kurulamadı: {ex}")

    if mode != "post":
        return msgs
    key = f"dipday|{day}"
    if key in state["sent"]:
        return msgs
    state["sent"][key] = day

    # 3) Süre sayaçları: dolmayan alışlar 2 seans sonra iptal, pozisyonlar 10 seans sonra kapatılır
    for s, o in pend_orders.items():
        pd_ = ds["pending"].setdefault(s, {"since": day, "sessions": 0})
        pd_["sessions"] += 1
        if pd_["sessions"] >= C.DIP_ENTRY_SESSIONS:
            try:
                api.cancel(o["id"])
                ds["pending"].pop(s, None)
                msgs.append(f"{tag} ⏹ <b>{s}</b> limitli alış {C.DIP_ENTRY_SESSIONS} seansta dolmadı, iptal edildi")
            except RuntimeError as ex:
                print(ex)
    for s in list(ds["pending"]):
        if s not in pend_orders and s not in held:
            ds["pending"].pop(s, None)
    for s, info in ds["held"].items():
        info["sessions"] += 1
        if info["sessions"] >= C.DIP_MAX_DAYS:
            for o in orders:
                if o["symbol"] == s and o["side"] == "sell":
                    try:
                        api.cancel(o["id"])
                    except RuntimeError as ex:
                        print(ex)
            try:
                api.close_position(s)
                msgs.append(f"{tag} 🔔 <b>{s}</b> {C.DIP_MAX_DAYS} seanstır stop/hedef olmadı, kapatılıyor (açılışta)")
            except RuntimeError as ex:
                msgs.append(f"⚠️ {s} kapatılamadı: {ex}")

    # 4) Yeni sinyaller -> limitli alış emirleri
    open_pend = {s for s in pend_orders if s in ds["pending"]}
    slots = C.DIP_MAX_POSITIONS - len(held) - len(open_pend)
    cands = sorted([s for s in summary["stocks"] if s.get("isTop") and (s.get("dip") or {}).get("signal")
                    and (s.get("sharia") or {}).get("ok")], key=lambda s: s["dip"]["drop3"])
    state.setdefault("dipSignals", {})[day] = [s["t"] for s in cands]
    room = max(cash, 0.0) * 0.98
    for s in cands:
        t = s["t"]
        if slots <= 0:
            msgs.append(f"{tag} ℹ️ <b>{t}</b> sinyal verdi ama {C.DIP_MAX_POSITIONS} pozisyon dolu")
            continue
        if t in positions or t in pend_orders:
            continue
        lim = s["dip"]["limit"]
        qty = math.floor(min(equity / C.DIP_MAX_POSITIONS, room) / lim)
        if qty < 1:
            msgs.append(f"⚠️ {t} sinyal verdi ama yeterli nakit yok")
            continue
        stop, tp = round(lim * (1 - C.DIP_STOP), 2), round(lim * (1 + C.DIP_TP), 2)
        try:
            api.bracket_buy(t, qty, lim, stop, tp, f"{TAG_PREFIX}{day}-{t}")
            ds["pending"][t] = {"since": day, "sessions": 0, "limit": lim}
            slots -= 1
            room -= qty * lim
            msgs.append(f"{tag} 📉 <b>{t}</b> 3 gün üst üste düştü ({s['dip']['drop3']:+.1f}%), 200 gün ort. üstünde\n"
                        f"   Limitli alış {qty} adet @ ${lim:,.2f} (kapanış ${s['dip']['close']:,.2f} − %{C.DIP_LIMIT_BELOW*100:g}) "
                        f"≈${qty*lim:,.0f} · stop ${stop:,.2f} · kâr al ${tp:,.2f} · {C.DIP_ENTRY_SESSIONS} seans geçerli")
        except RuntimeError as ex:
            msgs.append(f"⚠️ {t} alış emri verilemedi: {ex}")
    return msgs
