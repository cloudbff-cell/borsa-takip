"""Gün aşırı strateji: '3 gün düşüş' + kapanışın %2 altından limitli alış (ana strateji).

Kural (tools/down3_analiz.py ile 2017-2026 testi):
  Sinyal : İslami filtreden geçen ilk 50 hissede, fiyat 200 günlük ortalamanın üstündeyken 3 gün üst üste
           düşük kapanış. Aynı gün birden çok sinyal varsa 3 günlük düşüşü en büyük olanlar önce.
  Alış   : sinyal günü kapanıştan sonra, kapanışın %2 altına limitli alış emri (GTC). Ertesi 2 seans içinde
           dolmazsa iptal.
  Satış  : emre bağlı stop (giriş -%2) ve kâr al (giriş +%2). Dolum fiyatı limitten farklıysa seviyeler
           gerçek alış fiyatına göre güncellenir. 10 seans içinde ikisi de olmazsa pozisyon kapatılır.
  Boyut  : sermaye / DIP_MAX_POSITIONS (kaldıraçsız, yalnız nakit). Emir kimliği 'dip-' ile başlar.
Her adım kayit.py ile kayitlar/ klasöründeki günlüğe yazılır.
"""
import math
from datetime import datetime, timedelta

from . import config as C
from . import kayit

TAG_PREFIX = "dip-"
STRAT = "3 gün düşüş"


def signal_view(df):
    """Günlük veri -> sinyal bilgisi."""
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


def _orders(api, days=40):
    if not hasattr(api, "orders"):
        return []
    after = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%dT00:00:00Z")
    try:
        return api.orders("all", after=after)
    except RuntimeError as e:
        print("emir geçmişi okunamadı:", e)
        return []


def dip_symbols(api, hist=None):
    """'dip-' emriyle alınmış ya da bekleyen 'dip-' alışı olan semboller."""
    hist = _orders(api) if hist is None else hist
    return {o["symbol"] for o in hist if str(o.get("client_order_id", "")).startswith(TAG_PREFIX)
            and (float(o.get("filled_qty") or 0) > 0
                 or o.get("status") in ("new", "accepted", "held", "partially_filled", "pending_new"))}


def _num(x):
    return float(x or 0)


def _ts(o, k="filled_at"):
    """Emir zamanı -> (NY tarihi, saati)."""
    v = (o or {}).get(k) or (o or {}).get("updated_at") or (o or {}).get("submitted_at")
    if not v:
        n = kayit.now_ny()
        return n.strftime("%Y-%m-%d"), n.strftime("%H:%M")
    t = datetime.fromisoformat(str(v).replace("Z", "+00:00")).astimezone(kayit.NY)
    return t.strftime("%Y-%m-%d"), t.strftime("%H:%M")


def _context(s, rank, n_same, summary):
    """Sinyal anındaki koşullar (analiz için)."""
    sh = s.get("sharia") or {}
    b = summary.get("benchmark") or {}
    d = s["dip"]
    return {"sinyal_tarihi": summary["lastBar"], "sembol": s["t"], "sirket": s.get("name"), "sektor": s.get("sector"),
            "strateji": STRAT, "kapanis": d["close"], "limit": d["limit"], "dusus_3g_yuzde": d["drop3"],
            "sma200_uzaklik_yuzde": round((d["close"] / d["sma200"] - 1) * 100, 2) if d.get("sma200") else None,
            "rsi14": s.get("rsi"),
            "atr_yuzde": round(s["atr"] / s["price"] * 100, 2) if s.get("atr") and s.get("price") else None,
            "hacim_orani": s.get("volr"), "puan": s.get("score"),
            "piyasa_degeri_mr": round(s["mcap"] / 1e9, 1) if s.get("mcap") else None,
            "borc_orani": sh.get("debt"), "nakit_orani": sh.get("cash"),
            "spy_trend": b.get("trend"), "spy_gun_yuzde": b.get("chg"), "sira": rank, "ayni_gun_sinyal": n_same}


def run(summary, by, mode, state, api, equity, cash, tag):
    """(anlık bildirimler, gün sonu notları). Anlık bildirim yalnız emir girerken üretilir."""
    ds = state.setdefault("dip", {})
    for k in ("pending", "held", "closing", "notes"):
        ds.setdefault(k, {})
    alerts, notes = [], []
    day = summary["lastBar"]
    hist = _orders(api)
    mine = dip_symbols(api, hist)
    positions = {p["symbol"]: p for p in api.positions() if p.get("asset_class", "us_equity") != "crypto"}
    orders = api.open_orders()
    pend_orders = {o["symbol"]: o for o in orders if o["side"] == "buy"
                   and str(o.get("client_order_id", "")).startswith(TAG_PREFIX)}
    held = {s: p for s, p in positions.items() if s in mine}

    # 1) Yeni dolumlar
    for s, p in held.items():
        if s in ds["held"]:
            continue
        e = _num(p["avg_entry_price"])
        fill = next((o for o in hist if o["symbol"] == s and o["side"] == "buy" and _num(o.get("filled_qty")) > 0
                     and str(o.get("client_order_id", "")).startswith(TAG_PREFIX)), {})
        fd, ft = _ts(fill)
        key = (ds["pending"].pop(s, None) or {}).get("key") or f"{fd}-{s}"
        ds["held"][s] = {"since": fd, "sessions": 0, "entry": e, "key": key, "hi": e, "lo": e}
        kayit.add_event(tarih=fd, saat=ft, sembol=s, islem="ALIŞ", adet=p["qty"], fiyat=round(e, 2),
                        neden=f"{STRAT}: limitli alış emri doldu", strateji=STRAT,
                        emir_id=fill.get("client_order_id"), anahtar=f"alis-{fill.get('id', key)}")
        kayit.upsert_signal(key, durum="açık", adet=p["qty"], alis_tarihi=fd, alis_saati=ft, alis_fiyati=round(e, 2),
                            stop=round(e * (1 - C.DIP_STOP), 2), kar_al=round(e * (1 + C.DIP_TP), 2))
        notes.append(f"🟢 {s} alındı · {p['qty']} adet @ ${e:,.2f}")

    # 2) En yüksek / en düşük takip (analiz için)
    for s, info in ds["held"].items():
        st, p = by.get(s) or {}, positions.get(s)
        vals = [x for x in (st.get("high"), st.get("low"), _num(p["current_price"]) if p else None) if x]
        if vals:
            info["hi"], info["lo"] = max([info["hi"]] + vals), min([info["lo"]] + vals)

    # 3) Kapanan pozisyonlar
    for s in list(ds["held"]):
        if s in held:
            continue
        info = ds["held"].pop(s)
        sell = next((o for o in hist if o["symbol"] == s and o["side"] == "sell" and _num(o.get("filled_qty")) > 0
                     and _ts(o)[0] >= info["since"]), None)
        px = _num(sell.get("filled_avg_price")) if sell else None
        closing = ds["closing"].pop(s, None)
        why = {"stop": f"stop (−%{C.DIP_STOP*100:g})", "limit": f"kâr al (+%{C.DIP_TP*100:g})"}.get(
            (sell or {}).get("type")) or closing or "piyasa emriyle kapandı (elle?)"
        fd, ft = _ts(sell)
        e = info["entry"]
        qty = _num((sell or {}).get("filled_qty"))
        pl = (px - e) * qty if px else None
        kayit.add_event(tarih=fd, saat=ft, sembol=s, islem="SATIŞ", adet=qty or None,
                        fiyat=round(px, 2) if px else None, neden=why, strateji=STRAT,
                        emir_id=(sell or {}).get("client_order_id"), anahtar=f"satis-{(sell or {}).get('id', s + fd)}")
        kayit.upsert_signal(info["key"], durum="kapandı", cikis_tarihi=fd, cikis_saati=ft,
                            cikis_fiyati=round(px, 2) if px else None, cikis_nedeni=why,
                            kar_zarar_usd=round(pl, 2) if pl is not None else None,
                            kar_zarar_yuzde=round((px / e - 1) * 100, 2) if px else None,
                            tutma_seans=info["sessions"], en_yuksek_yuzde=round((info["hi"] / e - 1) * 100, 2),
                            en_dusuk_yuzde=round((info["lo"] / e - 1) * 100, 2))
        notes.append(f"⚪ {s} kapandı · {why}" + (f" · {pl:+,.2f}$" if pl is not None else ""))

    # 4) Stop / kâr al seviyelerini gerçek alış fiyatına göre düzelt; koruma yoksa kur
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
            try:
                api.stop_sell(s, abs(int(float(p["qty"]))), want["stop"], f"dip-stop-{s}-{datetime.now():%m%d%H%M}")
                notes.append(f"🛡 {s} stop emri yoktu, yeniden kuruldu @ ${want['stop']:,.2f}")
            except RuntimeError as ex:
                notes.append(f"⚠️ {s} stop kurulamadı: {ex}")

    def _done():
        ds["notes"].setdefault(day, []).extend(notes)
        old = (datetime.utcnow() - timedelta(days=10)).strftime("%Y-%m-%d")
        for k in [k for k in ds["notes"] if k < old]:
            ds["notes"].pop(k)
        return alerts, notes

    if mode != "post":
        return _done()
    key = f"dipday|{day}"
    if key in state["sent"]:
        return _done()
    state["sent"][key] = day

    # 5) Süre sayaçları: dolmayan alış 2 seans sonra iptal, pozisyon 10 seans sonra kapatılır
    for s, o in pend_orders.items():
        pd_ = ds["pending"].setdefault(s, {"since": day, "sessions": 0, "key": f"{day}-{s}"})
        pd_["sessions"] += 1
        if pd_["sessions"] >= C.DIP_ENTRY_SESSIONS:
            try:
                api.cancel(o["id"])
                ds["pending"].pop(s, None)
                kayit.add_event(sembol=s, islem="İPTAL", adet=o.get("qty"), fiyat=o.get("limit_price"),
                                neden=f"limitli alış {C.DIP_ENTRY_SESSIONS} seansta dolmadı", strateji=STRAT,
                                emir_id=o.get("client_order_id"), anahtar=f"iptal-{o['id']}")
                kayit.upsert_signal(pd_["key"], durum="iptal (dolmadı)")
                notes.append(f"⏹ {s} alış emri {C.DIP_ENTRY_SESSIONS} seansta dolmadı, iptal edildi")
            except RuntimeError as ex:
                print(ex)
    for s in list(ds["pending"]):
        if s not in pend_orders and s not in held:
            ds["pending"].pop(s, None)
    for s, info in ds["held"].items():
        info["sessions"] += 1
        if info["sessions"] >= C.DIP_MAX_DAYS and s not in ds["closing"]:
            for o in orders:
                if o["symbol"] == s and o["side"] == "sell":
                    try:
                        api.cancel(o["id"])
                    except RuntimeError as ex:
                        print(ex)
            try:
                api.close_position(s)
                ds["closing"][s] = f"süre doldu ({C.DIP_MAX_DAYS} seans)"
                notes.append(f"🔔 {s} {C.DIP_MAX_DAYS} seanstır stop/hedef olmadı, açılışta satılacak")
            except RuntimeError as ex:
                notes.append(f"⚠️ {s} kapatılamadı: {ex}")

    # 6) Yeni sinyaller -> limitli alış emirleri (anlık bildirim)
    open_pend = {s for s in pend_orders if s in ds["pending"]}
    slots = C.DIP_MAX_POSITIONS - len(held) - len(open_pend)
    cands = sorted([s for s in summary["stocks"] if s.get("isTop") and (s.get("dip") or {}).get("signal")
                    and (s.get("sharia") or {}).get("ok")], key=lambda s: s["dip"]["drop3"])
    room = max(cash, 0.0) * 0.98
    placed = []
    for rank, s in enumerate(cands, 1):
        t = s["t"]
        skey = f"{day}-{t}"
        ctx = _context(s, rank, len(cands), summary)
        if t in positions or t in pend_orders:
            kayit.upsert_signal(skey, durum="atlandı (zaten elde/emirde)", **ctx)
            continue
        if slots <= 0:
            kayit.upsert_signal(skey, durum=f"atlandı ({C.DIP_MAX_POSITIONS} pozisyon dolu)", **ctx)
            notes.append(f"ℹ️ {t} sinyal verdi ama {C.DIP_MAX_POSITIONS} pozisyon dolu")
            continue
        lim = s["dip"]["limit"]
        qty = math.floor(min(equity / C.DIP_MAX_POSITIONS, room) / lim)
        if qty < 1:
            kayit.upsert_signal(skey, durum="atlandı (nakit yok)", **ctx)
            notes.append(f"⚠️ {t} sinyal verdi ama yeterli nakit yok")
            continue
        stop, tp = round(lim * (1 - C.DIP_STOP), 2), round(lim * (1 + C.DIP_TP), 2)
        cid = f"{TAG_PREFIX}{day}-{t}"
        why = (f"3 gün üst üste düştü ({s['dip']['drop3']:+.1f}%), 200 gün ort. üstünde; "
               f"limit = kapanış ${s['dip']['close']:,.2f} − %{C.DIP_LIMIT_BELOW*100:g}")
        try:
            api.bracket_buy(t, qty, lim, stop, tp, cid)
        except RuntimeError as ex:
            kayit.upsert_signal(skey, durum=f"emir hatası: {str(ex)[:80]}", **ctx)
            notes.append(f"⚠️ {t} alış emri verilemedi: {ex}")
            continue
        ds["pending"][t] = {"since": day, "sessions": 0, "limit": lim, "key": skey, "qty": qty}
        slots -= 1
        room -= qty * lim
        kayit.upsert_signal(skey, durum="emir verildi", adet=qty, stop=stop, kar_al=tp, **ctx)
        kayit.add_event(sembol=t, islem="EMİR", adet=qty, fiyat=lim, neden=why, strateji=STRAT, emir_id=cid,
                        anahtar=f"emir-{cid}")
        placed.append(f"• <b>{t}</b> {qty} adet @ ${lim:,.2f} (≈${qty*lim:,.0f})\n"
                      f"   Stop ${stop:,.2f} · Kâr al ${tp:,.2f} · {C.DIP_ENTRY_SESSIONS} seans geçerli\n"
                      f"   Neden: {why}")
        notes.append(f"📥 {t} limitli alış emri girildi @ ${lim:,.2f}")
    if placed:
        alerts.append(f"📥 <b>Emir giriyorum</b> ({tag}) – {day} kapanışına göre limitli alış:\n" + "\n".join(placed))
    return _done()
