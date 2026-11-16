"""Gün aşırı (swing) strateji: sabit yüzde çıkış testi – 10 yıl günlük veri (işlem yapmaz).

Giriş: botun mevcut kuralları (son 3 günde AL sinyali + puan ≥ 65, ya da yükseliş trendi + puan ≥ 75 ve
20 EMA'ya geri çekilme). Evren: İslami filtreden geçen ilk 50.
Çıkış varyantları:
  - Mevcut: ilk stop giriş - 2 ATR, iz süren stop 3 ATR, SAT sinyalinde çık
  - Kullanıcı önerisi: stop -%2, kâr al +%1 (ve %2, %3, %5 hedefli; süre sınırlı varyantlar)
Günlük mumda aynı gün hem stop hem hedef görülürse hangisinin önce olduğu bilinmez: temkinli (stop önce)
ve iyimser (hedef önce) sonuçlar ayrı verilir. Açılış boşluğu stopun altındaysa çıkış açılış fiyatından.
Portföy: en fazla 5 pozisyon, her biri sermayenin %20'si. Kayma: giriş ve çıkışta %0.05.
"""
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from engine import config as C  # noqa: E402
from engine import data as D  # noqa: E402
from engine import sharia  # noqa: E402
from engine.indicators import add_indicators  # noqa: E402
from engine.run import pick_universe  # noqa: E402
from engine.signals import compute_signals  # noqa: E402
from tools.entry_test import season_bonus, volume_score  # noqa: E402
from tools.olcum import score_series  # noqa: E402

SLIP = 0.0005
K = 20.0
EXIT = ["S_BREAKDOWN", "S_TREND", "S_DEATH"]
VARIANTS = {
    "Mevcut (2 ATR stop, 3 ATR iz süren, SAT çıkışı)": dict(mode="atr"),
    "Stop -%2 / kâr al +%1 (öneri)": dict(sl=0.02, tp=0.01),
    "Stop -%2 / kâr al +%1, en fazla 10 gün": dict(sl=0.02, tp=0.01, max_days=10),
    "Stop -%2 / kâr al +%2": dict(sl=0.02, tp=0.02),
    "Stop -%2 / kâr al +%3": dict(sl=0.02, tp=0.03),
    "Stop -%2 / kâr al +%5": dict(sl=0.02, tp=0.05),
    "Stop -%3 / kâr al +%6": dict(sl=0.03, tp=0.06),
}
OUT = []


def log(s=""):
    print(s)
    OUT.append(s)


def entries(df, sig, total, up):
    """Botun giriş kuralları: (giriş günü indeksi, giriş fiyatı, puan) listesi – üst üste binmeyi sim çözer."""
    o, l, a, e20 = (df[k].values for k in ("Open", "Low", "atr", "ema20"))
    buy = sig[[k for k in sig.columns if k.startswith("B_")]].any(axis=1).values
    rec_buy = pd.Series(buy).rolling(3, min_periods=1).max().astype(bool).values
    sell = sig[EXIT].any(axis=1).values
    rec_sell = pd.Series(sell).rolling(3, min_periods=1).max().astype(bool).values
    rsi, T, upv = df["rsi"].values, total.values, up.values
    out = {}
    for i in range(C.EMA_SLOW + 60, len(df)):
        p = i - 1
        if rec_sell[p] or rsi[p] >= 75 or np.isnan(a[p]):
            continue
        zone = e20[p] + 0.5 * a[p]
        if rec_buy[p] and T[p] >= C.ENTRY_SIGNAL_MIN_SCORE:
            out[i] = (o[i], T[p])
        elif upv[p] and T[p] >= C.ENTRY_TREND_MIN_SCORE and l[i] <= zone:
            out[i] = (min(o[i], zone), T[p])
    return out, sell


AMBIG = set()          # (sembol, tarih) – aynı gün hem stop hem hedef
INTRA = {}             # (sembol, tarih) -> 5 dk mumlar (günlük mumla aynı ölçeğe getirilmiş)


def resolve(sym, d, entry, stop, tp, from_open):
    """5 dk veriyle hangisinin önce görüldüğünü bul: 'hedef' / 'stop' / None (bilinmiyor)."""
    b = INTRA.get((sym, d))
    if b is None:
        return None
    started = from_open
    for _, r in b.iterrows():
        if not started:                       # limitli giriş: fiyat giriş seviyesine inince başla
            if r["l"] <= entry:
                started = True
                if r["l"] <= stop and r["h"] >= tp:
                    return None
                if r["l"] <= stop:
                    return "stop"
            continue
        hs, ht = r["l"] <= stop, r["h"] >= tp
        if hs and ht:
            return None
        if hs:
            return "stop"
        if ht:
            return "hedef"
    return None


def simulate(df, ent, sell, v, optimistic=False, sym=None):
    o, h, l, c, a = (df[k].values for k in ("Open", "High", "Low", "Close", "atr"))
    idx, n, trades, i = df.index, len(df), [], 0
    keys = sorted(ent)
    k = 0
    while k < len(keys):
        i = keys[k]
        entry, score = ent[i]
        entry *= 1 + SLIP
        exit_px, why, j = None, "süre", i
        if v.get("mode") == "atr":
            stop, hi = entry - 2 * a[i - 1], entry
            for j in range(i, min(i + 250, n)):
                if l[j] <= stop:
                    exit_px, why = (min(o[j], stop) if j > i else stop), "stop"
                    break
                hi = max(hi, h[j])
                stop = max(stop, hi - C.TRAIL_ATR * a[j])
                if sell[j]:
                    exit_px, why = c[j], "SAT"
                    break
        else:
            stop, tp = entry * (1 - v["sl"]), entry * (1 + v["tp"])
            last = min(i + v.get("max_days", 250), n - 1)
            for j in range(i, last + 1):
                gap = j > i
                if gap and o[j] <= stop:
                    exit_px, why = o[j], "stop (boşluk)"
                    break
                if gap and o[j] >= tp:
                    exit_px, why = o[j], "hedef (boşluk)"
                    break
                hit_s, hit_t = l[j] <= stop, h[j] >= tp
                if hit_s and hit_t:
                    key = (sym, idx[j].date())
                    AMBIG.add(key)
                    res = resolve(sym, key[1], entry, stop, tp, j > i or o[i] == ent[i][0])
                    if res == "hedef" or (res is None and optimistic):
                        exit_px, why = tp, ("hedef (5dk)" if res else "hedef")
                    else:
                        exit_px, why = stop, ("stop (5dk)" if res else "stop")
                    break
                if hit_s:
                    exit_px, why = stop, "stop"
                    break
                if hit_t:
                    exit_px, why = tp, "hedef"
                    break
        if exit_px is None:
            exit_px = c[j]
        exit_px *= 1 - SLIP
        trades.append({"in": idx[i], "out": idx[j], "ret": exit_px / entry - 1, "why": why, "score": score,
                       "days": j - i + 1})
        while k < len(keys) and keys[k] <= j:
            k += 1
    return trades


def portfolio(trades, slots=5):
    """Kronolojik: aynı anda en fazla 5 pozisyon, her biri giriş anındaki sermayenin %20'si."""
    ev = sorted(trades, key=lambda t: (t["in"], -t["score"]))
    open_, eq, curve, taken = [], 1.0, [], []
    for t in ev:
        for o in sorted([o for o in open_ if o["out"] < t["in"]], key=lambda o: o["out"]):
            eq += o["alloc"] * o["ret"]
            curve.append((o["out"], eq))
            open_.remove(o)
        if len(open_) < slots:
            t = dict(t, alloc=eq / slots)
            open_.append(t)
            taken.append(t)
    for o in sorted(open_, key=lambda o: o["out"]):
        eq += o["alloc"] * o["ret"]
        curve.append((o["out"], eq))
    s = pd.Series([e for _, e in curve], index=[d for d, _ in curve])
    return taken, s


def summarize(name, trades, years):
    taken, eq = portfolio(trades)
    r = np.array([t["ret"] for t in taken]) * 100
    if not len(r):
        return f"| {name} | 0 | | | | | | |"
    cagr = (eq.iloc[-1] ** (1 / years) - 1) * 100 if eq.iloc[-1] > 0 else -100
    dd = ((eq / eq.cummax()) - 1).min() * 100
    days = np.mean([t["days"] for t in taken])
    return (f"| {name} | {len(taken)} | {(r > 0).mean()*100:.1f} | {r.mean():+.2f} | {r[r>0].mean() if (r>0).any() else 0:+.2f} / "
            f"{r[r<=0].mean() if (r<=0).any() else 0:+.2f} | {days:.1f} | {cagr:+.1f} | {dd:.1f} |")


def fetch_intraday(data):
    """Belirsiz günler için Alpaca 5 dk verisi (tarih başına tek istek, o günün tüm sembolleri)."""
    from engine import trader
    if not os.getenv("ALPACA_KEY_ID"):
        log("Alpaca anahtarı yok: belirsiz günler 5 dk veriyle çözülmedi")
        return
    api = trader.Alpaca()
    daily = {t: df for t, df, _, _ in data}
    by_date = {}
    for s, d in AMBIG:
        by_date.setdefault(d, []).append(s)
    ok = 0
    for n, (d, syms) in enumerate(sorted(by_date.items())):
        st = pd.Timestamp(d).tz_localize("America/New_York") + pd.Timedelta(hours=9, minutes=30)
        try:
            raw = api.bars(syms, start=st.isoformat(), end=(st + pd.Timedelta(hours=6, minutes=30)).isoformat(),
                           feed="sip", adjustment="all")
        except RuntimeError as e:
            print(d, e)
            continue
        for s, rows in raw.items():
            b = pd.DataFrame(rows)
            if b.empty:
                continue
            b = b[["o", "h", "l", "c"]].astype(float)
            dc = daily[s].loc[str(d), "Close"]
            dc = float(dc.iloc[0] if hasattr(dc, "iloc") else dc)
            INTRA[(s, d)] = b * (dc / b["c"].iloc[-1])     # günlük (düzeltilmiş) fiyat ölçeğine getir
            ok += 1
    log(f"Aynı gün hem stop hem hedef görülen {len(AMBIG)} gün; {ok} tanesi 5 dk veriyle incelendi")


def main():
    cands = C.CANDIDATES + (C.ADRS if C.INCLUDE_ADRS else [])
    prices = D.download_prices(cands + [C.BENCHMARK], period="10y")
    fund = D.fundamentals([t for t in cands if t in prices])
    sh = sharia.screen(fund, {t: df.tail(756) for t, df in prices.items()})
    top, _, _ = pick_universe(fund, set(prices), sh)
    spy = prices[C.BENCHMARK]["Close"]
    data = []
    for t in top:
        if t not in prices or len(prices[t]) < 400:
            continue
        df = add_indicators(prices[t])
        tr_score, up = score_series(df, spy)
        total = tr_score.fillna(0) + volume_score(df) + K + season_bonus(df, spy)
        sig = compute_signals(df)
        ent, sell = entries(df, sig, total, up.fillna(False))
        data.append((t, df, ent, sell))
    start = spy.index[C.EMA_SLOW + 60]
    years = (spy.index[-1] - start).days / 365.25
    spy_cagr = ((spy.iloc[-1] / spy.loc[start:].iloc[0]) ** (1 / years) - 1) * 100
    log(f"## Gün aşırı strateji – sabit yüzde çıkış testi ({start:%Y-%m} – {spy.index[-1]:%Y-%m}, {years:.1f} yıl)")
    log(f"Evren: İslami filtreden geçen ilk {len(data)} hisse · giriş: botun mevcut kuralları · portföy 5 x %20 · "
        f"kayma %0.05 x 2 · SPY al-tut yıllık {spy_cagr:+.1f}%")
    # 1. geçiş: belirsiz günleri topla, 5 dk veriyi çek
    for name, v in VARIANTS.items():
        if v.get("mode") != "atr":
            for t, df, ent, sell in data:
                simulate(df, ent, sell, v, False, t)
    fetch_intraday(data)
    for opt in (False, True):
        log("")
        log("### " + ("İyimser: 5 dk veriyle de çözülemeyen günlerde HEDEF önce" if opt
                      else "Temkinli: 5 dk veriyle de çözülemeyen günlerde STOP önce"))
        log("| Çıkış kuralı | İşlem | Kazanma % | Ort. % | Ort. kazanç / kayıp % | Ort. gün | Yıllık getiri % | Maks. düşüş % |")
        log("|---|---|---|---|---|---|---|---|")
        for name, v in VARIANTS.items():
            if opt and v.get("mode") == "atr":
                continue
            trades = [tr for t, df, ent, sell in data for tr in simulate(df, ent, sell, v, opt, t)]
            log(summarize(name, trades, years))
            if not opt and name.endswith("(öneri)"):
                w = pd.Series([t["why"] for t in trades]).value_counts()
                OUT.append(f"<!-- {dict(w)} -->")
                print("Öneri çıkış türleri:", dict(w))
    if os.getenv("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write("\n".join(OUT) + "\n")


if __name__ == "__main__":
    main()
