"""SAT sinyali çıkışlarının ayrıntılı testi – 10 yıl (2018 Q4, 2020 çöküşü, 2022 ayı piyasası dahil).
Tüm yöntemlerde: iz süren stop 3.5 ATR, sabit kâr al yok, ilk stop giriş - 2 ATR. İşlem yapmaz.
"""
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from engine import config as C  # noqa: E402
from engine import data as D  # noqa: E402
from engine.indicators import add_indicators, ema  # noqa: E402
from engine.signals import compute_signals  # noqa: E402
from tools.olcum import score_series  # noqa: E402

TRAIL = 3.5
VARIANTS = {
    "Tüm SAT çıkışları": dict(codes=["S_BREAKDOWN", "S_TREND", "S_DEATH"]),
    "SAT çıkışı yok": dict(codes=[]),
    "Sadece ölüm kesişimi": dict(codes=["S_DEATH"]),
    "Sadece trend bozulması": dict(codes=["S_TREND"]),
    "Sadece destek kırılımı": dict(codes=["S_BREAKDOWN"]),
    "Ölüm kesişimi + destek kırılımı": dict(codes=["S_DEATH", "S_BREAKDOWN"]),
    "Ölüm k. + piyasa filtresi": dict(codes=["S_DEATH"], regime=True),
    "Tüm SAT + piyasa filtresi": dict(codes=["S_BREAKDOWN", "S_TREND", "S_DEATH"], regime=True),
    "SAT yok + piyasa filtresi": dict(codes=[], regime=True),
    "Ölüm k. + piyasa filtresi + piyasa çıkışı": dict(codes=["S_DEATH"], regime=True, regime_exit=True),
}
# piyasa filtresi: SPY 200 günlük ortalamanın altındaysa yeni alım yok
# piyasa çıkışı: SPY kapanışta 200 günlük ortalamanın %3 altına inerse tüm pozisyonlar kapanır


def simulate(df, sig, score, up, spy_ok, spy_crash, v):
    o, h, l, c, a = (df[k].values for k in ("Open", "High", "Low", "Close", "atr"))
    e20 = df["ema20"].values
    idx = df.index
    buy = sig[[k for k in sig.columns if k.startswith("B_")]].any(axis=1).values
    sell = sig[v["codes"]].any(axis=1).values if v["codes"] else np.zeros(len(df), bool)
    sc, upv = score.values, up.values
    ok = spy_ok.reindex(idx).fillna(True).values
    crash = spy_crash.reindex(idx).fillna(False).values
    n, trades, i = len(df), [], C.EMA_SLOW + 1
    while i < n - 1:
        entry = None
        if v.get("regime") and not ok[i - 1]:
            i += 1
            continue
        if buy[i - 1]:
            entry = o[i]
        elif upv[i - 1] and sc[i - 1] >= 45:
            zone = e20[i - 1] + 0.5 * a[i - 1]
            if l[i] <= zone:
                entry = min(o[i], zone)
        if entry is None or np.isnan(a[i - 1]):
            i += 1
            continue
        stop0 = entry - 2 * a[i - 1]
        stop, hi, exit_px, j = stop0, entry, None, i
        for j in range(i, min(i + 250, n)):
            if l[j] <= stop:
                exit_px = min(o[j], stop) if j > i else stop
                break
            hi = max(hi, h[j])
            stop = max(stop, hi - TRAIL * a[j])
            if sell[j] or (v.get("regime_exit") and crash[j]):
                exit_px = c[j]
                break
        if exit_px is None:
            exit_px = c[j]
        trades.append({"R": (exit_px - entry) / (entry - stop0), "ret": exit_px / entry - 1,
                       "in": idx[i], "out": idx[j]})
        i = j + 1
    return trades


def stats(tr):
    if not tr:
        return None
    R = np.array([t["R"] for t in tr]); r = np.array([t["ret"] for t in tr]) * 100
    return {"n": len(tr), "win": round(float((r > 0).mean() * 100), 1), "avg%": round(float(r.mean()), 2),
            "R": round(float(R.mean()), 2), "p5%": round(float(np.percentile(r, 5)), 1)}


def max_dd_R(tr):
    """Tüm işlemler kapanış sırasına göre art arda yapılsaydı, toplam R eğrisindeki en büyük düşüş."""
    s = pd.Series([t["R"] for t in tr], index=[t["out"] for t in tr]).sort_index().cumsum()
    return round(float((s.cummax() - s).max()), 1) if len(s) else None


def main():
    prices = D.download_prices(C.CANDIDATES + [C.BENCHMARK], period="10y")
    spy = prices[C.BENCHMARK]["Close"]
    e200 = ema(spy, 200)
    spy_ok, spy_crash = spy > e200, spy < e200 * 0.97
    data = []
    for t in C.CANDIDATES:
        if t in prices and len(prices[t]) > 300:
            df = add_indicators(prices[t])
            s, up = score_series(df, spy)
            data.append((df, compute_signals(df), s.fillna(0), up.fillna(False)))
    periods = {"2018Q4": ("2018-10-01", "2018-12-31"), "2020cokus": ("2020-02-15", "2020-04-15"),
               "2022": ("2022-01-01", "2022-12-31"), "2025nis": ("2025-02-15", "2025-05-15")}
    for name, v in VARIANTS.items():
        tr = [t for d in data for t in simulate(*d, spy_ok, spy_crash, v)]
        out = {"tum": stats(tr), "maxDD_R": max_dd_R(tr), "yil": f"{tr[0]['in'].year if tr else ''}-"}
        for pn, (a, b) in periods.items():
            sub = [t for t in tr if pd.Timestamp(a) <= t["in"] <= pd.Timestamp(b)]
            out[pn] = stats(sub)
            # o dönemde açık olan pozisyonların dönem içi sonucu (kaba): dönemde kapanan işlemlerin toplam R'si
            cl = [t["R"] for t in tr if pd.Timestamp(a) <= t["out"] <= pd.Timestamp(b)]
            out[pn + "_kapananR"] = round(float(np.sum(cl)), 1) if cl else 0
        print(f"::notice title=EX {name}::" + json.dumps(out, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
