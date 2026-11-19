"""'Düşüş sonrası yatay taban' alımı – 10 yıllık geri test (işlem yapmaz).

Grafiğin sağ ucu: 1 yıl içinde güçlü yükseliş (dipten tepeye >= %40), tepe, sert düşüş (tepeden >= %25),
ardından fiyat dipte yatay gidiyor (son 40 seans bandı <= %15, son 15 seansta yeni dip yok).
Giriş varyantları: yatay sürerken (bandın alt yarısında) ya da bandın yukarı kırılışında.
Portföy, kayma, emir modeli tools/ortavade_test.py ile aynı.
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
from engine.ortavade_kural import entries_taban, features_taban  # noqa: E402
from engine.run import pick_universe  # noqa: E402
from tools import ortavade_test as O  # noqa: E402

log = O.log


def main():
    cands = C.CANDIDATES + (C.ADRS if C.INCLUDE_ADRS else [])
    prices = D.download_prices(cands + [C.BENCHMARK], period="10y")
    fund = D.fundamentals([t for t in cands if t in prices])
    sh = sharia.screen(fund, {t: df.tail(756) for t, df in prices.items()})
    top, _, _ = pick_universe(fund, set(prices), sh)
    allok = [t for t in fund if t in prices and (sh.get(t) or {}).get("ok")]
    spy = prices[C.BENCHMARK]
    spy_ok = spy["Close"] > spy["Close"].rolling(200).mean()
    feats = {t: features_taban(prices[t]) for t in set(top) | set(allok) if len(prices[t]) > 400}
    dates = spy.index[O.WARM:]
    closes = pd.DataFrame({t: f["Close"] for t, f in feats.items()}).reindex(spy.index).ffill()
    spy_eq = spy["Close"].loc[dates] / spy["Close"].loc[dates].iloc[0]
    log(f"# 'Düşüş sonrası yatay taban' geri testi · {dates[0]:%Y-%m} – {dates[-1]:%Y-%m}")
    log(f"Evren: İslami ilk {len(top)} · tüm uygun {len(allok)} · SPY al-tut yıllık **{O.cagr(spy_eq):+.1f}%**, "
        f"maks. düşüş {O.mdd(spy_eq):.1f}%")
    n_sig = sum(int(entries_taban(f, 'yatay_her').sum() > 0) for t, f in feats.items() if t in top)
    log(f"İlk 50'de 10 yılda en az bir kez bu kalıba giren hisse: {n_sig}")
    log("")

    def run(univ, kind, tp, stop=0.10, maxd=126, slots=5, mkt=False, band_stop=False, **kw):
        tr = []
        for t in univ:
            if t not in feats:
                continue
            d = feats[t]
            sig = entries_taban(d, kind, **kw)
            if mkt:
                sig = sig & spy_ok.reindex(d.index).fillna(False)
            sl = (d["side_lo"] * 0.98).to_numpy() if band_stop else None
            tr += O.trades_for(t, d, sig.to_numpy(), "none", stop_pct=stop, tp=tp, maxd=maxd, stop_lvl=sl)
        return O.simulate(tr, closes, dates, slots)

    rows = []
    for kname, k in [("yatay sürerken (bandın alt yarısı)", "yatay"), ("yatay sürerken (her gün)", "yatay_her"),
                     ("yataydan yukarı çıkış", "kirilim")]:
        log(f"## Giriş: {kname} · 5 pozisyon")
        log(O.HDR)
        log("|" + "---|" * (9 + len(O.PERIODS)))
        for lbl, kw in [("-%10 stop · +%20 hedef · 6 ay", dict(tp=0.20)),
                        ("-%10 stop · +%30 hedef · 6 ay", dict(tp=0.30)),
                        ("-%10 stop · +%30 hedef · 1 yıl", dict(tp=0.30, maxd=252)),
                        ("bant dibi stop · +%20 hedef · 6 ay", dict(tp=0.20, band_stop=True)),
                        ("bant dibi stop · +%30 hedef · 1 yıl", dict(tp=0.30, band_stop=True, maxd=252)),
                        ("-%15 stop · +%30 hedef · 1 yıl", dict(tp=0.30, stop=0.15, maxd=252)),
                        ("-%10 stop · +%30 · 1 yıl · SPY filtresi", dict(tp=0.30, maxd=252, mkt=True))]:
            taken, eq, ex = run(top, k, **kw)
            log(O.report(lbl, taken, eq, ex))
            if taken:
                rows.append((O.cagr(eq) / max(-O.mdd(eq), 1), O.cagr(eq), kname, k, lbl, kw))
        log("")
    rows.sort(key=lambda r: -r[0])
    log("## En iyi 5 (yıllık getiri / maks. düşüş)")
    for r in rows[:5]:
        log(f"- {r[2]} · {r[4]}: yıllık {r[1]:+.1f}%, oran {r[0]:.2f}")
    _, _, kname, k, lbl, kw = rows[0]
    log("")
    log(f"## Sağlamlık – {kname} · {lbl}")
    log(O.HDR)
    log("|" + "---|" * (9 + len(O.PERIODS)))
    for name, extra in [("seçilen (ilk 50)", {}), ("tüm uygun adaylar", {"univ": allok}),
                        ("bant ≤ %10", {"max_range": 0.10}), ("bant ≤ %20", {"max_range": 0.20}),
                        ("düşüş ≥ %20", {"min_drop": 0.20}), ("düşüş ≥ %35", {"min_drop": 0.35}),
                        ("yükseliş ≥ %25", {"min_rise": 0.25}), ("yükseliş ≥ %60", {"min_rise": 0.60}),
                        ("3 pozisyon", {"slots": 3}), ("8 pozisyon", {"slots": 8})]:
        a = dict(kw, **extra)
        univ = a.pop("univ", top)
        taken, eq, ex = run(univ, k, **a)
        log(O.report(name, taken, eq, ex))
    taken, eq, ex = run(top, k, **kw)
    log("")
    log("Çıkış nedenleri: " + ", ".join(f"{w} %{v*100:.0f}" for w, v in
                                        pd.Series([t["why"] for t in taken]).value_counts(normalize=True).items()))
    yr = eq.resample("YE").last().pct_change().dropna() * 100
    sy = spy_eq.resample("YE").last().pct_change().dropna() * 100
    log("Yıllara göre (strateji / SPY): " + " · ".join(f"{d_.year}: {v:+.0f}/{sy.get(d_, np.nan):+.0f}" for d_, v in yr.items()))
    log("Son 10 işlem: " + " · ".join(f"{t['s']} {t['in']:%Y-%m} {t['ret']*100:+.0f}%" for t in taken[-10:]))
    if os.getenv("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write("\n".join(O.OUT) + "\n")


if __name__ == "__main__":
    main()
