"""Orta vade: 'düşüş sonrası dipte yatay' + kademeli alım – 10 yıllık geri test (işlem yapmaz).

Kural (engine/ortavade_kural.py): 1 yıllık grafikte güçlü yükseliş (>= %40) -> tepe -> sert düşüş (>= %25);
3 aylık grafikte dipten sonra en az 3 seans yeni dip yok ve fiyat dibin en çok %10 üstünde.
Alım kademeli: ilk parça sinyalde, sonraki parçalar ilk alıştan N seans sonra (hâlâ dipte/yataydaysa).
Her parça ertesi seans kapanışın en çok %1 üstüne limitle. Stop: dibin %5 altı (dip kırılırsa hepsi satılır).
Kâr al: ortalama maliyetin %30 üstü. En fazla 1 yıl. Evren: İslami ilk 50. Portföy günlük piyasa değeriyle.
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
from engine.ortavade_kural import features_taban, setup_taban  # noqa: E402
from engine.run import pick_universe  # noqa: E402
from tools import ortavade_test as O  # noqa: E402

SLIP = 0.0005
log = O.log


def positions_for(sym, d, setup, tranches=(0, 5, 10), stop_below=0.05, tp=0.30, maxd=252, fixed_stop=None,
                  lim_above=0.01):
    o, h, l, c = (d[k].to_numpy() for k in ("Open", "High", "Low", "Close"))
    bot, r63 = d["bottom"].to_numpy(), d["ret63"].to_numpy()
    idx, n = d.index, len(c)
    out, busy, w = [], -1, 1 / len(tranches)
    for i in np.flatnonzero(setup):
        if i < O.WARM or i <= busy or i + 1 >= n:
            continue
        legs, stop, t0 = [], bot[i] * (1 - stop_below), None
        orders = {i + 1: c[i] * (1 + lim_above)}           # gün -> limit
        k_exit = px = why = None
        t = i + 1
        while t < n:
            filled_today = False
            if t in orders:
                lim = orders.pop(t)
                e = o[t] if o[t] <= lim else (lim if l[t] <= lim else None)
                if e is not None:
                    legs.append({"day": t, "e": e * (1 + SLIP)})
                    filled_today = True
                    if t0 is None:
                        t0 = t
                        if fixed_stop:
                            stop = legs[0]["e"] * (1 - fixed_stop)
            if t0 is None:                                  # ilk parça dolmadı -> vazgeç
                break
            avg = np.mean([x["e"] for x in legs])
            tgt = avg * (1 + tp)
            if not filled_today or t > t0:
                if o[t] <= stop and not filled_today:
                    k_exit, px, why = t, o[t], "stop"
                    break
                if o[t] >= tgt and not filled_today:
                    k_exit, px, why = t, o[t], "hedef"
                    break
            if l[t] <= stop:
                k_exit, px, why = t, stop, "stop"
                break
            if h[t] >= tgt and not filled_today:
                k_exit, px, why = t, tgt, "hedef"
                break
            if t - t0 >= maxd:
                k_exit, px, why = t, c[t], "süre"
                break
            # sonraki parça: ilk alıştan g seans sonra, hâlâ dipte/yataydaysa
            for g in tranches[1:]:
                if t == t0 + g and setup[t] and len(legs) < len(tranches):
                    orders[t + 1] = c[t] * (1 + lim_above)
            t += 1
        if t0 is None:
            continue
        if k_exit is None:
            k_exit, px, why = n - 1, c[-1], "açık"
        g_id = f"{sym}-{i}"
        for x in legs:
            out.append({"s": sym, "g": g_id, "w": w, "in": idx[x["day"]], "out": idx[k_exit], "e": x["e"],
                        "px": px * (1 - SLIP), "ret": px * (1 - SLIP) / x["e"] - 1,
                        "score": r63[i] if not np.isnan(r63[i]) else -9, "why": why, "days": k_exit - t0,
                        "n_legs": len(legs)})
        busy = k_exit
    return out


def simulate(legs, closes, dates, slots):
    by_in = {}
    for t in legs:
        by_in.setdefault(t["in"], []).append(t)
    cash, held, groups, eq, expo, prev = 1.0, [], {}, [], [], 1.0
    taken = []
    for d in dates:
        for h in [h for h in held if h["t"]["out"] == d]:
            held.remove(h)
            cash += h["q"] * h["t"]["px"]
            if not any(x["t"]["g"] == h["t"]["g"] for x in held):
                groups.pop(h["t"]["g"], None)
        for t in sorted(by_in.get(d, []), key=lambda t: -t["score"]):
            g = t["g"]
            if g not in groups:
                if len(groups) >= slots or any(gg.split("-")[0] == t["s"] for gg in groups):
                    continue
                if t is not min((x for x in legs if x["g"] == g), key=lambda x: x["in"]):
                    continue                               # ilk parçası alınmamış pozisyonun sonraki parçası
                groups[g] = prev / slots                   # pozisyon bütçesi ilk alışta sabitlenir
            amt = min(groups[g] * t["w"], cash)
            if amt <= 1e-9:
                continue
            q = amt / t["e"]
            cash -= amt
            taken.append(t)
            if t["out"] == d:
                cash += q * t["px"]
                if not any(x["t"]["g"] == g for x in held):
                    groups.pop(g, None)
                continue
            held.append({"q": q, "t": t})
        row = closes.loc[d]
        val = cash + sum(h["q"] * (row[h["t"]["s"]] if not np.isnan(row[h["t"]["s"]]) else h["t"]["e"]) for h in held)
        expo.append(1 - cash / val if val else 0)
        eq.append(val)
        prev = val
    return taken, pd.Series(eq, index=dates), float(np.mean(expo))


HDR = ("| Kural | Pozisyon | Ort. parça | Kazanma % | Ort. % (pozisyon) | Ort. kazanç / kayıp % | Ort. gün | Yatırımda % | "
       "Yıllık % | Maks. düşüş % | " + " | ".join(p[0] for p in O.PERIODS) + " |")


def report(name, taken, eq, expo):
    if not taken:
        return f"| {name} | 0 |"
    pos = {}
    for t in taken:
        p = pos.setdefault(t["g"], {"cost": 0.0, "val": 0.0, "n": 0, "days": t["days"]})
        p["cost"] += t["w"]
        p["val"] += t["w"] * (1 + t["ret"])
        p["n"] += 1
    r = np.array([(p["val"] / p["cost"] - 1) * 100 for p in pos.values()])
    w_, lo_ = r[r > 0], r[r <= 0]
    sub = [O.cagr(eq, a, b) for _, a, b in O.PERIODS]
    return (f"| {name} | {len(pos)} | {np.mean([p['n'] for p in pos.values()]):.1f} | {(r > 0).mean()*100:.0f} | "
            f"{r.mean():+.1f} | {(w_.mean() if len(w_) else 0):+.1f} / {(lo_.mean() if len(lo_) else 0):+.1f} | "
            f"{np.mean([p['days'] for p in pos.values()]):.0f} | {expo*100:.0f} | **{O.cagr(eq):+.1f}** | {O.mdd(eq):.1f} | "
            + " | ".join("–" if s is None else f"{s:+.1f}" for s in sub) + " |")


def main():
    cands = C.CANDIDATES + (C.ADRS if C.INCLUDE_ADRS else [])
    prices = D.download_prices(cands + [C.BENCHMARK], period="10y")
    fund = D.fundamentals([t for t in cands if t in prices])
    sh = sharia.screen(fund, {t: df.tail(756) for t, df in prices.items()})
    top, _, _ = pick_universe(fund, set(prices), sh)
    spy = prices[C.BENCHMARK]
    feats = {t: features_taban(prices[t]) for t in top if t in prices and len(prices[t]) > 400}
    dates = spy.index[O.WARM:]
    closes = pd.DataFrame({t: f["Close"] for t, f in feats.items()}).reindex(spy.index).ffill()
    spy_eq = spy["Close"].loc[dates] / spy["Close"].loc[dates].iloc[0]
    log(f"# Orta vade: düşüş sonrası dipte yatay + kademeli alım · {dates[0]:%Y-%m} – {dates[-1]:%Y-%m}")
    log(f"Evren: İslami ilk {len(feats)} · SPY al-tut yıllık **{O.cagr(spy_eq):+.1f}%**, maks. düşüş {O.mdd(spy_eq):.1f}%")
    log("")

    def run(tranches=(0, 5, 10), slots=5, setup_kw=None, **kw):
        legs = []
        for t, d in feats.items():
            legs += positions_for(t, d, setup_taban(d, **(setup_kw or {})).to_numpy(), tranches=tranches, **kw)
        return simulate(legs, closes, dates, slots)

    variants = [
        ("tek seferde al (karşılaştırma)", dict(tranches=(0,))),
        ("3 parça: 0 / 5 / 10. seans", dict(tranches=(0, 5, 10))),
        ("3 parça: 0 / 3 / 6. seans", dict(tranches=(0, 3, 6))),
        ("3 parça: 0 / 10 / 20. seans", dict(tranches=(0, 10, 20))),
        ("4 parça: 0 / 5 / 10 / 15", dict(tranches=(0, 5, 10, 15))),
        ("2 parça: 0 / 10", dict(tranches=(0, 10))),
    ]
    best = []
    for title, extra in [("Stop dibin %5 altı · hedef ort. maliyet +%30", {}),
                         ("Stop dibin %10 altı · hedef +%30", {"stop_below": 0.10}),
                         ("Stop dibin %5 altı · hedef +%20", {"tp": 0.20})]:
        log(f"## {title} · 5 pozisyon")
        log(HDR)
        log("|" + "---|" * (10 + len(O.PERIODS)))
        for name, kw in variants:
            a = dict(kw, **extra)
            taken, eq, ex = run(**a)
            log(report(name, taken, eq, ex))
            if taken:
                best.append((O.cagr(eq) / max(-O.mdd(eq), 1), O.cagr(eq), f"{title} · {name}", a))
        log("")
    best.sort(key=lambda b: -b[0])
    log("## En iyi 5 (yıllık getiri / maks. düşüş)")
    for b in best[:5]:
        log(f"- {b[2]}: yıllık {b[1]:+.1f}%, oran {b[0]:.2f}")
    log("")
    base = dict(tranches=(0, 5, 10))
    log("## Sağlamlık – 3 parça 0/5/10, stop dibin %5 altı, hedef +%30")
    log(HDR)
    log("|" + "---|" * (10 + len(O.PERIODS)))
    for name, kw in [("varsayılan", {}), ("yatay ≥ 2 seans", {"setup_kw": {"min_flat": 2}}),
                     ("yatay ≥ 5 seans", {"setup_kw": {"min_flat": 5}}),
                     ("dibe uzaklık ≤ %5", {"setup_kw": {"near": 0.05}}), ("dibe uzaklık ≤ %15", {"setup_kw": {"near": 0.15}}),
                     ("düşüş ≥ %20", {"setup_kw": {"min_drop": 0.20}}), ("düşüş ≥ %30", {"setup_kw": {"min_drop": 0.30}}),
                     ("yükseliş ≥ %30", {"setup_kw": {"min_rise": 0.30}}), ("yükseliş ≥ %60", {"setup_kw": {"min_rise": 0.60}}),
                     ("3 pozisyon", {"slots": 3}), ("4 pozisyon", {"slots": 4}), ("8 pozisyon", {"slots": 8})]:
        taken, eq, ex = run(**dict(base, **kw))
        log(report(name, taken, eq, ex))
    taken, eq, ex = run(**base)
    log("")
    log("Çıkış nedenleri: " + ", ".join(f"{w} %{v*100:.0f}" for w, v in
                                        pd.Series({t["g"]: t["why"] for t in taken}).value_counts(normalize=True).items()))
    yr = eq.resample("YE").last().pct_change().dropna() * 100
    sy = spy_eq.resample("YE").last().pct_change().dropna() * 100
    log("Yıllara göre (strateji / SPY): " + " · ".join(f"{d_.year}: {v:+.0f}/{sy.get(d_, np.nan):+.0f}" for d_, v in yr.items()))
    seen, last = set(), []
    for t in reversed(taken):
        if t["g"] not in seen:
            seen.add(t["g"])
            last.append(t)
    log("Son 10 pozisyon: " + " · ".join(f"{t['s']} {t['in']:%Y-%m} {t['n_legs']} parça {t['why']}" for t in last[:10]))
    if os.getenv("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write("\n".join(O.OUT) + "\n")


if __name__ == "__main__":
    main()
