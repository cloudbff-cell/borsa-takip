"""Yükselişte kazanma: günlük (gün içi) uzun yönlü momentum stratejileri – 10 yıl 5 dk test (işlem yapmaz).

Mevcut ana strateji düşüşte alır. Bu araç fiyat YÜKSELİRKEN alıp aynı gün satan kurallar arar.
Hepsi yalnız alış (açığa satış yok), kaldıraçsız: en fazla 3 pozisyon, her biri sermayenin 1/3'ü.
Getiri bu nakit modeliyle hesaplanır (risk bazlı boyut, kaldıraç gerektirebileceği için kullanılmaz).

Stratejiler (sinyal 5 dk mum kapanışında, giriş sonraki mumun açılışında, 15:50'de her şey kapanır):
  A  Trend günü (11:00)    : SPY açılıştan +%0.2 ve VWAP üstünde; hisse açılıştan >= +%1, mumların >= %85'i
                             VWAP üstünde, günün zirvesine 0.25 ATR yakın. Stop: max(gün dibi, VWAP - 0.15 ATR).
  A2 aynısı, VWAP altında 5 dk kapanışta çık (iz süren)
  B  Boşluk ve devam       : %1-5 yukarı boşluk, ilk 15 dk hacmi 10 gün ort. >= 2 katı, ilk 5 dk mum yeşil;
                             10:00'a kadar ilk mumun zirvesi kırılınca al. Stop: ilk mumun dibi. VWAP altında çık.
  C  ORB 30 + piyasa yukarı: 30 dk aralık kırılımı, SPY açılıştan yukarı ve VWAP üstünde; stop aralık dibi, kapanışa
                             kadar tut (önceki 12 aylık testte en iyi görünen, burada 10 yılla sınanır).
  C2 aynısı, VWAP altında çık
  D  Zirvede bayrak        : 10:30-14:00, hisse dünden >= +%1, son 30 dk aralığı <= 0.35 ATR ve zirveye yakın,
                             aralık zirvesi kırılınca al. Stop: aralık dibi. Kapanışa kadar tut.
  D2 aynısı, hedef 2R, +1R'de stop girişe
  E  Dünkü güçlü gün devamı: dün >= +%2 ve günün üst %15'inde kapandı; 10:00'da fiyat dünkü kapanış ve VWAP
                             üstünde ise al. Stop: gün dibi. Kapanışa kadar tut.
  F  Gece momentumu (karşılaştırma): bugün >= +%1, günün üst %20'sinde, SPY yukarı -> kapanışta al, ertesi
                             açılışta sat (gece taşır; tam gün içi değil).
Ortak: hisse günlük trendi yukarı (dünkü kapanış > EMA50, EMA20 > EMA50), açılış boşluğu < %5 (B hariç).
Kayma giriş ve çıkışta %0.05. Evren: İslami filtreden geçen ilk 50 (bugünkü liste – hayatta kalma yanlılığı).
Güvenilirlik ölçütü: iki yarıda da pozitif VE yılların en az %70'inde pozitif.
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
from tools import strateji_arastirma as SA  # noqa: E402
from tools.vwap_orb_10y import load5m  # noqa: E402

SLIP = SA.SLIP
SLOTS = 3
OUT = []


def log(s=""):
    print(s, flush=True)
    OUT.append(s)


def at(m, minute):
    i = int(np.searchsorted(m, minute))
    return i if i < len(m) and m[i] == minute else None


def spy_up(spy, i, pct=0.0):
    return spy is not None and i < len(spy["c"]) and spy["c"][i] > spy["vw"][i] and spy["c"][i] / spy["o"][0] - 1 > pct


# ---------------- stratejiler: (x, k, spy) -> (i, stop, sim_kw, skor) ----------------

def s_trend_day(x, k, spy, vwap_exit=False):
    o, c, vw, m = x["o"], x["c"], x["vw"], x["m"]
    i = at(m, 85)                      # 10:55-11:00 mumu kapanışı
    if i is None or not k["trend"] or abs(o[0] / k["pc"] - 1) > 0.05 or not spy_up(spy, i, 0.002):
        return None
    atr = k["atr"]
    if (c[i] / o[0] - 1 >= 0.01 and c[i] > vw[i] and (c[:i + 1] > vw[:i + 1]).mean() >= 0.85
            and c[i] >= x["hod"][i] - 0.25 * atr):
        stop = max(x["lod"][i], vw[i] - 0.15 * atr)
        if stop < c[i] * 0.999:
            return i, stop, {"exit_vwap": vwap_exit}, c[i] / o[0] - 1
    return None


def s_gap_go(x, k, spy):
    o, h, l, c, vw, m = x["o"], x["h"], x["l"], x["c"], x["vw"], x["m"]
    gap = o[0] / k["pc"] - 1
    if not (0.01 <= gap <= 0.05) or not (x["rv15"] >= 2) or c[0] <= o[0] or k["pc"] < k["ema50"]:
        return None
    for i in range(1, len(c)):
        if m[i] > 25:
            return None
        if c[i] > h[0] and c[i] > vw[i]:
            if c[i] - l[0] > 0.6 * k["atr"]:
                return None
            return i, l[0], {"exit_vwap": True}, x["rv15"]
    return None


def s_orb30_up(x, k, spy, vwap_exit=False):
    o, h, l, c, vw, m = x["o"], x["h"], x["l"], x["c"], x["vw"], x["m"]
    orh, orl, atr = h[:6].max(), l[:6].min(), k["atr"]
    if not k["trend"] or abs(o[0] / k["pc"] - 1) > 0.05 or not (x["rv30"] >= 1.2):
        return None
    for i in range(6, len(c)):
        if m[i] > 150:
            return None
        if c[i] > orh and c[i] > vw[i] and spy_up(spy, i):
            if c[i] - orl > 1.0 * atr:
                return None
            return i, orl - 0.02 * atr, {"exit_vwap": vwap_exit}, x["rv30"]
    return None


def s_flag(x, k, spy, target=False):
    o, h, l, c, vw, m = x["o"], x["h"], x["l"], x["c"], x["vw"], x["m"]
    atr = k["atr"]
    if not k["trend"] or abs(o[0] / k["pc"] - 1) > 0.05:
        return None
    for i in range(12, len(c)):
        if m[i] < 60:
            continue
        if m[i] > 270:
            return None
        hi6, lo6 = h[i - 6:i].max(), l[i - 6:i].min()
        if (c[i] / k["pc"] - 1 >= 0.01 and hi6 - lo6 <= 0.35 * atr and hi6 >= x["hod"][i - 1] - 0.1 * atr
                and lo6 > vw[i - 1] and c[i] > hi6 and (spy is None or SA.spy_ok(spy, i))):
            stop = lo6 - 0.02 * atr
            kw = {"target": c[i] + 2 * (c[i] - stop), "be_r": 1.0} if target else {}
            return i, stop, kw, c[i] / k["pc"] - 1
    return None


def s_follow(x, k, spy):
    o, c, vw, m = x["o"], x["c"], x["vw"], x["m"]
    i = at(m, 25)                      # 09:55-10:00 mumu
    if (i is None or not k["trend"] or k.get("pret", 0) < 0.02 or k.get("ploc", 0) < 0.85
            or abs(o[0] / k["pc"] - 1) > 0.05):
        return None
    if c[i] > k["pc"] and c[i] > vw[i] and SA.spy_ok(spy, i):
        stop = x["lod"][i] - 0.05 * k["atr"]
        if stop < c[i] * 0.999:
            return i, stop, {}, k["pret"]
    return None


STRATS = {
    "A Trend günü 11:00, kapanışa kadar": lambda x, k, s: s_trend_day(x, k, s),
    "A2 Trend günü 11:00, VWAP altında çık": lambda x, k, s: s_trend_day(x, k, s, True),
    "B Boşluk ve devam (gap & go)": s_gap_go,
    "C ORB 30 + piyasa yukarı, kapanışa kadar": lambda x, k, s: s_orb30_up(x, k, s),
    "C2 ORB 30 + piyasa yukarı, VWAP altında çık": lambda x, k, s: s_orb30_up(x, k, s, True),
    "D Zirvede bayrak kırılımı, kapanışa kadar": lambda x, k, s: s_flag(x, k, s),
    "D2 Zirvede bayrak, hedef 2R": lambda x, k, s: s_flag(x, k, s, True),
    "E Dünkü güçlü gün devamı": s_follow,
    "Karşılaştırma: ORB 15 dk (eski günlük bot)": SA.s_orb15,
}


def overnight(days_by_sym, ctx_by_sym, spy_days, all_days):
    """F: kapanışta al (son 5 dk mum kapanışı), ertesi gün açılışta sat."""
    out = []
    nxt = dict(zip(all_days, all_days[1:]))
    for d in all_days:
        spy, d2 = spy_days.get(d), nxt.get(d)
        if spy is None or d2 is None or spy["c"][-1] <= spy["o"][0]:
            continue
        cands = []
        for s, days in days_by_sym.items():
            x, k, y = days.get(d), ctx_by_sym[s].get(d), days.get(d2)
            if x is None or k is None or y is None or not k["trend"]:
                continue
            c, hi, lo = x["c"][-1], x["h"].max(), x["l"].min()
            if c / k["pc"] - 1 >= 0.01 and hi > lo and (c - lo) / (hi - lo) >= 0.8:
                e, ex = c * (1 + SLIP), y["o"][0] * (1 - SLIP)
                cands.append((-(c / k["pc"] - 1), {"pct": (ex / e - 1) * 100, "d": d, "s": s, "why": "ertesi açılış"}))
        cands.sort(key=lambda z: z[0])
        out += [z[1] for z in cands[:SLOTS]]
    return out


# ---------------- sonuç ----------------

def equity(tr, all_days):
    """Nakit modeli: her işlem sermayenin 1/SLOTS'u; günlük getiri = işlemlerin % ortalaması x pay."""
    daily = pd.Series(0.0, index=pd.Index(all_days))
    if tr:
        g = pd.Series([t["pct"] / 100 / SLOTS for t in tr], index=[t["d"] for t in tr]).groupby(level=0).sum()
        daily = daily.add(g, fill_value=0.0)
    return (1 + daily.sort_index()).cumprod()


def summary_row(name, tr, all_days, mid, years):
    eq = equity(tr, all_days)
    if not tr:
        return f"| {name} | 0 | | | | | | | |", None
    p = np.array([t["pct"] for t in tr])
    g, ls = p[p > 0].sum(), -p[p < 0].sum()
    yrs = (pd.Timestamp(all_days[-1]) - pd.Timestamp(all_days[0])).days / 365.25
    cagr = (eq.iloc[-1] ** (1 / yrs) - 1) * 100
    dd = ((eq / eq.cummax()) - 1).min() * 100
    h1 = np.mean([t["pct"] for t in tr if t["d"] < mid] or [0])
    h2 = np.mean([t["pct"] for t in tr if t["d"] >= mid] or [0])
    ys = {}
    for y in years:
        e = eq[[d.year == y for d in eq.index]]
        prev = eq[[d.year < y for d in eq.index]]
        if len(e):
            ys[y] = (e.iloc[-1] / (prev.iloc[-1] if len(prev) else 1.0) - 1) * 100
    pos = sum(1 for v in ys.values() if v > 0)
    ok = "✅" if h1 > 0 and h2 > 0 and pos >= 0.7 * len(ys) else "❌"
    return (f"| {name} | {len(tr)} | {(p > 0).mean()*100:.0f} | {p.mean():+.3f} | {g/ls if ls else 0:.2f} | "
            f"{cagr:+.1f} | {dd:.1f} | {h1:+.3f} · {h2:+.3f} | {pos}/{len(ys)} | {ok} |"), ys


def main():
    cands = C.CANDIDATES + (C.ADRS if C.INCLUDE_ADRS else [])
    prices = D.download_prices(cands + [C.BENCHMARK], period="10y")
    fund = D.fundamentals([t for t in cands if t in prices])
    sh = sharia.screen(fund, {t: df.tail(756) for t, df in prices.items()})
    top, _, _ = pick_universe(fund, set(prices), sh)
    raw = load5m(top + [C.BENCHMARK])
    spy_days = SA.split_days(raw.pop(C.BENCHMARK)) if C.BENCHMARK in raw else {}
    days_by_sym, ctx_by_sym = {}, {}
    for s in top:
        if s not in raw or s not in prices:
            continue
        days = SA.split_days(raw.pop(s))
        SA.add_rvol(days)
        ctx = SA.daily_ctx(prices[s])
        ds = sorted(days)
        for a, b in zip(ds, ds[1:]):     # dünkü kapanış/zirve/gün yapısı gün içi veriden (düzeltme farkı olmasın)
            kb, xa = ctx.get(b), days[a]
            if kb:
                hi, lo, cl = float(xa["h"].max()), float(xa["l"].min()), float(xa["c"][-1])
                pc_a = ctx.get(a, {}).get("pc")
                kb.update(pc=cl, ph=hi, ploc=(cl - lo) / (hi - lo) if hi > lo else 0.5,
                          pret=(cl / pc_a - 1) if pc_a else 0.0)
        for d in days:                    # bellek: kullanılmayan dizi
            days[d].pop("sd", None)
        days_by_sym[s], ctx_by_sym[s] = days, ctx
    all_days = sorted({d for v in days_by_sym.values() for d in v})[12:]
    mid = all_days[len(all_days) // 2]
    years = sorted({d.year for d in all_days})
    log(f"## Yükselişte kazanma – günlük momentum stratejileri ({all_days[0]} – {all_days[-1]}, "
        f"{len(all_days)} işlem günü, {len(days_by_sym)} hisse, 5 dk)")
    spy = prices[C.BENCHMARK]["Close"].loc[str(all_days[0]):str(all_days[-1])]
    yrs = (spy.index[-1] - spy.index[0]).days / 365.25
    log(f"Karşılaştırma: SPY al-tut yıllık {((spy.iloc[-1]/spy.iloc[0])**(1/yrs)-1)*100:+.1f}% · "
        f"en büyük düşüş {((spy/spy.cummax())-1).min()*100:.1f}%")
    log(f"Nakit modeli: en fazla {SLOTS} pozisyon x sermayenin 1/{SLOTS}'ü, kaldıraç yok, kayma %{SLIP*100:.2f} x 2.")
    log("")
    log("| Strateji | İşlem | Kazanma % | Ort. % / işlem | Kâr faktörü | Yıllık getiri % | Maks. düşüş % | "
        "Ort. % 1. yarı · 2. yarı | Pozitif yıl | Güvenilir |")
    log("|---|---|---|---|---|---|---|---|---|---|")
    results, by_year = {}, {}
    for name, fn in STRATS.items():
        tr = SA.run_strategy(fn, days_by_sym, ctx_by_sym, spy_days, all_days)
        results[name] = tr
        line, ys = summary_row(name, tr, all_days, mid, years)
        by_year[name] = ys
        log(line)
    name = "F Gece momentumu (kapanışta al, ertesi açılışta sat)"
    results[name] = overnight(days_by_sym, ctx_by_sym, spy_days, all_days)
    line, by_year[name] = summary_row(name, results[name], all_days, mid, years)
    log(line)

    log("")
    log("### Yıllara göre getiri % (nakit modeli)")
    short = {n: ("ORB15" if n.startswith("Karşılaştırma") else n.split(" ")[0]) for n in results}
    log("| Yıl | " + " | ".join(short.values()) + " | SPY |")
    log("|---" * (len(short) + 2) + "|")
    for y in years:
        s_y = spy[spy.index.year == y]
        s_prev = spy[spy.index.year < y]
        s_ret = (s_y.iloc[-1] / (s_prev.iloc[-1] if len(s_prev) else s_y.iloc[0]) - 1) * 100 if len(s_y) else 0
        cells = [f"{(by_year[n] or {}).get(y, 0):+.1f}" for n in results]
        log(f"| {y} | " + " | ".join(cells) + f" | {s_ret:+.1f} |")

    log("")
    log("### Çıkış türleri")
    for n, tr in results.items():
        if tr:
            w = pd.Series([t["why"] for t in tr]).value_counts()
            log(f"- {short[n]}: " + ", ".join(f"{k} {v}" for k, v in w.items()))
    if os.getenv("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write("\n".join(OUT) + "\n")


if __name__ == "__main__":
    main()
