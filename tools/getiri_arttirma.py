"""Ana stratejinin (3 gün düşüş + %2 aşağıdan limit) getirisini artırma seçenekleri (işlem yapmaz).

A) Boşta bekleyen nakdi İslami ETF'te tutmak (SPUS; 2019 öncesi için SPY vekil). Sinyal gelince ETF'ten
   satılıp hisse alınır. Günlük: E *= 1 + (1 - yatırımdaki pay) x ETF getirisi; işlem çıkışında
   E *= 1 + pozisyon payı x işlem getirisi.
B) Evren: ilk 50 yerine İslami filtreden geçen tüm adaylar (daha çok sinyal).
C) Giriş/çıkış seviyeleri: limit %2/%2.5/%3 altı, stop/hedef %2/%2, %3/%3, %2/%3, %1.5/%1.5.
Her biri 3 alt dönemde ayrı raporlanır (sağlamlık); en iyi görüneni seçmek aşırı uyum riski taşır.
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
from engine.run import pick_universe  # noqa: E402
from tools import down3_analiz as A  # noqa: E402
from tools.giris_arastirma import PERIODS, T_CLOSE, prep  # noqa: E402

OUT = []


def log(s=""):
    print(s)
    OUT.append(s)


def trades_for(data, limit_pct=0.02, sp=0.02, tp=0.02):
    out = []
    for t, d in data.items():
        sig, idx, busy = A.signals(d, "dn3"), d.index, -1
        c, o, l = d["Close"].to_numpy(), d["Open"].to_numpy(), d["Low"].to_numpy()
        for i in np.flatnonzero(sig):
            if i < 210 or i <= busy or i + 1 >= len(d):
                continue
            lvl = c[i] * (1 - limit_pct)
            en = None
            for j in (i + 1, i + 2):
                if j < len(d) and l[j] <= lvl:
                    en = (j, min(o[j], lvl), "limit_open" if o[j] <= lvl else "limit")
                    break
            if not en:
                continue
            j0, e, same = en
            j, px, why, _ = A.run_trade(d, j0, e, same, sp, tp, False)
            out.append({"in": idx[j0], "out": idx[j], "ret": px * (1 - A.SLIP) / (e * (1 + A.SLIP)) - 1,
                        "score": -(c[i] / c[i - 3] - 1), "s": t})
            busy = j
    return out


def simulate(trades, etf_ret, slots=3, park=True):
    """Günlük adımlı portföy: en fazla 'slots' pozisyon, her biri giriş anındaki sermayenin 1/slots'u."""
    days = etf_ret.index
    by_in = {}
    for t in sorted(trades, key=lambda t: -t["score"]):
        by_in.setdefault(t["in"], []).append(t)
    E, open_, curve, invested = 1.0, [], [], []
    for d in days:
        if park:
            frac = sum(p["frac"] for p in open_)
            E *= 1 + max(0.0, 1 - frac) * etf_ret.loc[d]
        for p in [p for p in open_ if p["out"] == d]:
            E *= 1 + p["frac"] * p["ret"]
            open_.remove(p)
        for t in by_in.get(d, []):
            if len(open_) < slots and not any(p["s"] == t["s"] for p in open_):
                if t["out"] == d:                    # aynı gün açılıp kapanan
                    E *= 1 + t["ret"] / slots
                else:
                    open_.append(dict(t, frac=1 / slots))
        invested.append(sum(p["frac"] for p in open_))
        curve.append(E)
    eq = pd.Series(curve, index=days)
    return eq, float(np.mean(invested))


def line(name, eq, inv, spy_sub):
    yrs = (eq.index[-1] - eq.index[0]).days / 365.25
    tot = (eq.iloc[-1] ** (1 / yrs) - 1) * 100
    dd = ((eq / eq.cummax()) - 1).min() * 100
    sub = []
    for _, a, b in PERIODS:
        s = eq[(eq.index >= pd.Timestamp(a)) & (eq.index <= pd.Timestamp(b))]
        y = (s.index[-1] - s.index[0]).days / 365.25
        sub.append((s.iloc[-1] / s.iloc[0]) ** (1 / y) * 100 - 100)
    return (f"| {name} | **{tot:+.1f}** | {dd:.1f} | {inv*100:.0f} | " + " | ".join(f"{x:+.1f}" for x in sub) + " |")


def main():
    cands = C.CANDIDATES + (C.ADRS if C.INCLUDE_ADRS else [])
    prices = D.download_prices(cands + [C.BENCHMARK, "SPUS"], period="10y")
    fund = D.fundamentals([t for t in cands if t in prices])
    sh = sharia.screen(fund, {t: df.tail(756) for t, df in prices.items()})
    top, _, _ = pick_universe(fund, set(prices), sh)
    allok = [t for t in cands if t in prices and sh.get(t, {}).get("ok")]
    spy = prices[C.BENCHMARK]["Close"]
    start = spy.index[210]
    spy_r = spy.pct_change().loc[start:].fillna(0)
    etf = spy_r.copy()
    if "SPUS" in prices:
        spus_r = prices["SPUS"]["Close"].pct_change().reindex(etf.index)
        etf = spus_r.where(spus_r.notna(), etf)          # SPUS öncesi SPY vekil
        log(f"ETF: SPUS ({prices['SPUS'].index[0]:%Y-%m}'den itibaren; öncesinde SPY vekil)")
    data50 = {t: prep(prices[t]) for t in top if len(prices[t]) > 400}
    data_all = {t: prep(prices[t]) for t in allok if len(prices[t]) > 400}
    spy_eq = (1 + spy_r).cumprod()
    spus_eq = (1 + etf).cumprod()
    log(f"## Getiriyi artırma seçenekleri ({start:%Y-%m} – {spy.index[-1]:%Y-%m}) · ilk 50: {len(data50)} hisse, "
        f"tüm uygun adaylar: {len(data_all)} hisse")
    log("")
    log("| Seçenek | Yıllık getiri % | Maks. düşüş % | Ort. hissede yatırım % | "
        + " | ".join(p[0] for p in PERIODS) + " |")
    log("|" + "---|" * (4 + len(PERIODS)))
    log(line("SPY al-tut", spy_eq, 1, None))
    log(line("İslami ETF (SPUS/SPY) al-tut", spus_eq, 1, None))
    base = trades_for(data50)
    rows = [("Mevcut: ilk 50, 3 pozisyon, nakit boşta", base, 3, False),
            ("A) + boşta nakit İslami ETF'te", base, 3, True),
            ("A) + 5 pozisyon", base, 5, True)]
    t_all = trades_for(data_all)
    rows += [("B) tüm uygun adaylar, 3 pozisyon, nakit boşta", t_all, 3, False),
             ("A+B) tüm adaylar + ETF, 3 pozisyon", t_all, 3, True),
             ("A+B) tüm adaylar + ETF, 5 pozisyon", t_all, 5, True)]
    for name, tr, n, park in rows:
        eq, inv = simulate(tr, etf, n, park)
        log(line(name, eq, inv, None))
    log("")
    log("### C) Seviye varyantları (ilk 50, 3 pozisyon, boşta nakit ETF'te)")
    log("| Limit / stop / hedef | Yıllık getiri % | Maks. düşüş % | Ort. hissede yatırım % | "
        + " | ".join(p[0] for p in PERIODS) + " |")
    log("|" + "---|" * (4 + len(PERIODS)))
    for lp, sp, tp in [(0.02, 0.02, 0.02), (0.025, 0.02, 0.02), (0.03, 0.02, 0.02), (0.02, 0.03, 0.03),
                       (0.02, 0.02, 0.03), (0.02, 0.015, 0.015), (0.02, 0.03, 0.02)]:
        eq, inv = simulate(trades_for(data50, lp, sp, tp), etf, 3, True)
        log(line(f"%{lp*100:g} altı / −%{sp*100:g} / +%{tp*100:g}", eq, inv, None))
    log("")
    log("Not: ETF'ten nakde geçişte alım-satım maliyeti ve vergi/arındırma hesaba katılmadı; "
        "hisse listesi bugünün büyükleri (iyimser yanlılık).")
    if os.getenv("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write("\n".join(OUT) + "\n")


if __name__ == "__main__":
    main()
