"""Piyasa öncesi bildirimi: açılıştan önce elde tutulan ve alış emri bekleyen hisselerin seans dışı fiyatı.

NY 09:00'dan sonraki ilk açılış öncesi çalışmada bir kez Telegram'a gider (TR ~17:00, kışın). Fiyat: Yahoo
5 dk, kapanış sonrası + piyasa öncesi dahil (prepost). Dünkü kapanışa göre değişim; işaretler:
  ⚠️  sert düşüş (PM_ALERT ve üstü) – haber olabilir
  🎯  bekleyen alış limitinin altında: açılışta dolması muhtemel
  🛑  stopun altında: açılışta stop çalışacak
  🚀  kâr alın üstünde: açılışta kâr al çalışacak
Emir değiştirmez; yalnız bildirir.
"""
import pandas as pd

from . import config as C

ALERT = getattr(C, "PM_ALERT", 0.03)


def prices(symbols):
    """{sembol: (dünkü kapanış, seans dışı son fiyat, saat)}."""
    from .data import _yf
    if not symbols:
        return {}
    raw = _yf().download(symbols, period="5d", interval="5m", prepost=True, group_by="ticker",
                         auto_adjust=False, threads=True, progress=False)
    out = {}
    for s in symbols:
        try:
            df = raw[s] if isinstance(raw.columns, pd.MultiIndex) else raw
            df = df.dropna(subset=["Close"])
            idx = df.index.tz_convert("America/New_York") if df.index.tz else df.index.tz_localize("America/New_York")
            m = idx.hour * 60 + idx.minute
            reg = df[(m >= 570) & (m < 960)]
            today = idx[-1].normalize()
            prev = reg[idx[(m >= 570) & (m < 960)].normalize() < today]
            if not len(prev):
                continue
            pc = float(prev["Close"].iloc[-1])
            px = float(df["Close"].iloc[-1])
            out[s] = (pc, px, idx[-1].strftime("%H:%M"))
        except Exception as e:  # noqa: BLE001
            print("seans dışı fiyat alınamadı", s, e)
    return out


def message(positions, orders, px):
    """positions: Alpaca pozisyonları, orders: açık emirler (bacaklar dahil), px: prices() çıktısı."""
    lines = []
    held = {p["symbol"]: p for p in positions if p.get("asset_class", "us_equity") != "crypto"}
    buys = {o["symbol"]: o for o in orders if o.get("side") == "buy" and o.get("limit_price")}
    lvl = lambda s, t, k: next((float(o[k]) for o in orders if o["symbol"] == s and o.get("side") == "sell"
                                and o.get("type") == t and o.get(k)), None)
    for s in sorted(set(held) | set(buys)):
        if s not in px:
            continue
        pc, p, hm = px[s]
        ch = p / pc - 1
        flag, note = "", ""
        if s in held:
            st, tp = lvl(s, "stop", "stop_price"), lvl(s, "limit", "limit_price")
            e = float(held[s]["avg_entry_price"])
            note = f"elde · alış ${e:,.2f} ({p / e - 1:+.1%})"
            if st and p <= st:
                flag, note = "🛑", note + f" · stop ${st:,.2f} altında, açılışta satılabilir"
            elif tp and p >= tp:
                flag, note = "🚀", note + f" · kâr al ${tp:,.2f} üstünde"
        else:
            lim = float(buys[s]["limit_price"])
            note = f"alış emri ${lim:,.2f}"
            if p <= lim:
                flag, note = "🎯", note + " · fiyat limitin altında, açılışta dolabilir"
        if ch <= -ALERT and not flag:
            flag = "⚠️"
        elif ch <= -ALERT:
            flag += "⚠️"
        lines.append(f"{flag or '•'} <b>{s}</b> ${p:,.2f} ({ch:+.1%}, {hm}) · {note}")
    if not lines:
        return None
    hot = sum(1 for x in lines if not x.startswith("•"))
    head = f"🌅 <b>Piyasa öncesi</b> – dünkü kapanışa göre" + (f" · {hot} dikkat" if hot else "")
    return head + "\n" + "\n".join(lines) + f"\n⚠️ = −%{ALERT*100:g} ve üstü düşüş (haber olabilir)"


def run(state, now_ny, api=None):
    """Açılış öncesi modda çağrılır; günde bir kez, NY 09:00'dan sonra mesaj metni döner (yoksa None)."""
    if now_ny.hour * 60 + now_ny.minute < 540:
        return None
    key = f"pm|{now_ny.date()}"
    if key in state["sent"]:
        return None
    if api is None:
        import os
        if os.getenv("TRADING_ENABLED", "").lower() != "true":
            return None
        from .trader import Alpaca
        api = Alpaca()
    positions, orders = api.positions(), api.open_orders()
    syms = sorted({p["symbol"] for p in positions if p.get("asset_class", "us_equity") != "crypto"}
                  | {o["symbol"] for o in orders if o.get("side") == "buy" and o.get("asset_class") != "crypto"})
    state["sent"][key] = str(now_ny.date())
    return message(positions, orders, prices(syms)) if syms else None
