"""VWAP + EMA9 geri çekilme alım stratejisi – 10 yıl 5 dk veriyle test (yalnız analiz, işlem yapmaz).

Yalnız alış (İslami kural). Her gün, her hisse için (New York saati, 5 dk mumlar):
  Zaman          : sinyal 10:00-15:00 arası (ilk 30 dk atlanır)
  Gün filtresi   : kapanış > VWAP ve EMA9 > VWAP (gün alıcıların kontrolünde)
  Piyasa filtresi: SPY aynı mumda kendi VWAP'ının üstünde
  Geri çekilme   : son 3 mumdan birinin dibi EMA9'a değmiş (%0.1 tolerans)
  Teyit          : mum EMA9 üstünde yeşil kapanır, hacmi önceki 20 mumun ortalamasından büyük
  Kovalama yasağı: kapanış VWAP'tan en fazla %1.2 yukarıda
  Giriş          : teyit mumundan sonraki mumun açılışı
  Stop           : son 3 mumun en düşüğü - %0.05, ama girişten en fazla %1 aşağıda; risk en az %0.2
  Çıkış          : hedef = giriş + 2R; ya da üst üste 2 mum EMA9 altında kapanırsa sonraki açılışta;
                   15:50'de her şey kapanır
  Hisse başına günde en fazla 2 işlem (biri kapanmadan ikincisi açılmaz).
Risk kuralları (VWAP+ORB testiyle aynı, karşılaştırılabilir olsun diye): işlem başı sermayenin %0.5'i,
  tek pozisyon en fazla sermayenin %50'si, günde en fazla 4 işlem, günlük zarar %1.5'te ya da
  üst üste 2 zararda o gün dur. Kayma: giriş+çıkış %0.05.
Evren: İslami filtreden geçen ilk 50 (bugünkü liste – hayatta kalma yanlılığı var).
"""
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

OUT = []


def log(s=""):
    print(s, flush=True)
    OUT.append(s)


# ---------------- sinyaller (vektörel) ----------------

def prepare(g, spy_ok, p):
    """g: indicators() çıktısı. Sinyal maskesi ve gereken dizileri döndürür."""
    day = g.index.normalize()
    same1 = pd.Series(day, index=g.index).shift(1) == day
    same2 = pd.Series(day, index=g.index).shift(2) == day
    l, e9 = g["l"], g["ema9"]
    l1, l2 = l.shift(1).where(same1), l.shift(2).where(same2)
    e91, e92 = e9.shift(1).where(same1), e9.shift(2).where(same2)
    tol = 1.001
    touch = (l <= e9 * tol) | (l1 <= e91 * tol) | (l2 <= e92 * tol)
    mins = (g.index.hour * 60 + g.index.minute) - 570
    c, o, vw = g["c"], g["o"], g["vwap"]
    sig = ((mins >= 30) & (mins <= 330) & (c > vw) & (e9 > vw) & (c > e9) & (c > o) & touch
           & (g["v"] > g["vavg"]) & (g["vavg"] > 0))
    if p["chase"]:
        sig &= (c / vw - 1) <= 0.012
    if p["spy"]:
        sig &= spy_ok.reindex(g.index).fillna(False).astype(bool)
    if p["daily"]:
        # günlük trend: dünkü kapanış > 50 günlük ortalama (5 dk veriden günlük kapanış)
        dc = c.groupby(day).last()
        up = (dc > dc.rolling(50).mean()).shift(1).fillna(False).astype(bool)
        sig &= pd.Series(up.reindex(day).to_numpy(), index=g.index)
    swing = pd.concat([l, l1, l2], axis=1).min(axis=1)
    return {
        "sig": sig.fillna(False).to_numpy(),
        "o": o.to_numpy(), "h": g["h"].to_numpy(), "l": l.to_numpy(), "c": c.to_numpy(),
        "e9": e9.to_numpy(), "swing": swing.to_numpy(), "mins": np.asarray(mins),
        "day": np.asarray(day), "idx": g.index,
    }


def trades_for_symbol(s, a, p, slip):
    """Bir hissenin tüm sinyallerini sırayla işler -> işlem adayları listesi."""
    out = []
    o, h, l, c, e9, mins, day = a["o"], a["h"], a["l"], a["c"], a["e9"], a["mins"], a["day"]
    n = len(c)
    busy_until = -1
    per_day = {}
    for i in np.flatnonzero(a["sig"]):
        k = i + 1
        if k >= n or day[k] != day[i] or i <= busy_until:
            continue
        dkey = day[i]
        if per_day.get(dkey, 0) >= 2:
            continue
        e_raw = o[k]
        stop = max(a["swing"][i] * 0.9995, e_raw * 0.99)
        if not (stop < e_raw * 0.998):           # risk en az %0.2 olmalı
            continue
        e = e_raw * (1 + slip)
        R = e - stop
        tgt = e + 2 * R
        ex, why, jx = None, None, None
        j = k
        while j < n and day[j] == dkey:
            if mins[j] >= 380:
                ex, why, jx = o[j], "gün sonu", j
                break
            if l[j] <= stop:
                ex, why, jx = min(o[j], stop), "stop", j
                break
            if p["target"] and j > k and h[j] >= tgt:
                ex, why, jx = max(o[j], tgt), "hedef", j
                break
            if j > k and c[j] < e9[j] and c[j - 1] < e9[j - 1]:
                if j + 1 < n and day[j + 1] == dkey:
                    ex, why, jx = o[j + 1], "EMA9 altı", j + 1
                else:
                    ex, why, jx = c[j], "EMA9 altı", j
                break
            j += 1
        if ex is None:                           # gün verisi erken bitti
            jx = j - 1
            ex, why = c[jx], "gün sonu"
        ex = float(ex) * (1 - slip)
        out.append({"d": pd.Timestamp(dkey).date(), "s": s, "t_in": int(mins[k]), "t_out": int(mins[jx]),
                    "e": float(e), "R": float(R), "ex": ex, "why": why})
        per_day[dkey] = per_day.get(dkey, 0) + 1
        busy_until = jx
    return out


# ---------------- portföy ----------------

def portfolio(cands, all_days, rules=True):
    by_day = {}
    for x in cands:
        by_day.setdefault(x["d"], []).append(x)
    E, curve, trades = 1.0, [], []
    for d in all_days:
        xs = sorted(by_day.get(d, []), key=lambda x: x["t_in"])
        day_pl, n, losses, open_ = 0.0, 0, 0, []
        start_E = E
        for x in xs:
            for q in [q for q in open_ if q["t_out"] <= x["t_in"]]:
                open_.remove(q)
                day_pl += q["pl"]
                losses = losses + 1 if q["pl"] < 0 else 0
            if rules and (n >= 4 or day_pl <= -0.015 * start_E or losses >= 2):
                break
            if any(q["s"] == x["s"] for q in open_):
                continue
            notional = min(0.005 * start_E / x["R"] * x["e"], 0.5 * start_E,
                           start_E - sum(q["notional"] for q in open_))
            if notional <= 0:
                continue
            ret = x["ex"] / x["e"] - 1
            pl = notional * ret
            open_.append({"t_out": x["t_out"], "pl": pl, "notional": notional, "s": x["s"]})
            trades.append({"d": d, "s": x["s"], "r": (x["ex"] - x["e"]) / x["R"], "ret": ret,
                           "pl_eq": pl / start_E, "why": x["why"]})
            n += 1
        day_pl += sum(q["pl"] for q in open_)
        E += day_pl
        curve.append(E)
    return pd.Series(curve, index=pd.to_datetime(list(all_days))), trades


def row(name, eq, tr, base=1.0):
    if len(eq) < 2:
        log(f"| {name} | 0 | – | – | – | – | – | – |")
        return
    yrs = max((eq.index[-1] - eq.index[0]).days / 365.25, 1 / 365.25)
    tot = eq.iloc[-1] / base
    cagr = (tot ** (1 / yrs) - 1) * 100 if yrs >= 1 else (tot - 1) * 100
    full = pd.concat([pd.Series([base]), eq.reset_index(drop=True)])
    dd = ((full / full.cummax()) - 1).min() * 100
    r = np.array([t["r"] for t in tr]) if tr else np.array([0.0])
    pf = r[r > 0].sum() / -r[r < 0].sum() if (r < 0).any() else float("nan")
    lab = "Yıllık getiri %" if yrs >= 1 else "Dönem getirisi %"
    log(f"| {name} | {len(tr)} | {len(tr)/yrs:.0f} | {(r > 0).mean()*100:.0f} | {r.mean():+.3f} | {pf:.2f} | "
        f"**{cagr:+.1f}** ({lab.split()[0].lower()}) | {dd:.1f} |")


VARIANTS = [
    ("Ana sistem (SPY filtresi, hedef 2R, kayma %0.05)", dict(spy=True, chase=True, daily=False, target=True), 0.0005),
    ("Aynısı, kayma %0.02", dict(spy=True, chase=True, daily=False, target=True), 0.0002),
    ("SPY filtresi yok", dict(spy=False, chase=True, daily=False, target=True), 0.0005),
    ("Kovalama yasağı yok", dict(spy=True, chase=False, daily=False, target=True), 0.0005),
    ("Hedef yok, yalnız EMA9 çıkışı", dict(spy=True, chase=True, daily=False, target=False), 0.0005),
    ("+ günlük trend (dün kapanış > SMA50)", dict(spy=True, chase=True, daily=True, target=True), 0.0005),
]


def main():
    from engine import config as C
    from engine import data as D
    from engine import sharia
    from engine.run import pick_universe
    from tools.vwap_orb_10y import indicators, load5m

    cands = C.CANDIDATES + (C.ADRS if C.INCLUDE_ADRS else [])
    prices = D.download_prices(cands + [C.BENCHMARK], period="10y")
    fund = D.fundamentals([t for t in cands if t in prices])
    sh = sharia.screen(fund, {t: df.tail(756) for t, df in prices.items()})
    top, _, _ = pick_universe(fund, set(prices), sh)
    raw = load5m(list(top) + [C.BENCHMARK])
    spy5 = raw.pop(C.BENCHMARK, None)
    if spy5 is None:
        log("SPY 5 dk verisi alınamadı, test durdu.")
        return
    sg = indicators(spy5)
    spy_ok = sg["c"] > sg["vwap"]
    G = {s: indicators(df) for s, df in raw.items()}
    all_days = sorted({d for g in G.values() for d in g.index.date})

    spy = prices[C.BENCHMARK]["Close"]
    sp = spy.loc[str(all_days[0]):str(all_days[-1])]
    yrs = (sp.index[-1] - sp.index[0]).days / 365.25
    log(f"## VWAP + EMA9 geri çekilme – {all_days[0]} – {all_days[-1]} ({yrs:.1f} yıl, {len(G)} hisse, 5 dk)")
    log(f"SPY al-tut yıllık {((sp.iloc[-1]/sp.iloc[0])**(1/yrs)-1)*100:+.1f}% · "
        f"en büyük düşüş {((sp/sp.cummax())-1).min()*100:.1f}%")
    log("")
    hdr = ("| Varyant | İşlem | Yılda | Kazanma % | Ort. R | Kâr faktörü | Getiri % | Maks. düşüş % |\n"
           "|---|---|---|---|---|---|---|---|")
    log(hdr)
    results = {}
    for name, p, slip in VARIANTS:
        cands_all = []
        for s, g in G.items():
            cands_all += trades_for_symbol(s, prepare(g, spy_ok, p), p, slip)
        eq, tr = portfolio(cands_all, all_days)
        results[name] = (eq, tr)
        row(name, eq, tr)
    base_name = VARIANTS[0][0]
    eq, tr = results[base_name]

    log("")
    log("### Son 6 ay ve son 1 yıl (ana sistem)")
    log(hdr)
    for lab, months in (("Son 1 yıl", 12), ("Son 6 ay", 6)):
        cut = eq.index[-1] - pd.DateOffset(months=months)
        before = eq[eq.index <= cut]
        b = before.iloc[-1] if len(before) else 1.0
        row(lab, eq[eq.index > cut], [t for t in tr if pd.Timestamp(t["d"]) > cut], base=b)

    log("")
    log("### Yıllara göre (ana sistem)")
    log("| Yıl | İşlem | Kazanma % | Ort. R | Yıl getirisi % | SPY % |")
    log("|---|---|---|---|---|---|")
    for y in sorted({t["d"].year for t in tr}):
        t_y = [t for t in tr if t["d"].year == y]
        e_y = eq[eq.index.year == y]
        prev = eq[eq.index.year < y]
        y_ret = (e_y.iloc[-1] / (prev.iloc[-1] if len(prev) else 1.0) - 1) * 100
        s_y = spy[spy.index.year == y]
        s_prev = spy[spy.index.year < y]
        s_ret = (s_y.iloc[-1] / (s_prev.iloc[-1] if len(s_prev) else s_y.iloc[0]) - 1) * 100
        r = np.array([t["r"] for t in t_y])
        log(f"| {y} | {len(t_y)} | {(r > 0).mean()*100:.0f} | {r.mean():+.2f} | {y_ret:+.1f} | {s_ret:+.1f} |")
    w = pd.Series([t["why"] for t in tr]).value_counts() if tr else pd.Series(dtype=int)
    log("")
    log("Çıkışlar: " + ", ".join(f"{k} {v}" for k, v in w.items()))
    if os.getenv("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write("\n".join(OUT) + "\n")


if __name__ == "__main__":
    main()
