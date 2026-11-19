"""Orta vade stratejisi – evre 2 alımı (kurallar: engine/ortavade_kural.py, test: tools/ortavade_test.py).

Akış (kapanıştan sonra, 'post' modunda günde bir kez):
  1) Sinyal: İslami filtreden geçen ilk 50'de 1 yıllık grafikte evre 2 + 3 aylık grafikte taban kırılımı ya da
     50 g ortalamaya geri çekilip dönüş. Aynı gün birden çok sinyal varsa son 3 ayda en güçlü olan önce.
  2) Alış: kapanışın en çok OV_LIMIT_ABOVE üstüne limitli alış + bağlı -OV_STOP stop (OTO, GTC). Ertesi seans
     dolmazsa iptal (fiyat boşlukla kaçtıysa kovalanmaz).
  3) Satış: stop gün içinde; ayrıca kapanış 50 g ortalamanın altına inince (OV_EXIT) ertesi açılışta satılır.
     Hisse İslami filtreden çıkarsa satılır.
  4) Boyut: sermayenin OV_ALLOC payı / OV_MAX_POSITIONS. Emir kimliği 'ov-' ile başlar; kısa vade (dip-)
     ve günlük işlem (dt-) pozisyonlarına dokunmaz.
"""
import math
from datetime import datetime, timedelta

from . import config as C
from . import kayit

TAG_PREFIX = "ov-"
STRAT = "orta vade"
OPEN_ST = ("new", "accepted", "held", "partially_filled", "pending_new")


def _num(x):
    return float(x or 0)


def _orders(api, days=400):
    if not hasattr(api, "orders"):
        return []
    after = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%dT00:00:00Z")
    try:
        return api.orders("all", after=after)
    except RuntimeError as e:
        print("emir geçmişi okunamadı:", e)
        return []


def last_buy_owner(hist):
    """sembol -> en son dolan alış emrinin kimliği (hangi stratejinin pozisyonu olduğunu belirler)."""
    best = {}
    for o in hist:
        if o.get("side") != "buy" or _num(o.get("filled_qty")) <= 0:
            continue
        t = str(o.get("filled_at") or o.get("updated_at") or "")
        if o["symbol"] not in best or t > best[o["symbol"]][0]:
            best[o["symbol"]] = (t, str(o.get("client_order_id", "")))
    return {s: v[1] for s, v in best.items()}


def ov_symbols(api, hist=None):
    """Elde 'ov-' ile alınmış ya da bekleyen 'ov-' alışı olan semboller."""
    hist = _orders(api) if hist is None else hist
    own = {s for s, cid in last_buy_owner(hist).items() if cid.startswith(TAG_PREFIX)}
    pend = {o["symbol"] for o in hist if str(o.get("client_order_id", "")).startswith(TAG_PREFIX)
            and o.get("side") == "buy" and o.get("status") in OPEN_ST}
    return own | pend


def _ts(o, k="filled_at"):
    v = (o or {}).get(k) or (o or {}).get("updated_at") or (o or {}).get("submitted_at")
    if not v:
        n = kayit.now_ny()
        return n.strftime("%Y-%m-%d"), n.strftime("%H:%M")
    t = datetime.fromisoformat(str(v).replace("Z", "+00:00")).astimezone(kayit.NY)
    return t.strftime("%Y-%m-%d"), t.strftime("%H:%M")


KIND_TR = {"kirilim": "3 aylık taban kırılımı", "geri": "evre 2'de 50 g ortalamaya geri çekilip dönüş"}


def why_text(o):
    k = o.get("kind")
    base = KIND_TR.get(k, k)
    if k == "kirilim":
        base += (f" (taban derinliği %{o['baseDepth']:.0f}, hacim {o['volr']:.1f}x, 3 ay zirvesi ${o['hi63']:,.2f})")
    else:
        base += f" (50 g ort. ${o['sma50']:,.2f})"
    return (f"{base}; 1 yıllık grafik evre 2: 150 g ort. yükseliyor, fiyat ortalamanın %{o['ext150']:+.0f} üstünde, "
            f"son 3 ay %{o['ret63']:+.0f}")


def _context(s, rank, n_same, summary):
    o = s["ov"]
    b = summary.get("benchmark") or {}
    sh = s.get("sharia") or {}
    return {"sinyal_tarihi": summary["lastBar"], "sembol": s["t"], "sirket": s.get("name"), "sektor": s.get("sector"),
            "strateji": STRAT, "kapanis": o["close"], "limit": o["limit"], "giris_turu": KIND_TR.get(o["kind"]),
            "evre": o.get("stage"), "taban_derinlik_yuzde": o.get("baseDepth"), "sma150_uzaklik_yuzde": o.get("ext150"),
            "getiri_3a_yuzde": o.get("ret63"), "rsi14": s.get("rsi"), "hacim_orani": o.get("volr"),
            "atr_yuzde": round(s["atr"] / s["price"] * 100, 2) if s.get("atr") and s.get("price") else None,
            "puan": s.get("score"), "piyasa_degeri_mr": round(s["mcap"] / 1e9, 1) if s.get("mcap") else None,
            "borc_orani": sh.get("debt"), "nakit_orani": sh.get("cash"),
            "spy_trend": b.get("trend"), "spy_gun_yuzde": b.get("chg"), "sira": rank, "ayni_gun_sinyal": n_same}


def exit_reason(snap, info):
    """Kapanışa göre çıkış nedeni ya da None."""
    if not snap or snap.get("close") is None:
        return None
    c = snap["close"]
    buf = 1 - C.OV_EXIT_BUFFER
    if C.OV_EXIT == "sma50" and snap.get("sma50") and c < snap["sma50"] * buf:
        return f"kapanış 50 g ortalamanın altında (${c:,.2f} < ${snap['sma50']:,.2f}) – yükseliş evresi bozuldu"
    if C.OV_EXIT == "sma150" and snap.get("sma150") and c < snap["sma150"] * buf:
        return f"kapanış 150 g ortalamanın altında (${c:,.2f} < ${snap['sma150']:,.2f}) – düşüş evresi"
    return None


def candidates(summary):
    if C.OV_MARKET_FILTER and (summary.get("benchmark") or {}).get("above200") is False:
        return []
    return sorted([s for s in summary["stocks"] if s.get("isTop") and (s.get("ov") or {}).get("signal")
                   and (s.get("sharia") or {}).get("ok")], key=lambda s: -(s["ov"].get("ret63") or -999))


def run(summary, by, mode, state, api, pocket, cash, tag):
    """pocket: orta vadeye ayrılan sermaye. (anlık bildirimler, notlar) döner."""
    vs = state.setdefault("ov", {})
    for k in ("pending", "held", "closing", "notes"):
        vs.setdefault(k, {})
    alerts, notes = [], []
    day = summary["lastBar"]
    hist = _orders(api)
    mine = ov_symbols(api, hist)
    positions = {p["symbol"]: p for p in api.positions() if p.get("asset_class", "us_equity") != "crypto"}
    orders = api.open_orders()
    pend_orders = {o["symbol"]: o for o in orders if o["side"] == "buy"
                   and str(o.get("client_order_id", "")).startswith(TAG_PREFIX)}
    held = {s: p for s, p in positions.items() if s in mine}

    # 1) Yeni dolumlar
    for s, p in held.items():
        if s in vs["held"]:
            continue
        e = _num(p["avg_entry_price"])
        fill = next((o for o in hist if o["symbol"] == s and o["side"] == "buy" and _num(o.get("filled_qty")) > 0
                     and str(o.get("client_order_id", "")).startswith(TAG_PREFIX)), {})
        fd, ft = _ts(fill)
        key = (vs["pending"].pop(s, None) or {}).get("key") or f"ov-{fd}-{s}"
        vs["held"][s] = {"since": fd, "sessions": 0, "entry": e, "key": key, "hi": e, "lo": e, "hic": e,
                         "stop": round(e * (1 - C.OV_STOP), 2)}
        kayit.add_event(tarih=fd, saat=ft, sembol=s, islem="ALIŞ", adet=p["qty"], fiyat=round(e, 2),
                        neden=f"{STRAT}: limitli alış emri doldu", strateji=STRAT,
                        emir_id=fill.get("client_order_id"), anahtar=f"alis-{fill.get('id', key)}")
        kayit.upsert_signal(key, durum="açık", adet=p["qty"], alis_tarihi=fd, alis_saati=ft, alis_fiyati=round(e, 2),
                            stop=vs["held"][s]["stop"])
        notes.append(f"🟢 {s} (orta vade) alındı · {p['qty']} adet @ ${e:,.2f}")

    # 2) En yüksek / en düşük takip
    for s, info in vs["held"].items():
        st, p = by.get(s) or {}, positions.get(s)
        vals = [x for x in (st.get("high"), st.get("low"), _num(p["current_price"]) if p else None) if x]
        if vals:
            info["hi"], info["lo"] = max([info["hi"]] + vals), min([info["lo"]] + vals)

    # 3) Kapanan pozisyonlar
    for s in list(vs["held"]):
        if s in held:
            continue
        info = vs["held"].pop(s)
        sell = next((o for o in hist if o["symbol"] == s and o["side"] == "sell" and _num(o.get("filled_qty")) > 0
                     and _ts(o)[0] >= info["since"]), None)
        px = _num(sell.get("filled_avg_price")) if sell else None
        closing = vs["closing"].pop(s, None)
        why = closing or {"stop": f"stop (−%{C.OV_STOP*100:g})", "limit": "kâr al"}.get(
            (sell or {}).get("type")) or "piyasa emriyle kapandı (elle?)"
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
        notes.append(f"⚪ {s} (orta vade) kapandı · {why}" + (f" · {pl:+,.2f}$" if pl is not None else ""))

    # 4) Stop: gerçek alış fiyatına göre (iz süren modda yalnız yukarı); stop emri yoksa kur
    for s, p in held.items():
        info = vs["held"].get(s) or {}
        if s in vs["closing"]:
            continue
        want = info.get("stop") or round(_num(p["avg_entry_price"]) * (1 - C.OV_STOP), 2)
        if C.OV_TP:
            tp = round(_num(p["avg_entry_price"]) * (1 + C.OV_TP), 2)
            for o in orders:
                if (o["symbol"] == s and o["side"] == "sell" and o.get("type") == "limit"
                        and abs(_num(o.get("limit_price")) - tp) > 0.011):
                    try:
                        api.replace_limit(o["id"], tp)
                    except RuntimeError as ex:
                        print(ex)
        stops = [o for o in orders if o["symbol"] == s and o["side"] == "sell" and o.get("type") == "stop"]
        for o in stops:
            if abs(_num(o.get("stop_price")) - want) > 0.011:
                try:
                    api.replace_stop(o["id"], want)
                except RuntimeError as ex:
                    print(ex)
        if not stops:
            try:
                api.stop_sell(s, abs(int(float(p["qty"]))), want, f"ov-stop-{s}-{datetime.now():%m%d%H%M}")
                notes.append(f"🛡 {s} (orta vade) stop emri yoktu, kuruldu @ ${want:,.2f}")
            except RuntimeError as ex:
                notes.append(f"⚠️ {s} stop kurulamadı: {ex}")

    def _done():
        vs["notes"].setdefault(day, []).extend(notes)
        old = (datetime.utcnow() - timedelta(days=10)).strftime("%Y-%m-%d")
        for k in [k for k in vs["notes"] if k < old]:
            vs["notes"].pop(k)
        return alerts, notes

    if mode != "post":
        return _done()
    key = f"ovday|{day}"
    if key in state["sent"]:
        return _done()
    state["sent"][key] = day

    # 5) Dolmayan alışlar OV_ENTRY_SESSIONS seans sonra iptal
    for s, o in pend_orders.items():
        pd_ = vs["pending"].setdefault(s, {"since": day, "sessions": 0, "key": f"ov-{day}-{s}"})
        pd_["sessions"] += 1
        if pd_["sessions"] >= C.OV_ENTRY_SESSIONS:
            try:
                api.cancel(o["id"])
                vs["pending"].pop(s, None)
                kayit.add_event(sembol=s, islem="İPTAL", adet=o.get("qty"), fiyat=o.get("limit_price"),
                                neden="orta vade limitli alış dolmadı (fiyat kaçtı)", strateji=STRAT,
                                emir_id=o.get("client_order_id"), anahtar=f"iptal-{o['id']}")
                kayit.upsert_signal(pd_["key"], durum="iptal (dolmadı)")
                notes.append(f"⏹ {s} orta vade alış emri dolmadı, iptal edildi")
            except RuntimeError as ex:
                print(ex)
    for s in list(vs["pending"]):
        if s not in pend_orders and s not in held:
            vs["pending"].pop(s, None)

    # 6) Çıkış kuralları (kapanışa göre; satış ertesi açılışta) + iz süren stop
    for s, info in vs["held"].items():
        info["sessions"] += 1
        if s in vs["closing"] or s not in held:
            continue
        st = by.get(s) or {}
        snap = st.get("ov") or {}
        if snap.get("close"):
            info["hic"] = max(info.get("hic", 0), snap["close"])
        why = exit_reason(snap, info)
        if not why and info["sessions"] >= C.OV_MAX_DAYS:
            why = f"süre doldu ({C.OV_MAX_DAYS} seans)"
        shr = st.get("sharia") or {}
        if not why and C.SHARIA_FILTER and C.SHARIA_SELL_IF_FAILS and shr and not shr.get("ok", True) and shr.get("hard"):
            why = f"İslami filtreden çıktı ({shr.get('reason')})"
        if why:
            for o in orders:
                if o["symbol"] == s and o["side"] == "sell":
                    try:
                        api.cancel(o["id"])
                    except RuntimeError as ex:
                        print(ex)
            try:
                api.close_position(s)
                vs["closing"][s] = why
                notes.append(f"🔔 {s} (orta vade) satılacak (açılışta): {why}")
            except RuntimeError as ex:
                notes.append(f"⚠️ {s} kapatılamadı: {ex}")
            continue
        if C.OV_EXIT == "atr" and snap.get("atr"):
            new = round(info["hic"] - C.OV_TRAIL_ATR * snap["atr"], 2)
            if new > info.get("stop", 0):
                info["stop"] = new
                notes.append(f"⬆️ {s} (orta vade) iz süren stop ${new:,.2f}")

    # 7) Yeni sinyaller -> limitli alış + bağlı stop
    open_pend = {s for s in pend_orders if s in vs["pending"]}
    slots = C.OV_MAX_POSITIONS - len(held) - len(open_pend)
    cands = candidates(summary)
    any_pend = {o["symbol"] for o in orders if o["side"] == "buy"}
    room = max(cash, 0.0) * 0.98
    placed = []
    for rank, s in enumerate(cands, 1):
        t = s["t"]
        skey = f"ov-{day}-{t}"
        ctx = _context(s, rank, len(cands), summary)
        if t in positions or t in any_pend:
            kayit.upsert_signal(skey, durum="atlandı (zaten elde/emirde)", **ctx)
            continue
        if slots <= 0:
            kayit.upsert_signal(skey, durum=f"atlandı ({C.OV_MAX_POSITIONS} orta vade pozisyonu dolu)", **ctx)
            notes.append(f"ℹ️ {t} orta vade sinyali verdi ama {C.OV_MAX_POSITIONS} pozisyon dolu")
            continue
        lim = s["ov"]["limit"]
        qty = math.floor(min(pocket / C.OV_MAX_POSITIONS, room) / lim)
        if qty < 1:
            kayit.upsert_signal(skey, durum="atlandı (nakit yok)", **ctx)
            notes.append(f"⚠️ {t} orta vade sinyali verdi ama yeterli nakit yok")
            continue
        stop = round(lim * (1 - C.OV_STOP), 2)
        tp = round(lim * (1 + C.OV_TP), 2) if C.OV_TP else None
        cid = f"{TAG_PREFIX}{day}-{t}"
        why = why_text(s["ov"])
        try:
            api.bracket_buy(t, qty, lim, stop, tp, cid)
        except RuntimeError as ex:
            kayit.upsert_signal(skey, durum=f"emir hatası: {str(ex)[:80]}", **ctx)
            notes.append(f"⚠️ {t} orta vade alış emri verilemedi: {ex}")
            continue
        vs["pending"][t] = {"since": day, "sessions": 0, "limit": lim, "key": skey, "qty": qty}
        slots -= 1
        room -= qty * lim
        kayit.upsert_signal(skey, durum="emir verildi", adet=qty, stop=stop, kar_al=tp, **ctx)
        kayit.add_event(sembol=t, islem="EMİR", adet=qty, fiyat=lim, neden=why, strateji=STRAT, emir_id=cid,
                        anahtar=f"emir-{cid}")
        exit_txt = {"yok": "", "sma50": "kapanış 50 g ortalamanın altına inince", "sma150": "kapanış 150 g ortalamanın altına inince",
                    "atr": f"iz süren stop ({C.OV_TRAIL_ATR:g} ATR)"}.get(C.OV_EXIT, "")
        placed.append(f"• <b>{t}</b> {qty} adet @ ${lim:,.2f} (≈${qty*lim:,.0f})\n"
                      f"   Stop ${stop:,.2f} (−%{C.OV_STOP*100:g})" + (f" · Kâr al ${tp:,.2f}" if tp else "")
                      + (f" · Satış: {exit_txt}" if exit_txt else "") + "\n"
                      f"   Neden: {why}")
        notes.append(f"📥 {t} orta vade limitli alış emri girildi @ ${lim:,.2f}")
    if placed:
        alerts.append(f"📈 <b>Orta vade – emir giriyorum</b> ({tag}) – {day} kapanışına göre:\n" + "\n".join(placed))
    return _done()
