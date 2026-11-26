"""Orta vade stratejisi (kullanıcının kuralı): düşüş sonrası dipte yatay + kademeli alım.

Kurallar: engine/ortavade_kural.py (features_taban / setup_taban), test: tools/taban_test.py.
  Sinyal : İslami filtreden geçen ilk 50'de; 1 yıllık grafikte dipten tepeye >= %40 yükseliş, tepeden dibe
           >= %25 düşüş; 3 aylık grafikte dipten sonra en az 3 seans yeni dip yok ve fiyat dibin en çok %10 üstünde.
  Alış   : kademeli, TB_TRANCHES parça (varsayılan 3: ilk alış günü, 3. ve 6. seans – hâlâ dipte/yataydaysa).
           Her parça kapanıştan sonra, ertesi seans için kapanışın en çok %1 üstüne limitli alış (1 seans geçerli).
           Her parçaya bağlı stop (dibin TB_STOP_BELOW altı) ve kâr al (ortalama maliyet + TB_TP).
  Satış  : stop (dip kırıldı -> yatay bitti), kâr al, en fazla TB_MAX_DAYS seans, İslami filtreden çıkarsa.
  Boyut  : pozisyon bütçesi = orta vade payı / OV_MAX_POSITIONS, ilk alışta sabitlenir; her parça bütçenin 1/n'i.
Emir kimliği 'ov-' ile başlar; kısa vade (dip-) ve günlük işlem (dt-) pozisyonlarına dokunmaz.
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
    """sembol -> en son dolan alış emrinin kimliği (pozisyonun hangi stratejiye ait olduğu)."""
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


def n_parts():
    return len(C.TB_TRANCHES)


def why_text(o, k=1):
    return (f"{k}/{n_parts()}. parça · dipten tepeye %{o['rise']:.0f} yükseliş, tepeden %{o['drop']:.0f} düşüş, "
            f"{o['flat']} seanstır yeni dip yok (dip ${o['bottom']:,.2f}), fiyat dibin %{o['near']:.1f} üstünde")


def _context(s, rank, n_same, summary):
    o = s["ov"]
    b = summary.get("benchmark") or {}
    sh = s.get("sharia") or {}
    return {"sinyal_tarihi": summary["lastBar"], "sembol": s["t"], "sirket": s.get("name"), "sektor": s.get("sector"),
            "strateji": STRAT, "kapanis": o["close"], "limit": o["limit"], "giris_turu": "dipte yatay (kademeli)",
            "evre": f"yükseliş %{o['rise']:.0f} / düşüş %{o['drop']:.0f} / yatay {o['flat']} seans",
            "getiri_3a_yuzde": o.get("ret63"), "rsi14": s.get("rsi"),
            "atr_yuzde": round(s["atr"] / s["price"] * 100, 2) if s.get("atr") and s.get("price") else None,
            "hacim_orani": s.get("volr"), "puan": s.get("score"),
            "piyasa_degeri_mr": round(s["mcap"] / 1e9, 1) if s.get("mcap") else None,
            "borc_orani": sh.get("debt"), "nakit_orani": sh.get("cash"),
            "spy_trend": b.get("trend"), "spy_gun_yuzde": b.get("chg"), "sira": rank, "ayni_gun_sinyal": n_same}


def candidates(summary):
    return sorted([s for s in summary["stocks"] if s.get("isTop") and (s.get("ov") or {}).get("signal")
                   and (s.get("sharia") or {}).get("ok")], key=lambda s: -(s["ov"].get("ret63") or -999))


def _missed_line(t, part, qty, lim, stop, tp, room, why):
    """Nakit yetmediği için verilemeyen parçanın 'para olsaydı' özeti (Telegram)."""
    if qty < 1:
        size = f"1 hisse (${lim:,.2f}) bile parça bütçesini aşıyor"
    else:
        size = f"{qty} adet @ ${lim:,.2f} (≈${qty*lim:,.0f})"
    cash_txt = f" · eldeki nakit ${max(room, 0):,.0f}" if room is not None else ""
    return (f"• <b>{t}</b> {part}/{n_parts()}. parça · {size}{cash_txt}\n"
            f"   Stop ${stop:,.2f} · Kâr al ≈${tp:,.2f}\n"
            f"   Neden: {why}")


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

    # 1) Yeni dolumlar (ilk parça ya da ek parça)
    for s, p in held.items():
        q, e = _num(p["qty"]), _num(p["avg_entry_price"])
        info = vs["held"].get(s)
        if info and q <= info.get("qty", 0) + 1e-9:
            continue
        fill = next((o for o in hist if o["symbol"] == s and o["side"] == "buy" and _num(o.get("filled_qty")) > 0
                     and str(o.get("client_order_id", "")).startswith(TAG_PREFIX)), {})
        fd, ft = _ts(fill)
        pend = vs["pending"].pop(s, None) or {}
        if not info:
            key = pend.get("key") or f"ov-{fd}-{s}"
            snap = (by.get(s) or {}).get("ov") or {}
            info = vs["held"][s] = {"since": fd, "posts": 0, "parts": 0, "qty": 0.0, "entry": e, "key": key,
                                    "hi": e, "lo": e, "bottom": pend.get("bottom") or snap.get("bottom"),
                                    "budget": pend.get("budget") or pocket / C.OV_MAX_POSITIONS}
        added = q - info["qty"]
        info["parts"] = min(info["parts"] + 1, n_parts())
        info["qty"], info["entry"] = q, e
        px = _num(fill.get("filled_avg_price")) or e
        kayit.add_event(tarih=fd, saat=ft, sembol=s, islem="ALIŞ", adet=round(added, 4), fiyat=round(px, 2),
                        neden=f"{STRAT}: {info['parts']}/{n_parts()}. parça doldu (ort. maliyet ${e:,.2f})",
                        strateji=STRAT, emir_id=fill.get("client_order_id"), anahtar=f"alis-{fill.get('id', s + fd)}")
        kayit.upsert_signal(info["key"], durum="açık", adet=q, alis_tarihi=info["since"], alis_fiyati=round(e, 2),
                            stop=round(info["bottom"] * (1 - C.TB_STOP_BELOW), 2) if info.get("bottom") else None,
                            kar_al=round(e * (1 + C.TB_TP), 2))
        notes.append(f"🟢 {s} (orta vade) {info['parts']}/{n_parts()}. parça alındı · {added:g} adet @ ${px:,.2f}")

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
        sells = [o for o in hist if o["symbol"] == s and o["side"] == "sell" and _num(o.get("filled_qty")) > 0
                 and _ts(o)[0] >= info["since"]]
        qty = sum(_num(o.get("filled_qty")) for o in sells)
        px = (sum(_num(o.get("filled_avg_price")) * _num(o.get("filled_qty")) for o in sells) / qty) if qty else None
        last = sells[0] if sells else None
        closing = vs["closing"].pop(s, None)
        why = closing or {"stop": f"stop (dibin %{C.TB_STOP_BELOW*100:g} altı – dip kırıldı)",
                          "limit": f"kâr al (+%{C.TB_TP*100:g})"}.get((last or {}).get("type")) \
            or "piyasa emriyle kapandı (elle?)"
        fd, ft = _ts(last)
        e = info["entry"]
        pl = (px - e) * qty if px else None
        kayit.add_event(tarih=fd, saat=ft, sembol=s, islem="SATIŞ", adet=qty or None,
                        fiyat=round(px, 2) if px else None, neden=why, strateji=STRAT,
                        emir_id=(last or {}).get("client_order_id"), anahtar=f"satis-{(last or {}).get('id', s + fd)}")
        kayit.upsert_signal(info["key"], durum="kapandı", cikis_tarihi=fd, cikis_saati=ft,
                            cikis_fiyati=round(px, 2) if px else None, cikis_nedeni=why,
                            kar_zarar_usd=round(pl, 2) if pl is not None else None,
                            kar_zarar_yuzde=round((px / e - 1) * 100, 2) if px else None,
                            tutma_seans=info.get("posts"), en_yuksek_yuzde=round((info["hi"] / e - 1) * 100, 2),
                            en_dusuk_yuzde=round((info["lo"] / e - 1) * 100, 2))
        notes.append(f"⚪ {s} (orta vade) kapandı · {why}" + (f" · {pl:+,.2f}$" if pl is not None else ""))

    # 4) Koruma: tüm parçaların stopu aynı (dibin altı), kâr al ortalama maliyete göre; korunmayan adet varsa OCO kur
    for s, p in held.items():
        info = vs["held"].get(s) or {}
        if s in vs["closing"]:
            continue
        e = _num(p["avg_entry_price"])
        bottom = info.get("bottom") or ((by.get(s) or {}).get("ov") or {}).get("bottom")
        stop = round(bottom * (1 - C.TB_STOP_BELOW), 2) if bottom else round(e * 0.85, 2)
        tp = round(e * (1 + C.TB_TP), 2)
        sells = [o for o in orders if o["symbol"] == s and o["side"] == "sell"]
        for o in sells:
            try:
                if o.get("type") == "stop" and abs(_num(o.get("stop_price")) - stop) > 0.011:
                    api.replace_stop(o["id"], stop)
                elif o.get("type") == "limit" and abs(_num(o.get("limit_price")) - tp) > 0.011:
                    api.replace_limit(o["id"], tp)
            except RuntimeError as ex:
                print(ex)
        covered = sum(_num(o.get("qty")) - _num(o.get("filled_qty")) for o in sells if o.get("type") == "stop")
        missing = int(_num(p["qty"]) - covered)
        if missing >= 1:
            try:
                api.oco_sell(s, missing, tp, stop, f"ov-oco-{s}-{datetime.now():%m%d%H%M}")
                notes.append(f"🛡 {s} (orta vade) {missing} adet korumasızdı, stop ${stop:,.2f} / kâr al ${tp:,.2f} kuruldu")
            except RuntimeError as ex:
                notes.append(f"⚠️ {s} koruma emri kurulamadı: {ex}")

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

    # 5) Dolmayan alışlar 1 seans sonra iptal
    for s, o in pend_orders.items():
        pd_ = vs["pending"].setdefault(s, {"since": day, "sessions": 0, "key": f"ov-{day}-{s}"})
        pd_["sessions"] += 1
        if pd_["sessions"] >= C.OV_ENTRY_SESSIONS:
            try:
                api.cancel(o["id"])
                vs["pending"].pop(s, None)
                kayit.add_event(sembol=s, islem="İPTAL", adet=o.get("qty"), fiyat=o.get("limit_price"),
                                neden=f"orta vade {pd_.get('part', 1)}. parça limitli alış dolmadı", strateji=STRAT,
                                emir_id=o.get("client_order_id"), anahtar=f"iptal-{o['id']}")
                if s not in held:
                    kayit.upsert_signal(pd_["key"], durum="iptal (dolmadı)")
                notes.append(f"⏹ {s} orta vade {pd_.get('part', 1)}. parça alışı dolmadı, iptal edildi")
            except RuntimeError as ex:
                print(ex)
    pend_orders = {s: o for s, o in pend_orders.items() if s in vs["pending"]}
    for s in list(vs["pending"]):
        if s not in pend_orders:
            vs["pending"].pop(s, None)

    # 6) Elde olanlar: süre, İslami filtre, sonraki parça (ilk alıştan g seans sonra, hâlâ dipte/yataydaysa)
    room = max(cash, 0.0) * 0.98
    placed, missed, no_slot = [], [], []
    for s, info in vs["held"].items():
        if s not in held or s in vs["closing"]:
            continue
        day_n = info["posts"]               # ilk alış günü kapanışı = 0
        info["posts"] += 1
        st = by.get(s) or {}
        snap = st.get("ov") or {}
        shr = st.get("sharia") or {}
        why = None
        if day_n >= C.TB_MAX_DAYS:
            why = f"süre doldu ({C.TB_MAX_DAYS} seans)"
        elif C.SHARIA_FILTER and C.SHARIA_SELL_IF_FAILS and shr and not shr.get("ok", True) and shr.get("hard"):
            why = f"İslami filtreden çıktı ({shr.get('reason')})"
        if why:
            for o in orders:
                if o["symbol"] == s:
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
        if (day_n in C.TB_TRANCHES[1:] and info["parts"] < n_parts() and snap.get("signal")
                and s not in pend_orders):
            part = info["parts"] + 1
            lim = snap["limit"]
            qty = math.floor(min(info["budget"] / n_parts(), room) / lim)
            stop = round((info.get("bottom") or snap["bottom"]) * (1 - C.TB_STOP_BELOW), 2)
            tp = round(info["entry"] * (1 + C.TB_TP), 2)
            if qty < 1:
                notes.append(f"⚠️ {s} {part}. parça için nakit yok")
                full = math.floor(info["budget"] / n_parts() / lim)
                missed.append(_missed_line(s, part, full, lim, stop, tp, room, why_text(snap, part)))
                continue
            cid = f"{TAG_PREFIX}{day}-{s}-{part}"
            try:
                api.bracket_buy(s, qty, lim, stop, tp, cid)
            except RuntimeError as ex:
                notes.append(f"⚠️ {s} {part}. parça emri verilemedi: {ex}")
                continue
            room -= qty * lim
            vs["pending"][s] = {"since": day, "sessions": 0, "limit": lim, "key": info["key"], "qty": qty, "part": part}
            why_ = why_text(snap, part)
            kayit.add_event(sembol=s, islem="EMİR", adet=qty, fiyat=lim, neden=why_, strateji=STRAT, emir_id=cid,
                            anahtar=f"emir-{cid}")
            placed.append(f"• <b>{s}</b> {part}/{n_parts()}. parça · {qty} adet @ ${lim:,.2f} (≈${qty*lim:,.0f})\n"
                          f"   Stop ${stop:,.2f} (dibin %{C.TB_STOP_BELOW*100:g} altı) · Kâr al ≈${tp:,.2f}\n   Neden: {why_}")
            notes.append(f"📥 {s} orta vade {part}. parça emri girildi @ ${lim:,.2f}")

    # 7) Yeni pozisyonlar: ilk parça
    open_new = {s for s in vs["pending"] if s not in held}
    slots = C.OV_MAX_POSITIONS - len(held) - len(open_new)
    cands = candidates(summary)
    any_pend = {o["symbol"] for o in orders if o["side"] == "buy"}
    budget = pocket / C.OV_MAX_POSITIONS
    for rank, s in enumerate(cands, 1):
        t = s["t"]
        skey = f"ov-{day}-{t}"
        if t in positions or t in any_pend or t in vs["pending"]:
            continue
        ctx = _context(s, rank, len(cands), summary)
        o = s["ov"]
        lim = o["limit"]
        stop = o["stop"]
        tp = round(lim * (1 + C.TB_TP), 2)
        if slots <= 0:
            full = math.floor(budget / n_parts() / lim)
            kayit.upsert_signal(skey, durum=f"atlandı ({C.OV_MAX_POSITIONS} orta vade pozisyonu dolu)", adet=full,
                                stop=stop, kar_al=tp, **ctx)
            notes.append(f"ℹ️ {t} orta vade sinyali verdi ama {C.OV_MAX_POSITIONS} pozisyon dolu")
            no_slot.append(_missed_line(t, 1, full, lim, stop, tp, None, why_text(o, 1)))
            continue
        qty = math.floor(min(budget / n_parts(), room) / lim)
        if qty < 1:
            full = math.floor(budget / n_parts() / lim)
            kayit.upsert_signal(skey, durum="atlandı (nakit yok)", adet=full, stop=stop, kar_al=tp, **ctx)
            notes.append(f"⚠️ {t} orta vade sinyali verdi ama yeterli nakit yok")
            missed.append(_missed_line(t, 1, full, lim, stop, tp, room, why_text(o, 1)))
            continue
        cid = f"{TAG_PREFIX}{day}-{t}-1"
        why_ = why_text(o, 1)
        try:
            api.bracket_buy(t, qty, lim, stop, tp, cid)
        except RuntimeError as ex:
            kayit.upsert_signal(skey, durum=f"emir hatası: {str(ex)[:80]}", **ctx)
            notes.append(f"⚠️ {t} orta vade alış emri verilemedi: {ex}")
            continue
        vs["pending"][t] = {"since": day, "sessions": 0, "limit": lim, "key": skey, "qty": qty, "part": 1,
                            "bottom": o["bottom"], "budget": budget}
        slots -= 1
        room -= qty * lim
        kayit.upsert_signal(skey, durum="emir verildi", adet=qty, stop=stop, kar_al=tp, **ctx)
        kayit.add_event(sembol=t, islem="EMİR", adet=qty, fiyat=lim, neden=why_, strateji=STRAT, emir_id=cid,
                        anahtar=f"emir-{cid}")
        nxt = " ve ".join(f"{g}." for g in C.TB_TRANCHES[1:])
        placed.append(f"• <b>{t}</b> 1/{n_parts()}. parça · {qty} adet @ ${lim:,.2f} (≈${qty*lim:,.0f})\n"
                      f"   Stop ${stop:,.2f} (dibin %{C.TB_STOP_BELOW*100:g} altı) · Kâr al ${tp:,.2f} (+%{C.TB_TP*100:g})\n"
                      + (f"   Sonraki parçalar: {nxt} seansta, hâlâ dipte/yataydaysa\n" if nxt else "")
                      + f"   Neden: {why_}")
        notes.append(f"📥 {t} orta vade 1. parça emri girildi @ ${lim:,.2f}")
    if placed:
        alerts.append(f"📈 <b>Orta vade – emir giriyorum</b> ({tag}) – {day} kapanışına göre:\n" + "\n".join(placed))
    if missed:
        alerts.append(f"💸 <b>Orta vade – nakit yetmedi, para olsaydı alırdım</b> ({tag}) – {day} kapanışına göre:\n"
                      + "\n".join(missed))
    if no_slot:
        held_txt = ", ".join(sorted(set(held) | set(vs["pending"]))) or "-"
        alerts.append(f"📦 <b>Orta vade – pozisyon sınırı dolu ({C.OV_MAX_POSITIONS}), yer olsaydı alırdım</b> ({tag}) – "
                      f"{day} kapanışına göre (elde/emirde: {held_txt}):\n" + "\n".join(no_slot))
    return _done()
