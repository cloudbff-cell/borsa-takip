"""Orta vade (evre 2) alım stratejisi – 10 yıllık geri test (işlem yapmaz).

Kurallar engine/ortavade_kural.py'dedir (canlı botla aynı kod). Sinyal gün kapanışında; ertesi seans
kapanışın en çok %1 üstüne limitli alış (açılış limitin altındaysa açılıştan dolar, fiyat limitin hep
üstündeyse alınmaz). Çıkış kuralları kapanışta kontrol edilir, ertesi açılışta satılır; -%8 stop gün içinde.
Portföy her gün piyasa değeriyle izlenir (en büyük düşüş gerçekçi), eşit pay, kaldıraç yok,
kayma giriş ve çıkışta %0.05. Aynı gün birden çok sinyal varsa son 3 ayda en güçlü olan önce.
Uyarı: evren bugünün büyük şirketleri (hayatta kalma yanlılığı) – trend takipçisi stratejilerde bu etki
belirgindir; bu yüzden 'tüm uygun adaylar' evreni ve alt dönemler ayrıca raporlanır.
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
from engine.ortavade_kural import entries, features  # noqa: E402
from engine.run import pick_universe  # noqa: E402

SLIP = 0.0005
MAXD = 252
WARM = 260
OUT = []
PERIODS = [("2017-2019", "2017-01-01", "2019-12-31"), ("2020-2022", "2020-01-01", "2022-12-31"),
           ("2023-2026", "2023-01-01", "2026-12-31")]


def log(s=""):
    print(s, flush=True)
    OUT.append(s)


def trades_for(sym, d, sig, exit_kind, stop_pct=0.08, lim_above=0.01, tp=None, buf=0.0, trail_atr=3.0):
    o, h, l, c = (d[k].to_numpy() for k in ("Open", "High", "Low", "Close"))
    s50, s150, a, r63 = (d[k].to_numpy() for k in ("sma50", "sma150", "atr", "ret63"))
    idx = d.index
    n = len(c)
    out, busy = [], -1
    for i in np.flatnonzero(sig):
        if i < WARM or i <= busy or i + 1 >= n:
            continue
        j = i + 1
        lim = c[i] * (1 + lim_above)
        if o[j] <= lim:
            e = o[j]
        elif l[j] <= lim:
            e = lim
        else:
            continue
        e *= 1 + SLIP
        stop = e * (1 - stop_pct)
        tgt = e * (1 + tp) if tp else np.inf
        hic = e
        k, px, why = None, None, None
        if l[j] <= stop:                       # aynı gün stop (temkinli)
            k, px, why = j, stop, "stop"
        else:
            for t in range(j, min(j + MAXD, n)):
                if t > j:
                    if o[t] <= stop:
                        k, px, why = t, o[t], "stop"
                        break
                    if o[t] >= tgt:
                        k, px, why = t, o[t], "hedef"
                        break
                    if l[t] <= stop:
                        k, px, why = t, stop, "stop"
                        break
                    if h[t] >= tgt:
                        k, px, why = t, tgt, "hedef"
                        break
                hic = max(hic, c[t])
                brk = False
                if exit_kind == "sma50":
                    brk = c[t] < s50[t] * (1 - buf)
                elif exit_kind == "sma150":
                    brk = c[t] < s150[t] * (1 - buf)
                elif exit_kind == "atr":
                    stop = max(stop, hic - trail_atr * a[t])
                if brk:
                    if t + 1 < n:
                        k, px, why = t + 1, o[t + 1], "kural"
                    else:
                        k, px, why = t, c[t], "kural"
                    break
            if k is None:
                k = min(j + MAXD, n) - 1
                px, why = c[k], ("süre" if k < n - 1 else "açık")
        out.append({"s": sym, "in": idx[j], "out": idx[k], "e": e, "px": px * (1 - SLIP),
                    "ret": px * (1 - SLIP) / e - 1, "score": r63[i] if not np.isnan(r63[i]) else -9,
                    "why": why, "days": k - j})
        busy = k
    return out


def simulate(trades, closes, dates, slots):
    """Günlük piyasa değerli portföy. closes: DataFrame (tarih x sembol)."""
    by_in, by_out = {}, {}
    for t in trades:
        by_in.setdefault(t["in"], []).append(t)
    cash, held, eq, taken, prev = 1.0, {}, [], [], 1.0
    expo = []
    for d in dates:
        for s in [s for s, h in held.items() if h["t"]["out"] == d]:
            h = held.pop(s)
            cash += h["q"] * h["t"]["px"]
        for t in sorted(by_in.get(d, []), key=lambda t: -t["score"]):
            if len(held) >= slots or t["s"] in held:
                continue
            amt = min(prev / slots, cash)
            if amt <= 1e-9:
                continue
            q = amt / t["e"]
            cash -= amt
            if t["out"] == d:                 # aynı gün çıkış
                cash += q * t["px"]
                taken.append(t)
                continue
            held[t["s"]] = {"q": q, "t": t}
            taken.append(t)
        row = closes.loc[d]
        val = cash + sum(h["q"] * (row[s] if not np.isnan(row[s]) else h["t"]["e"]) for s, h in held.items())
        expo.append(1 - cash / val if val else 0)
        eq.append(val)
        prev = val
    return taken, pd.Series(eq, index=dates), float(np.mean(expo))


def cagr(eq, a=None, b=None):
    s = eq
    if a:
        base = eq[eq.index < pd.Timestamp(a)]
        s = eq[(eq.index >= pd.Timestamp(a)) & (eq.index <= pd.Timestamp(b))]
        if len(s) < 20:
            return None
        v0 = base.iloc[-1] if len(base) else s.iloc[0]
    else:
        v0 = s.iloc[0]
    yrs = (s.index[-1] - (pd.Timestamp(a) if a else s.index[0])).days / 365.25
    return ((s.iloc[-1] / v0) ** (1 / max(yrs, 0.25)) - 1) * 100


def mdd(eq):
    return ((eq / eq.cummax()) - 1).min() * 100


HDR = ("| Kural | İşlem | Kazanma % | Ort. % | Ort. kazanç / kayıp % | Ort. gün | Yatırımda % | Yıllık % | Maks. düşüş % | "
       + " | ".join(p[0] for p in PERIODS) + " |")


def report(name, taken, eq, expo):
    if not taken:
        return f"| {name} | 0 |"
    r = np.array([t["ret"] for t in taken]) * 100
    w, lo_ = r[r > 0], r[r <= 0]
    sub = [cagr(eq, a, b) for _, a, b in PERIODS]
    return (f"| {name} | {len(taken)} | {(r > 0).mean()*100:.0f} | {r.mean():+.1f} | "
            f"{(w.mean() if len(w) else 0):+.1f} / {(lo_.mean() if len(lo_) else 0):+.1f} | "
            f"{np.mean([t['days'] for t in taken]):.0f} | {expo*100:.0f} | **{cagr(eq):+.1f}** | {mdd(eq):.1f} | "
            + " | ".join("–" if s is None else f"{s:+.1f}" for s in sub) + " |")


def main():
    cands = C.CANDIDATES + (C.ADRS if C.INCLUDE_ADRS else [])
    prices = D.download_prices(cands + [C.BENCHMARK], period="10y")
    fund = D.fundamentals([t for t in cands if t in prices])
    sh = sharia.screen(fund, {t: df.tail(756) for t, df in prices.items()})
    top, _, _ = pick_universe(fund, set(prices), sh)
    allok = [t for t in fund if t in prices and (sh.get(t) or {}).get("ok")]
    spy = prices[C.BENCHMARK]
    spy_ok = (spy["Close"] > spy["Close"].rolling(200).mean())
    feats = {t: features(prices[t]) for t in set(top) | set(allok) if len(prices[t]) > 400}
    dates = spy.index[WARM:]
    closes = pd.DataFrame({t: f["Close"] for t, f in feats.items()}).reindex(spy.index).ffill()
    spy_c = spy["Close"].loc[dates]
    spy_eq = spy_c / spy_c.iloc[0]
    log(f"# Orta vade (evre 2) geri testi · {dates[0]:%Y-%m} – {dates[-1]:%Y-%m}")
    log(f"Evren: İslami filtreden geçen ilk {len(top)} (bugünkü liste) · 'tüm uygun' evreni {len(allok)} hisse")
    log(f"SPY al-tut: yıllık **{cagr(spy_eq):+.1f}%**, maks. düşüş {mdd(spy_eq):.1f}% · "
        + " · ".join(f"{p[0]} {cagr(spy_eq, p[1], p[2]):+.1f}%" for p in PERIODS))
    log("")

    def run(univ, kind, exit_kind, mkt=False, slots=5, tp=None, stop=0.08, **kw):
        tr = []
        for t in univ:
            if t not in feats:
                continue
            d = feats[t]
            sig = entries(d, kind, **kw)
            if mkt:
                sig = sig & spy_ok.reindex(d.index).fillna(False)
            tr += trades_for(t, d, sig.to_numpy(), exit_kind, stop_pct=stop, tp=tp)
        return simulate(tr, closes, dates, slots)

    exits = [("50 g ort. altı kapanış", "sma50", None), ("150 g ort. altı (evre 4)", "sma150", None),
             ("iz süren 3 ATR", "atr", None), ("+%25 hedef", "none", 0.25)]
    kinds = [("3 aylık taban kırılımı", "kirilim"), ("50 g ort.'ya geri çekilme", "geri"),
             ("kırılım + geri çekilme", "ikisi"), ("evre 2'ye ilk geçiş", "evre2")]
    best = []
    for mkt in (False, True):
        log(f"## İlk 50 · 5 pozisyon · -%8 stop · piyasa filtresi (SPY > 200 g ort.): {'var' if mkt else 'yok'}")
        log(HDR)
        log("|" + "---|" * (9 + len(PERIODS)))
        for kn, k in kinds:
            for en, e, tp in exits:
                taken, eq, ex = run(top, k, e, mkt, tp=tp)
                log(report(f"{kn} · {en}", taken, eq, ex))
                if taken:
                    best.append((cagr(eq) / max(-mdd(eq), 1), cagr(eq), kn, k, en, e, tp, mkt))
        log("")

    best.sort(reverse=True)
    log("## En iyi 5 (yıllık getiri / maks. düşüş oranına göre)")
    for b in best[:5]:
        log(f"- {b[2]} · {b[4]} · piyasa filtresi {'var' if b[7] else 'yok'}: yıllık {b[1]:+.1f}%, oran {b[0]:.2f}")
    log("")
    _, _, kn, k, en, e, tp, mkt = best[0]

    log(f"## Sağlamlık – seçilen: {kn} · {en} · piyasa filtresi {'var' if mkt else 'yok'}")
    log(HDR)
    log("|" + "---|" * (9 + len(PERIODS)))
    for lbl, kw in [("ilk 50 (seçilen)", {}), ("tüm uygun adaylar", {"univ": allok}),
                    ("stop -%6", {"stop": 0.06}), ("stop -%10", {"stop": 0.10}),
                    ("taban ≤ %20", {"base_max": 0.20}), ("taban ≤ %35", {"base_max": 0.35}),
                    ("hacim ≥ 1.0x", {"vol_mult": 1.0}), ("hacim ≥ 1.5x", {"vol_mult": 1.5}),
                    ("150 g uzaklık ≤ %25", {"max_ext": 0.25}), ("150 g uzaklık sınırsız", {"max_ext": 9.0})]:
        univ = kw.pop("univ", top)
        stop = kw.pop("stop", 0.08)
        taken, eq, ex = run(univ, k, e, mkt, tp=tp, stop=stop, **kw)
        log(report(lbl, taken, eq, ex))
    log("")
    log("## Pozisyon sayısı (seçilen kural, ilk 50)")
    log(HDR)
    log("|" + "---|" * (9 + len(PERIODS)))
    for n in (3, 4, 5, 8, 10):
        taken, eq, ex = run(top, k, e, mkt, slots=n, tp=tp)
        log(report(f"{n} pozisyon", taken, eq, ex))
    taken, eq, ex = run(top, k, e, mkt, tp=tp)
    log("")
    log("Çıkış nedenleri: " + ", ".join(f"{w} %{v*100:.0f}" for w, v in
                                        pd.Series([t["why"] for t in taken]).value_counts(normalize=True).items()))
    r = pd.Series([t["ret"] * 100 for t in taken])
    log(f"Getiri dağılımı: en iyi %10 işlem ort. {r[r >= r.quantile(0.9)].mean():+.1f}%, "
        f"medyan {r.median():+.1f}%, en kötü {r.min():+.1f}%")
    yr = eq.resample("YE").last().pct_change().dropna() * 100
    sy = spy_eq.resample("YE").last().pct_change().dropna() * 100
    log("Yıllara göre (strateji / SPY): " + " · ".join(f"{d_.year}: {v:+.0f}/{sy.get(d_, np.nan):+.0f}" for d_, v in yr.items()))
    if os.getenv("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write("\n".join(OUT) + "\n")


if __name__ == "__main__":
    main()
