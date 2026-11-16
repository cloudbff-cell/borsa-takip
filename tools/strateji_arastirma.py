"""Günlük işlem strateji araştırması (işlem yapmaz).

1) ORB (mevcut strateji) neden zarar ediyor: çıkış türü, giriş saati, piyasa yönü, MFE (girişten sonra
   görülen en yüksek kâr) ve "kapanışa kadar tutsaydık" analizi.
2) Alternatif uzun-yönlü (açığa satışsız) günlük stratejiler aynı veri, aynı maliyet ve aynı portföy
   kısıtıyla (günde en fazla 3 işlem, sinyal sırasına göre) karşılaştırılır.
3) Sağlamlık: dönem iki yarıya bölünür; iki yarıda da pozitif olmayan strateji güvenilir sayılmaz.

Veri: Alpaca 5 dk (SIP, ~12 ay; olmazsa IEX, olmazsa Yahoo 60 gün). Evren: İslami filtreden geçen ilk 50
(bugünkü liste – hafif hayatta kalma yanlılığı). Kayma: giriş ve çıkışta %0.05.
"""
import json
import os
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from engine import config as C  # noqa: E402
from engine import data as D  # noqa: E402
from engine import sharia  # noqa: E402
from engine import trader  # noqa: E402
from engine.indicators import add_indicators  # noqa: E402
from engine.run import pick_universe  # noqa: E402

NY = "America/New_York"
SLIP = float(os.getenv("SLIP", "0.0005"))
MAX_PER_DAY = 3
FLAT_MIN = 380          # 15:50 = açılıştan 380 dk sonra
OUT = []


def log(*a):
    s = " ".join(str(x) for x in a)
    print(s)
    OUT.append(s)


# ---------------- veri ----------------

def load_intraday(symbols, months=12):
    end = pd.Timestamp.now(tz="UTC") - pd.Timedelta(minutes=20)
    start = end - pd.DateOffset(months=months)
    api = trader.Alpaca()
    for feed in ("sip", "iex"):
        try:
            out = {}
            for i in range(0, len(symbols), 10):
                raw = api.bars(symbols[i:i + 10], start=start.isoformat(), end=end.isoformat(), feed=feed,
                               adjustment="split")
                for s, rows in raw.items():
                    df = pd.DataFrame(rows)
                    df.index = pd.to_datetime(df["t"], utc=True).dt.tz_convert(NY)
                    out[s] = df[["o", "h", "l", "c", "v"]].astype(float)
            if len(out) > len(symbols) * 0.8:
                log(f"Veri: Alpaca {feed.upper()}, {start:%Y-%m-%d} – {end:%Y-%m-%d}, {len(out)} sembol")
                return out
        except Exception as e:  # noqa: BLE001
            log(f"Alpaca {feed} verisi alınamadı: {str(e)[:200]}")
    from tools.daytrade_test import intraday
    log("Veri: Yahoo 5 dk, son 60 gün (Alpaca başarısız)")
    return intraday(symbols)


def split_days(df):
    """{tarih: dict(numpy dizileri)} – yalnız normal seans, tam günler."""
    df = df[(df.index.hour * 60 + df.index.minute >= 570) & (df.index.hour * 60 + df.index.minute < 960)]
    out = {}
    for d, g in df.groupby(df.index.date):
        if len(g) < 70 or g.index[0].hour * 60 + g.index[0].minute != 570:
            continue
        mins = (g.index.hour * 60 + g.index.minute - 570).to_numpy()
        o, h, l, c, v = (g[k].to_numpy() for k in "ohlcv")
        tp = (h + l + c) / 3
        cv = np.cumsum(v)
        vw = np.cumsum(tp * v) / np.where(cv > 0, cv, np.nan)
        var = np.cumsum(v * (tp - vw) ** 2) / np.where(cv > 0, cv, np.nan)
        out[d] = {"m": mins, "o": o, "h": h, "l": l, "c": c, "v": v, "vw": vw, "sd": np.sqrt(np.maximum(var, 0)),
                  "cv": cv, "hod": np.maximum.accumulate(h), "lod": np.minimum.accumulate(l)}
    return out


def daily_ctx(df):
    d = add_indicators(df)
    prev = d.shift(1)          # her gün için yalnız dünkü veriler
    ctx = {}
    for t, r in prev.iterrows():
        if np.isnan(r["atr"]) or np.isnan(r["ema50"]):
            continue
        ctx[t.date()] = {"pc": r["Close"], "ph": r["High"], "atr": r["atr"], "ema20": r["ema20"], "ema50": r["ema50"],
                         "trend": bool(r["Close"] > r["ema50"] and r["ema20"] > r["ema50"])}
    return ctx


def add_rvol(days, n=10):
    """Açılış hacmi (ilk 15 / 30 dk) ve kümülatif hacim için önceki n günün ortalaması."""
    ds = sorted(days)
    hist15, hist30, histcv = [], [], []
    for d in ds:
        x = days[d]
        x["rv15"] = x["cv"][2] / np.mean(hist15[-n:]) if len(hist15) >= 3 else np.nan
        x["rv30"] = x["cv"][5] / np.mean(hist30[-n:]) if len(hist30) >= 3 else np.nan
        x["cvavg"] = np.mean(histcv[-n:], axis=0) if len(histcv) >= 3 else None
        hist15.append(x["cv"][2])
        hist30.append(x["cv"][5])
        if len(x["cv"]) >= 78:
            histcv.append(x["cv"][:78])


# ---------------- simülasyon ----------------

def sim(x, i, stop, target=None, be_r=None, exit_vwap=False, time_exit=FLAT_MIN):
    """i: sinyal mumu; giriş i+1 açılışı. Aynı mumda stop ve hedef varsa stop varsayılır."""
    o, h, l, c, m, vw = x["o"], x["h"], x["l"], x["c"], x["m"], x["vw"]
    n = len(o)
    if i + 1 >= n:
        return None
    e = o[i + 1] * (1 + SLIP)
    risk = e - stop
    if risk <= e * 0.001:
        return None
    hi, why, ex = e, "zaman", None
    j = i + 1
    for j in range(i + 1, n):
        if m[j] >= min(time_exit, FLAT_MIN):
            ex = o[j]
            break
        if l[j] <= stop:
            ex, why = min(o[j], stop), ("başa baş" if stop >= e else "stop")
            break
        hi = max(hi, h[j])
        if target is not None and h[j] >= target:
            ex, why = max(o[j], target), "hedef"
            break
        if be_r and h[j] >= e + be_r * risk:
            stop = max(stop, e)
        if exit_vwap and c[j] < vw[j]:
            ex, why = c[j], "vwap altı"
            break
    if ex is None:
        ex = c[-1]
    ex *= 1 - SLIP
    hold = (c[min(np.searchsorted(m, FLAT_MIN), n - 1)] * (1 - SLIP) - e) / risk
    return {"r": (ex - e) / risk, "pct": (ex / e - 1) * 100, "why": why, "mfe": (hi - e) / risk,
            "min": int(m[i]), "bars": j - i, "hold_r": hold}


# ---------------- stratejiler (her biri: x, ctx, spy -> (i, stop, sim_kw, skor) ya da None) ----------------

def s_orb15(x, k, spy):
    o, h, l, c, m, vw = x["o"], x["h"], x["l"], x["c"], x["m"], x["vw"]
    orh, orl, atr = h[:3].max(), l[:3].min(), k["atr"]
    gap = o[0] / k["pc"] - 1
    if not k["trend"] or abs(gap) > 0.04 or not (x["rv15"] >= 1.5) or orh - orl < 0.1 * atr or c[0] < 10:
        return None
    for i in range(3, len(c)):
        if m[i] + 5 > 120:
            return None
        if c[i] > orh and c[i] - orh <= 0.25 * atr and c[i] > vw[i] and c[i] > o[i] and spy_ok(spy, i):
            stop = c[i] - max(c[i] - max(orl, c[i] - 0.35 * atr), 0.002 * c[i])
            return i, stop, {"target": c[i] + 2 * (c[i] - stop), "be_r": 1.0}, x["rv15"]
    return None


def s_orb30_hold(x, k, spy):
    """30 dk aralık kırılımı, stop aralık dibi, hedef yok: trend gününü kapanışa kadar taşı."""
    o, h, l, c, m, vw = x["o"], x["h"], x["l"], x["c"], x["m"], x["vw"]
    orh, orl, atr = h[:6].max(), l[:6].min(), k["atr"]
    if not k["trend"] or abs(o[0] / k["pc"] - 1) > 0.04 or not (x["rv30"] >= 1.2):
        return None
    for i in range(6, len(c)):
        if m[i] > 150:
            return None
        if c[i] > orh and c[i] > vw[i] and spy_ok(spy, i):
            if c[i] - orl > 1.0 * atr:
                return None
            return i, orl - 0.02 * atr, {}, x["rv30"]
    return None


def s_vwap_pullback(x, k, spy):
    """Yükselen günde VWAP'a geri çekilme ve tepki: 10:30-14:30."""
    o, h, l, c, m, vw = x["o"], x["h"], x["l"], x["c"], x["m"], x["vw"]
    atr = k["atr"]
    if not k["trend"]:
        return None
    above = np.cumsum(c > vw)
    for i in range(12, len(c)):
        if m[i] < 60:
            continue
        if m[i] > 300:
            return None
        if (above[i - 1] / i >= 0.8 and c[i] > k["pc"] and l[i] <= vw[i] * 1.001 and c[i] > vw[i] and c[i] > o[i]
                and spy_ok(spy, i)):
            stop = min(l[i], vw[i]) - 0.1 * atr
            return i, stop, {"target": c[i] + 2 * (c[i] - stop), "be_r": 1.0}, c[i] / k["pc"] - 1
    return None


def s_gap_reversal(x, k, spy):
    """Yükseliş trendindeki hissede aşağı boşluk (%1.5-6), VWAP'ı geri alınca al, hedef dünkü kapanış."""
    o, h, l, c, m, vw = x["o"], x["h"], x["l"], x["c"], x["m"], x["vw"]
    gap = o[0] / k["pc"] - 1
    if not (k["pc"] > k["ema50"] and -0.06 <= gap <= -0.015):
        return None
    for i in range(6, len(c)):
        if m[i] > 150:
            return None
        if c[i] > vw[i] and c[i - 1] <= vw[i - 1] and c[i] < k["pc"]:
            stop = x["lod"][i] - 0.05 * k["atr"]
            return i, stop, {"target": k["pc"]}, -gap
    return None


def s_vwap_band(x, k, spy):
    """Aşırı satım dönüşü: VWAP - 2σ altından içeri dönüş, hedef VWAP (10:00-15:00)."""
    o, h, l, c, m, vw, sd = x["o"], x["h"], x["l"], x["c"], x["m"], x["vw"], x["sd"]
    if not k["trend"]:
        return None
    for i in range(7, len(c)):
        if m[i] < 30:
            continue
        if m[i] > 330:
            return None
        lo = vw[i - 1] - 2 * sd[i - 1]
        if c[i - 1] < lo and c[i] > vw[i] - 2 * sd[i] and c[i] > o[i]:
            stop = l[i - 3:i + 1].min() - 0.1 * k["atr"]
            if vw[i] - c[i] < 0.5 * (c[i] - stop):
                continue
            return i, stop, {"target": vw[i]}, (vw[i] - c[i]) / k["atr"]
    return None


def s_pdh_breakout(x, k, spy):
    """Dünkü zirvenin yukarı kırılımı (10:00-13:00), hacim ortalamanın üstünde."""
    o, h, l, c, m, vw = x["o"], x["h"], x["l"], x["c"], x["m"], x["vw"]
    atr, ph = k["atr"], k["ph"]
    if not k["trend"] or o[0] > ph or x["cvavg"] is None:
        return None
    for i in range(6, len(c)):
        if m[i] > 210:
            return None
        if c[i] > ph and c[i - 1] <= ph and c[i] > vw[i] and x["cv"][i] >= 1.2 * x["cvavg"][min(i, 77)] and spy_ok(spy, i):
            stop = c[i] - 0.4 * atr
            return i, stop, {"target": c[i] + 2 * 0.4 * atr, "be_r": 1.0}, x["cv"][i] / x["cvavg"][min(i, 77)]
    return None


def s_power_hour(x, k, spy):
    """14:30'da günü güçlü (+%1, VWAP üstü, zirveye yakın) hisse; kapanışa kadar tut."""
    o, h, l, c, m, vw = x["o"], x["h"], x["l"], x["c"], x["m"], x["vw"]
    i = int(np.searchsorted(m, 295))
    if i >= len(c) or m[i] != 295:
        return None
    if (c[i] / k["pc"] - 1 > 0.01 and c[i] > vw[i] and c[i] >= 0.995 * x["hod"][i] and spy is not None
            and spy["c"][i] > spy["o"][0]):
        return i, c[i] - 0.3 * k["atr"], {}, c[i] / k["pc"] - 1
    return None


def spy_ok(spy, i):
    return spy is None or i >= len(spy["c"]) or spy["c"][i] > spy["vw"][i]


def rs_momentum(days_by_sym, ctx_by_sym, spy_days, d):
    """10:00'da SPY'a göre en güçlü 3 hisse (VWAP üstü, trend yukarı), stop 0.5 ATR, kapanışa kadar tut."""
    spy = spy_days.get(d)
    if spy is None or spy["c"][5] < spy["vw"][5]:
        return []
    spy_ret = spy["c"][5] / spy["o"][0] - 1
    rows = []
    for s, days in days_by_sym.items():
        x, k = days.get(d), ctx_by_sym[s].get(d)
        if x is None or k is None or not k["trend"] or abs(x["o"][0] / k["pc"] - 1) > 0.04:
            continue
        if x["c"][5] > x["vw"][5] and x["rv30"] >= 1.0:
            rows.append((x["c"][5] / x["o"][0] - 1 - spy_ret, s, x, k))
    rows.sort(key=lambda r: -r[0])
    out = []
    for ex, s, x, k in rows[:MAX_PER_DAY]:
        if ex <= 0:
            break
        r = sim(x, 5, x["c"][5] - 0.5 * k["atr"])
        if r:
            out.append(dict(r, s=s, d=d))
    return out


STRATS = {
    "ORB 15 dk (mevcut)": s_orb15,
    "ORB 30 dk, kapanışa kadar tut": s_orb30_hold,
    "VWAP geri çekilme": s_vwap_pullback,
    "Aşağı boşluk dönüşü": s_gap_reversal,
    "VWAP -2σ aşırı satım dönüşü": s_vwap_band,
    "Dünkü zirve kırılımı": s_pdh_breakout,
    "Güç saati (14:30-15:50)": s_power_hour,
}


def run_strategy(fn, days_by_sym, ctx_by_sym, spy_days, all_days):
    trades = []
    for d in all_days:
        cands = []
        spy = spy_days.get(d)
        for s, days in days_by_sym.items():
            x, k = days.get(d), ctx_by_sym[s].get(d)
            if x is None or k is None or x.get("rv15") is None:
                continue
            sig = fn(x, k, spy)
            if not sig:
                continue
            i, stop, kw, score = sig
            r = sim(x, i, stop, **kw)
            if r:
                cands.append((i, -score, dict(r, s=s, d=d)))
        cands.sort(key=lambda z: (z[0], z[1]))
        trades += [z[2] for z in cands[:MAX_PER_DAY]]
    return trades


def stats(tr, risk=0.01):
    if not tr:
        return {"n": 0}
    r = np.array([t["r"] for t in tr])
    p = np.array([t["pct"] for t in tr])
    g, ls = r[r > 0].sum(), -r[r < 0].sum()
    daily = pd.Series(r * risk, index=[t["d"] for t in tr]).groupby(level=0).sum().sort_index()
    eq = (1 + daily).cumprod()
    return {"n": len(tr), "win": round((r > 0).mean() * 100, 1), "avgR": round(r.mean(), 3), "avgPct": round(p.mean(), 3),
            "pf": round(g / ls, 2) if ls else None, "ret": round((eq.iloc[-1] - 1) * 100, 1),
            "dd": round(((eq / eq.cummax()) - 1).min() * 100, 1)}


def row(name, tr, mid):
    a, b1, b2 = stats(tr), stats([t for t in tr if t["d"] < mid]), stats([t for t in tr if t["d"] >= mid])
    f = lambda s: f"{s['avgR']:+.2f}R / PF {s['pf']}" if s["n"] else "–"
    robust = "✅" if b1.get("avgR", -1) > 0 and b2.get("avgR", -1) > 0 else "❌"
    if not a["n"]:
        return f"| {name} | 0 | | | | | | | | {robust} |"
    return (f"| {name} | {a['n']} | {a['win']} | {a['avgR']:+.3f} | {a['avgPct']:+.3f} | {a['pf']} | {a['ret']:+.1f} | "
            f"{a['dd']} | {f(b1)} · {f(b2)} | {robust} |")


def orb_diagnosis(tr):
    log("\n## 1) ORB neden zarar ediyor – teşhis")
    if not tr:
        log("ORB işlemi yok")
        return
    df = pd.DataFrame(tr)
    log(f"İşlem: {len(df)} · ort. {df.r.mean():+.3f}R · kazanma %{(df.r > 0).mean()*100:.0f}")
    log("\n**Çıkış türüne göre:**")
    for w, g in df.groupby("why"):
        log(f"- {w}: {len(g)} işlem (%{len(g)/len(df)*100:.0f}), ort. {g.r.mean():+.2f}R")
    st = df[df.why == "stop"]
    if len(st):
        log(f"\n**Stop yiyenler:** %{(st.bars <= 3).mean()*100:.0f}'i ilk 15 dakikada stop oldu · "
            f"%{(st.mfe >= 0.5).mean()*100:.0f}'i önce +0.5R, %{(st.mfe >= 1).mean()*100:.0f}'i önce +1R görmüştü · "
            f"medyan MFE {st.mfe.median():.2f}R")
    log(f"\n**Girişten sonra görülen en yüksek kâr (MFE):** medyan {df.mfe.median():.2f}R · "
        f"%{(df.mfe >= 2).mean()*100:.0f}'i 2R hedefe ulaştı · %{(df.mfe < 0.3).mean()*100:.0f}'i hiç 0.3R'ye çıkmadı")
    log(f"**Stop olmasa ve 15:50'ye kadar tutulsaydı:** ort. {df.hold_r.mean():+.3f}R")
    df["saat"] = pd.cut(df["min"], [0, 30, 60, 90, 120], labels=["09:45-10:00", "10:00-10:30", "10:30-11:00", "11:00-11:30"])
    log("\n**Giriş saatine göre:**")
    for w, g in df.groupby("saat", observed=True):
        log(f"- {w}: {len(g)} işlem, ort. {g.r.mean():+.2f}R")


def main():
    cands = C.CANDIDATES + (C.ADRS if C.INCLUDE_ADRS else [])
    daily = D.download_prices(cands + [C.BENCHMARK], period="3y")
    fund = D.fundamentals([t for t in cands if t in daily])
    sh = sharia.screen(fund, daily)
    top, _, _ = pick_universe(fund, set(daily), sh)
    intra = load_intraday(top + [C.BENCHMARK])
    spy_days = split_days(intra.pop(C.BENCHMARK)) if C.BENCHMARK in intra else {}
    days_by_sym, ctx_by_sym = {}, {}
    for s in top:
        if s in intra and s in daily:
            days_by_sym[s] = split_days(intra[s])
            add_rvol(days_by_sym[s])
            ctx_by_sym[s] = daily_ctx(daily[s])
            # Önceki kapanış/zirve: temettü düzeltmesi farkı olmasın diye gün içi veriden
            ds = sorted(days_by_sym[s])
            for a, b in zip(ds, ds[1:]):
                k = ctx_by_sym[s].get(b)
                if k:
                    k["pc"], k["ph"] = float(days_by_sym[s][a]["c"][-1]), float(days_by_sym[s][a]["h"].max())
    all_days = sorted({d for v in days_by_sym.values() for d in v})[12:]
    mid = all_days[len(all_days) // 2]
    log(f"Test dönemi: {all_days[0]} – {all_days[-1]} ({len(all_days)} işlem günü), {len(days_by_sym)} hisse, "
        f"yarı bölme: {mid}")
    spy_intraday = [((sd["c"][np.searchsorted(sd["m"], FLAT_MIN)] / sd["o"][0]) - 1) * 100
                    for d, sd in spy_days.items() if d >= all_days[0]]
    if spy_intraday:
        log(f"Karşılaştırma: SPY'ı her gün açılışta alıp 15:50'de satmak ort. {np.mean(spy_intraday):+.3f}%/gün")

    results = {}
    for name, fn in STRATS.items():
        results[name] = run_strategy(fn, days_by_sym, ctx_by_sym, spy_days, all_days)
    results["Göreceli güç 10:00 (kapanışa kadar)"] = [t for d in all_days
                                                      for t in rs_momentum(days_by_sym, ctx_by_sym, spy_days, d)]
    orb_diagnosis(results["ORB 15 dk (mevcut)"])

    log("\n## 2) Strateji karşılaştırması (yalnız alış, günde en fazla 3 işlem, kayma %0.05 x 2, işlem başı %1 risk)")
    log("| Strateji | İşlem | Kazanma % | Ort. R | Ort. % | Kâr faktörü | Getiri % | Maks. düşüş % | 1. yarı · 2. yarı | İki yarıda da + |")
    log("|---|---|---|---|---|---|---|---|---|---|")
    for name, tr in results.items():
        log(row(name, tr, mid))
    log("\n## 3) Çıkış türleri")
    for name, tr in results.items():
        if tr:
            w = pd.Series([t["why"] for t in tr]).value_counts()
            log(f"- {name}: " + ", ".join(f"{k} {v}" for k, v in w.items()))
    best = {n: stats(t) for n, t in results.items()}
    Path("arastirma.json").write_text(json.dumps({"stats": best, "mid": str(mid)}, default=str, ensure_ascii=False))
    if os.getenv("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write("\n".join(OUT) + "\n")


if __name__ == "__main__":
    main()
