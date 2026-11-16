"""Günlük işlem (day trading) – açılış aralığı kırılımı (ORB), yalnız alış, kaldıraçsız.

Kurallar (parametreler config.py'de, DT_ ile başlar):
  Evren   : İslami filtreden geçen, piyasa değerine göre ilk 50 şirket.
  Günlük  : önceki kapanış 50 günlük EMA'nın üstünde ve 20 EMA > 50 EMA (yükseliş trendi),
            açılış boşluğu %4'ten küçük, bugün bilanço yok, fiyat ≥ $10.
  Aralık  : ilk 15 dakikanın (09:30-09:45 NY) en yükseği / en düşüğü.
  Giriş   : 09:45-11:30 arasında tamamlanan 5 dakikalık mum aralığın üstünde ve VWAP'ın üstünde kapanır,
            mum yeşil, açılış aralığı hacmi son 10 günün ortalamasının ≥ 1.5 katı, SPY de kendi VWAP'ının
            üstünde. Fiyat kırılım seviyesinden 0.25 günlük ATR'den fazla uzaklaşmışsa kovalanmaz.
  Emir    : limitli alış (son fiyat +%0.1) + bağlı stop ve kâr al (bracket, yalnız bugün geçerli).
            Stop = max(aralık dibi, giriş - 0.35 x günlük ATR); kâr al = giriş + 2R; +1R'de stop girişe çekilir.
  Çıkış   : stop / kâr al ya da 15:50'de (erken kapanışta kapanış -10 dk) tüm pozisyonlar kapatılır.
  Risk    : sermayenin DAYTRADE_ALLOC kadarı; işlem başına bunun %1'i risk; en fazla 3 açık pozisyon,
            günde 4 giriş; günlük-işlem kaybı %3'ü aşarsa yeni giriş yok. Alımlar yalnız nakitle.
Çalıştırma: python -m engine.daytrade   (gunluk.yml iş akışı seans boyunca çalıştırır)
"""
import argparse
import json
import math
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from . import config as C
from . import data as D
from . import sharia
from . import telegram
from . import trader
from .indicators import add_indicators

NY = ZoneInfo("America/New_York")
STATE_DIR = Path("cache_dt")
BAR = pd.Timedelta(minutes=C.DT_BAR_MIN)
OR_LEN = pd.Timedelta(minutes=C.DT_OR_MIN)


def _hm(s):
    h, m = s.split(":")
    return pd.Timedelta(hours=int(h), minutes=int(m))


def enabled():
    return (os.getenv("TRADING_ENABLED", "").lower() == "true"
            and os.getenv("DAYTRADE_ENABLED", "").lower() == "true")


# ---------------- Saf kural fonksiyonları (canlı ve geri test ortak) ----------------

def to_frame(rows) -> pd.DataFrame:
    """Alpaca bar listesi -> NY saatli DataFrame (index = mum başlangıcı)."""
    if not rows:
        return pd.DataFrame(columns=["o", "h", "l", "c", "v"])
    df = pd.DataFrame(rows)
    df.index = pd.to_datetime(df["t"], utc=True).dt.tz_convert(NY)
    return df[["o", "h", "l", "c", "v"]].astype(float).sort_index()


def daily_context(df: pd.DataFrame, today, fund: dict = None) -> dict:
    """Günlük veriden bugüne ait bağlam (yalnız dünkü kapanışa kadarki veri: ileriye bakma yok)."""
    d = df if "atr" in df.columns else add_indicators(df)    # göstergeler önceden hesaplanmışsa tekrar hesaplama
    d = d[d.index.date < today]
    if len(d) < 60:
        return None
    r = d.iloc[-1]
    ne = (fund or {}).get("nextEarnings")
    return {"prev_close": float(r["Close"]), "atr": float(r["atr"]), "ema20": float(r["ema20"]),
            "ema50": float(r["ema50"]),
            "trend_ok": bool(r["Close"] > r["ema50"] and r["ema20"] > r["ema50"]),
            "earnings_today": bool(ne and str(ne)[:10] == today.isoformat())}


def or_volume_avg(hist: pd.DataFrame, today, n=None) -> float:
    """Son n günün açılış aralığı hacim ortalaması (bugün hariç)."""
    n = n or C.DT_HISTORY_DAYS
    if hist is None or not len(hist):
        return float("nan")
    h = hist[hist.index.date < today]
    if not len(h):
        return float("nan")
    day = h.index.normalize()
    start = day + pd.Timedelta(hours=9, minutes=30)
    m = (h.index >= start) & (h.index < start + OR_LEN)
    vols = h["v"][m].groupby(day[m]).sum()
    vols = vols[vols > 0].tail(n)
    return float(vols.mean()) if len(vols) >= 3 else float("nan")


def vwap(bars: pd.DataFrame) -> pd.Series:
    tp = (bars["h"] + bars["l"] + bars["c"]) / 3
    return (tp * bars["v"]).cumsum() / bars["v"].cumsum().replace(0, np.nan)


def market_ok(spy: pd.DataFrame) -> bool:
    if spy is None or not len(spy):
        return True        # veri yoksa filtre uygulanmaz
    return bool(spy["c"].iloc[-1] > vwap(spy).iloc[-1])


def evaluate(bars: pd.DataFrame, ctx: dict, spy_ok: bool = True):
    """bars: bugünün TAMAMLANMIŞ 5 dakikalık mumları. Giriş koşulları sağlanıyorsa plan, değilse None."""
    if ctx is None or bars is None or len(bars) == 0 or not ctx.get("trend_ok") or ctx.get("earnings_today"):
        return None
    day_open = bars.index[0].normalize() + pd.Timedelta(hours=9, minutes=30)
    or_end = day_open + OR_LEN
    orb = bars[bars.index < or_end]
    if len(orb) < OR_LEN / BAR or bars.index[0] != day_open:
        return None
    last_t = bars.index[-1]
    if last_t < or_end or last_t + BAR > day_open.normalize() + _hm(C.DT_ENTRY_END):
        return None
    last = bars.iloc[-1]
    price, atr = float(last["c"]), ctx["atr"]
    orh, orl = float(orb["h"].max()), float(orb["l"].min())
    gap = float(orb["o"].iloc[0]) / ctx["prev_close"] - 1
    rv = float(orb["v"].sum()) / ctx["or_vol_avg"] if ctx.get("or_vol_avg", 0) > 0 else float("nan")
    vw = float(vwap(bars).iloc[-1])
    ok = (spy_ok and price >= C.DT_MIN_PRICE and abs(gap) <= C.DT_MAX_GAP
          and price > orh and price - orh <= C.DT_MAX_CHASE_ATR * atr
          and price > vw and last["c"] > last["o"]
          and rv >= C.DT_RVOL_MIN and orh - orl >= 0.1 * atr)
    if not ok:
        return None
    risk = max(price - max(orl, price - C.DT_STOP_ATR * atr), 0.002 * price)
    return {"price": price, "orh": orh, "orl": orl, "vwap": vw, "rvol": rv, "gap": gap, "atr": atr,
            "stop": price - risk, "tp": price + C.DT_TARGET_R * risk, "risk": risk, "bar": str(last_t)}


def simulate_day(bars: pd.DataFrame, ctx: dict, spy: pd.DataFrame = None, slip=0.0005):
    """Geri test: tek hisse, tek gün. Sinyal mumundan sonraki mumda limitli emirle giriş;
    aynı mumda hem stop hem hedef görülürse stop varsayılır (temkinli). Sonuç dict ya da None."""
    if not len(bars) or ctx is None or not ctx.get("trend_ok"):
        return None
    flat_t = bars.index[0].normalize() + _hm(C.DT_FLATTEN)
    orh = bars["h"][bars.index < bars.index[0].normalize() + pd.Timedelta(hours=9, minutes=30) + OR_LEN].max()
    cl, op = bars["c"].to_numpy(), bars["o"].to_numpy()
    for i in range(len(bars) - 1):
        if not (cl[i] > orh and cl[i] > op[i]):
            continue                         # hızlı ön eleme (evaluate ile aynı koşullar)
        sp = spy[spy.index <= bars.index[i]] if spy is not None else None
        plan = evaluate(bars.iloc[: i + 1], ctx, market_ok(sp))
        if not plan:
            continue
        limit = plan["price"] * 1.001
        nb = bars.iloc[i + 1]
        if nb["l"] > limit:
            return None                      # emir dolmadı (gerçekte 3 dk sonra iptal)
        entry = min(float(nb["o"]), limit) * (1 + slip)
        risk = entry - plan["stop"]
        if risk <= 0:
            return None
        stop, tp = plan["stop"], plan["tp"]
        for t, b in bars.iloc[i + 1:].iterrows():
            if t >= flat_t:
                ex, why = float(b["o"]), "gün sonu"
                break
            if b["l"] <= stop:
                ex, why = min(float(b["o"]), stop), "stop"
                break
            if b["h"] >= tp:
                ex, why = max(float(b["o"]), tp), "kâr al"
                break
            if b["h"] >= entry + C.DT_BREAKEVEN_R * risk:
                stop = max(stop, entry)
        else:
            ex, why = float(bars["c"].iloc[-1]), "gün sonu"
        ex *= (1 - slip)
        return {"entry": entry, "exit": ex, "r": (ex - entry) / risk, "pct": (ex / entry - 1) * 100,
                "why": why, "time": plan["bar"], "rvol": plan["rvol"]}
    return None


# ---------------- Canlı işlem ----------------

def load_state(today):
    p = STATE_DIR / "state.json"
    st = json.loads(p.read_text()) if p.exists() else {}
    if st.get("date") != today:
        st = {"date": today, "trades": {}, "open": [], "closed": {}, "sent": {}, "entries": 0}
    return st


def save_state(st):
    STATE_DIR.mkdir(exist_ok=True)
    (STATE_DIR / "state.json").write_text(json.dumps(st, default=str))


class DayTrader:
    def __init__(self, api, session_open, session_close, now_fn=None, dry=False):
        self.api, self.open, self.close, self.dry = api, session_open, session_close, dry
        self.now = now_fn or (lambda: datetime.now(NY))
        self.today = session_open.date()
        self.flat_at = min(pd.Timestamp(session_open.normalize() + _hm(C.DT_FLATTEN)),
                           pd.Timestamp(session_close) - pd.Timedelta(minutes=10))
        self.state = load_state(self.today.isoformat())
        self.tag = "🧪 SANAL" if getattr(api, "paper", True) else "💵 GERÇEK"
        self.universe, self.ctx, self.msgs = [], {}, []
        self.last_scan = None
        self.wait = 5

    # -- hazırlık --
    def setup(self, prices=None, fund=None, hist=None):
        """Evren + günlük bağlam + geçmiş açılış aralığı hacimleri."""
        from .run import pick_universe   # döngüsel içe aktarmayı önle
        cands = C.CANDIDATES + (C.ADRS if C.INCLUDE_ADRS else [])
        if prices is None:
            prices = D.download_prices(cands + [C.BENCHMARK], period="3y")
        if fund is None:
            fund = D.fundamentals([t for t in cands if t in prices])
        sh = sharia.screen(fund, prices)
        top, _, _ = pick_universe(fund, set(prices), sh)
        self.universe = top
        if hist is None:
            start = (pd.Timestamp(self.open) - pd.Timedelta(days=C.DT_HISTORY_DAYS + 8)).isoformat()
            raw = self.api.bars(top + [C.BENCHMARK], start=start, end=pd.Timestamp(self.open).isoformat())
            hist = {s: to_frame(r) for s, r in raw.items()}
        for t in top:
            c = daily_context(prices[t], self.today, fund.get(t))
            if c:
                c["or_vol_avg"] = or_volume_avg(hist.get(t), self.today)
                self.ctx[t] = c
        trader.ensure_no_margin(self.api, self.state)
        print(f"Günlük işlem evreni: {len(self.universe)} hisse, trend uygun: "
              f"{sum(1 for c in self.ctx.values() if c['trend_ok'])}")

    # -- hesap --
    def _account(self):
        acct = self.api.account()
        dt_syms = trader.day_symbols(self.api)
        pos_all = [p for p in self.api.positions() if p.get("asset_class", "us_equity") != "crypto"]
        orders = self.api.open_orders()
        self.dt_pos = {p["symbol"]: p for p in pos_all if p["symbol"] in dt_syms}
        self.busy = {p["symbol"] for p in pos_all} | {o["symbol"] for o in orders}   # swing bot dahil
        self.dt_orders = [o for o in orders if o["symbol"] in dt_syms]
        self.cap = float(acct.get("last_equity") or acct["equity"]) * C.DAYTRADE_ALLOC
        self.cash = max(float(acct.get("cash", 0) or 0), 0.0)
        return acct

    def _today_orders(self):
        try:
            return [o for o in self.api.orders("all", after=trader.ny_midnight_iso())
                    if str(o.get("client_order_id", "")).startswith("dt-") or o["symbol"] in self.state["trades"]]
        except RuntimeError as e:
            print(e)
            return []

    def realized(self):
        """Bugünkü günlük işlemlerin gerçekleşen K/Z'si (sembol bazında)."""
        out = {}
        for o in self._today_orders():
            if o["symbol"] not in self.state["trades"]:
                continue
            q, px = float(o.get("filled_qty") or 0), float(o.get("filled_avg_price") or 0)
            if q and px:
                r = out.setdefault(o["symbol"], {"buy": 0.0, "sell": 0.0, "bq": 0.0, "sq": 0.0})
                k = "buy" if o["side"] == "buy" else "sell"
                r[k] += q * px
                r["bq" if k == "buy" else "sq"] += q
        return out

    # -- döngü adımları --
    def manage(self):
        """Dolmayan girişleri iptal et, +1R'de stopu girişe çek, kapanan pozisyonları bildir."""
        now = pd.Timestamp(self.now())
        for o in self.dt_orders:
            if o["side"] == "buy" and str(o.get("client_order_id", "")).startswith("dt-"):
                sub = pd.Timestamp(o.get("submitted_at") or o.get("created_at") or now)
                if now - sub.tz_convert(NY) >= pd.Timedelta(minutes=C.DT_FILL_TIMEOUT_MIN):
                    try:
                        self.api.cancel(o["id"])
                        self.msgs.append(f"{self.tag} ⏹ <b>{o['symbol']}</b> günlük alış emri {C.DT_FILL_TIMEOUT_MIN} dk "
                                         "içinde dolmadı, iptal edildi")
                    except RuntimeError as e:
                        print(e)
        for sym, p in self.dt_pos.items():
            tr = self.state["trades"].get(sym, {})
            entry, price = float(p["avg_entry_price"]), float(p["current_price"])
            legs = [o for o in self.dt_orders if o["symbol"] == sym and o["side"] == "sell"
                    and o.get("type") in ("stop", "stop_limit") and o.get("stop_price")]
            if not legs:
                continue
            cur = float(legs[0]["stop_price"])
            risk = tr.get("risk") or (entry - cur)
            if risk > 0 and cur < entry and price >= entry + C.DT_BREAKEVEN_R * risk and not tr.get("be"):
                try:
                    self.api.replace_stop(legs[0]["id"], round(entry, 2))
                    tr["be"] = True
                    self.msgs.append(f"{self.tag} 🔒 <b>{sym}</b> +{C.DT_BREAKEVEN_R:g}R görüldü, stop girişe çekildi (${entry:,.2f})")
                except RuntimeError as e:
                    print(e)
        prev = set(self.state["open"])
        gone = prev - set(self.dt_pos)
        if gone:
            rz = self.realized()
            for sym in gone:
                r = rz.get(sym)
                if r and r["sq"]:
                    pl = r["sell"] - r["buy"] * (r["sq"] / r["bq"] if r["bq"] else 1)
                    self.state["closed"][sym] = pl
                    self.msgs.append(f"{self.tag} {'✅' if pl >= 0 else '🔻'} <b>{sym}</b> günlük pozisyon kapandı · "
                                     f"K/Z {pl:+,.2f}$ (ort. alış ${r['buy']/r['bq']:,.2f} → satış ${r['sell']/r['sq']:,.2f})")
        self.state["open"] = sorted(self.dt_pos)

    def day_pl(self):
        unreal = sum(float(p.get("unrealized_pl") or 0) for p in self.dt_pos.values())
        return sum(self.state["closed"].values()) + unreal

    def scan(self, today_bars=None):
        """Yeni tamamlanan mumda girişleri değerlendir ve emir ver."""
        if self.state["entries"] >= C.DT_MAX_TRADES:
            return
        if self.day_pl() <= -C.DT_DAILY_LOSS * self.cap:
            k = "losslimit"
            if k not in self.state["sent"]:
                self.state["sent"][k] = 1
                self.msgs.append(f"{self.tag} ⛔ Günlük işlem kaybı {self.day_pl():+,.0f}$ – limit "
                                 f"(%{C.DT_DAILY_LOSS*100:.0f}) aşıldı, bugün yeni giriş yok")
            return
        now = pd.Timestamp(self.now())
        if today_bars is None:
            raw = self.api.bars(self.universe + [C.BENCHMARK], start=pd.Timestamp(self.open).isoformat())
            today_bars = {s: to_frame(r) for s, r in raw.items()}
        done = lambda df: df[df.index + BAR <= now] if len(df) else df
        spy_ok = market_ok(done(today_bars.get(C.BENCHMARK, pd.DataFrame())))
        plans = []
        for t in self.universe:
            if t in self.state["trades"] or t in self.busy:
                continue                         # hisse başına günde tek giriş; swing botun hissesine dokunma
            p = evaluate(done(today_bars.get(t, pd.DataFrame())), self.ctx.get(t), spy_ok)
            if p:
                plans.append((t, p))
        plans.sort(key=lambda x: -x[1]["rvol"])
        notional = sum(abs(float(p.get("market_value") or 0)) for p in self.dt_pos.values())
        notional += sum(float(o.get("qty") or 0) * float(o.get("limit_price") or 0) for o in self.dt_orders
                        if o["side"] == "buy")
        slots = C.DT_MAX_POSITIONS - len(self.dt_pos) - sum(1 for o in self.dt_orders if o["side"] == "buy")
        for t, p in plans:
            if slots <= 0 or self.state["entries"] >= C.DT_MAX_TRADES:
                break
            limit = round(p["price"] * 1.001, 2)
            stop, tp = round(p["stop"], 2), round(p["tp"], 2)
            room = min(self.cap - notional, self.cash) * 0.95      # nakitle sınırlı, kaldıraç yok
            qty = math.floor(min(self.cap * C.DT_RISK / (limit - stop), self.cap / C.DT_MAX_POSITIONS / limit,
                                 room / limit))
            if qty < 1 or stop >= limit:
                continue
            cid = f"dt-{self.today:%Y%m%d}-{t}"
            try:
                if not self.dry:
                    self.api.day_buy(t, qty, limit, stop, tp, cid)
                self.state["trades"][t] = {"qty": qty, "limit": limit, "stop": stop, "tp": tp,
                                           "risk": limit - stop, "time": f"{now:%H:%M}", "rvol": round(p["rvol"], 2)}
                self.state["entries"] += 1
                slots -= 1
                notional += qty * limit
                self.cash -= qty * limit
                self.msgs.append(
                    f"{self.tag} ⚡ <b>{t}</b> GÜNLÜK ALIŞ · {qty} adet @ ${limit:,.2f} (≈${qty*limit:,.0f})\n"
                    f"   Stop ${stop:,.2f} · Kâr al ${tp:,.2f} ({C.DT_TARGET_R:g}R) · risk ≈${qty*(limit-stop):,.0f}\n"
                    f"   Neden: 15 dk açılış aralığı (${p['orh']:,.2f}) yukarı kırıldı, VWAP ${p['vwap']:,.2f} üstünde, "
                    f"aralık hacmi ortalamanın {p['rvol']:.1f} katı, günlük trend yukarı · {C.DT_FLATTEN}'de kapanır")
            except RuntimeError as e:
                self.state["trades"][t] = {"error": str(e)[:120]}
                self.msgs.append(f"⚠️ {t} günlük alış emri verilemedi: {e}")

    def flatten(self, reason="gün sonu"):
        for o in self.dt_orders:
            try:
                self.api.cancel(o["id"])
            except RuntimeError as e:
                print(e)
        for sym, p in self.dt_pos.items():
            try:
                self.api.close_position(sym)
                self.msgs.append(f"{self.tag} 🔔 <b>{sym}</b> günlük pozisyon kapatıldı ({reason}) · "
                                 f"son K/Z {float(p.get('unrealized_pl') or 0):+,.2f}$")
            except RuntimeError as e:
                self.msgs.append(f"⚠️ {sym} kapatılamadı: {e}")

    def report(self):
        if "report" in self.state["sent"]:
            return None
        self.state["sent"]["report"] = 1
        rz = self.realized()
        L = [f"⚡ <b>Günlük işlem özeti</b> – {self.today} ({self.tag})"]
        tot = 0.0
        for sym, tr in self.state["trades"].items():
            r = rz.get(sym)
            if tr.get("error"):
                L.append(f"• {sym}: emir hatası")
            elif not r or not r["bq"]:
                L.append(f"• {sym}: emir dolmadı")
            else:
                pl = r["sell"] - r["buy"] * (r["sq"] / r["bq"]) if r["sq"] else 0.0
                tot += pl
                L.append(f"• <b>{sym}</b> {r['bq']:g} adet · ${r['buy']/r['bq']:,.2f} → "
                         + (f"${r['sell']/r['sq']:,.2f} · {pl:+,.2f}$" if r["sq"] else "açık"))
        if len(L) == 1:
            L.append("Bugün kurallara uyan giriş olmadı.")
        else:
            L.append(f"Toplam: <b>{tot:+,.2f}$</b> (günlük işlem sermayesi ≈${self.cap:,.0f}, "
                     f"{tot/self.cap*100 if self.cap else 0:+.2f}%)")
        return "\n".join(L)

    def flush(self):
        if self.msgs:
            telegram.send("\n\n".join(self.msgs))
            self.msgs = []
        save_state(self.state)

    def step(self):
        """Tek döngü: hesap + yönetim + (yeni mum tamamlandıysa) tarama. True: gün bitti."""
        now = pd.Timestamp(self.now())
        self._account()
        if now >= self.flat_at:
            self.flatten()
            for _ in range(6):                  # piyasa emirlerinin dolmasını bekle
                time.sleep(self.wait)
                self._account()
                if not self.dt_pos:
                    break
            self.manage()
            rep = self.report()
            if rep:
                self.msgs.append(rep)
            return True
        self.manage()
        open_ts = pd.Timestamp(self.open)
        if now >= open_ts + OR_LEN:
            n_bars = int((now - open_ts) / BAR)
            bar_end = open_ts + n_bars * BAR
            if self.last_scan != bar_end and now - bar_end >= pd.Timedelta(seconds=15):
                self.last_scan = bar_end
                if bar_end <= open_ts.normalize() + _hm(C.DT_ENTRY_END):
                    try:
                        self.scan()
                    except RuntimeError as e:
                        print("Tarama hatası:", e)
        return False


def safety_flatten(now_ny, client=None):
    """Tarama iş akışından yedek: kapanışa yakın/sonra açık kalmış günlük pozisyonları kapat."""
    if not (enabled() or client is not None):
        return []
    if now_ny.time() < datetime.strptime("15:52", "%H:%M").time():
        return []
    api = client or trader.Alpaca()
    syms = trader.day_symbols(api)
    msgs = []
    if not syms:
        return msgs
    for o in api.open_orders():
        if o["symbol"] in syms:
            try:
                api.cancel(o["id"])
            except RuntimeError as e:
                print(e)
    for p in api.positions():
        if p["symbol"] in syms:
            try:
                api.close_position(p["symbol"])
                msgs.append(f"⚡ <b>{p['symbol']}</b> günlük pozisyon açık kalmıştı, kapatıldı (yedek kontrol)")
            except RuntimeError as e:
                msgs.append(f"⚠️ {p['symbol']} günlük pozisyonu kapatılamadı: {e}")
    return msgs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-minutes", type=float, default=340, help="GitHub iş süresi sınırı için güvenli çıkış")
    ap.add_argument("--dry", action="store_true", help="emir verme, yalnız yazdır")
    ap.add_argument("--poll", type=float, default=20)
    a = ap.parse_args()
    t0 = time.time()
    if not enabled() and not a.dry:
        print("Günlük işlem kapalı (TRADING_ENABLED ve DAYTRADE_ENABLED 'true' olmalı).")
        return
    api = trader.Alpaca()
    today = datetime.now(NY).date().isoformat()
    cal = api.calendar(today, today)
    if not cal or cal[0].get("date") != today:
        print("Bugün piyasa kapalı.")
        return
    o, c = cal[0]["open"], cal[0]["close"]
    s_open = pd.Timestamp(f"{today} {o}", tz=NY)
    s_close = pd.Timestamp(f"{today} {c}", tz=NY)
    dt = DayTrader(api, s_open, s_close, dry=a.dry)
    if pd.Timestamp(datetime.now(NY)) >= dt.flat_at + pd.Timedelta(minutes=10):
        print("Seans bitti.")
        return
    # Açılışa kadar bekle (hazırlık açılıştan önce yapılır)
    dt.setup()
    while pd.Timestamp(datetime.now(NY)) < s_open and time.time() - t0 < a.max_minutes * 60:
        time.sleep(30)
    print("Döngü başladı:", datetime.now(NY))
    errors = 0
    while time.time() - t0 < a.max_minutes * 60:
        try:
            finished = dt.step()
            errors = 0
        except Exception as e:  # noqa: BLE001 – ağ hataları döngüyü durdurmasın
            errors += 1
            print("Adım hatası:", e)
            if errors == 5:
                dt.msgs.append(f"⚠️ Günlük işlem döngüsünde art arda hata: {str(e)[:200]}")
            finished = False
        dt.flush()
        if finished:
            break
        time.sleep(a.poll)
    dt.flush()
    print("Çıkış:", datetime.now(NY))


if __name__ == "__main__":
    main()
