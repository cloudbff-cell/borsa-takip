"""Çıkış yöntemi karşılaştırması (geriye dönük test; işlem yapmaz, secret kullanmaz).

Giriş (canlı sistemin yaklaşık hali, günlük barlarla):
  a) Kapanışta herhangi bir AL sinyali  -> ertesi gün açılıştan alım
  b) Yükseliş trendi + teknik/göreceli puan >= 45/60 ve fiyat 20 EMA + 0.5 ATR bölgesine iner -> o seviyeden alım
Her hissede aynı anda tek pozisyon. İlk stop = giriş - 2 ATR. Tüm yöntemlerde kapanışta teyitli
SAT sinyalleri (destek kırılımı, trend bozulması, ölüm kesişimi) de çıkış sebebidir. En uzun tutma 120 gün.
Gün içinde hem stop hem hedef görülürse önce stop varsayılır (temkinli).
"""
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from engine import config as C  # noqa: E402
from engine import data as D  # noqa: E402
from engine.indicators import add_indicators  # noqa: E402
from engine.signals import compute_signals  # noqa: E402
from tools.olcum import score_series  # noqa: E402

METHODS = {
    "mevcut (başa baş @2ATR, kâr al @4ATR)": dict(be=2.0, tp=4.0, trail=None),
    "iz süren 2.0 ATR": dict(be=None, tp=None, trail=2.0),
    "iz süren 2.5 ATR": dict(be=None, tp=None, trail=2.5),
    "iz süren 3.0 ATR": dict(be=None, tp=None, trail=3.0),
    "iz süren 3.5 ATR": dict(be=None, tp=None, trail=3.5),
    "iz süren 4.0 ATR": dict(be=None, tp=None, trail=4.0),
    "karma: iz süren 3 ATR + kâr al @6ATR": dict(be=None, tp=6.0, trail=3.0),
    "iz süren 3.0 ATR, SAT sinyali çıkışı YOK": dict(be=None, tp=None, trail=3.0, nosig=True),
    "iz süren 4.0 ATR, SAT sinyali çıkışı YOK": dict(be=None, tp=None, trail=4.0, nosig=True),
    "mevcut, SAT sinyali çıkışı YOK": dict(be=2.0, tp=4.0, trail=None, nosig=True),
}
EXIT_CODES = ["S_BREAKDOWN", "S_TREND", "S_DEATH"]


def simulate(df, sig, score, up, m):
    o, h, l, c, a = (df[k].values for k in ("Open", "High", "Low", "Close", "atr"))
    e20 = df["ema20"].values
    buy = sig[[k for k in sig.columns if k.startswith("B_")]].any(axis=1).values
    sell = sig[EXIT_CODES].any(axis=1).values
    sc = score.values
    upv = up.values
    n = len(df)
    trades = []
    i = C.EMA_SLOW + 1
    while i < n - 1:
        entry = None
        if buy[i - 1]:
            entry, kind = o[i], "sinyal"
        elif upv[i - 1] and sc[i - 1] >= 45:
            zone = e20[i - 1] + 0.5 * a[i - 1]
            if l[i] <= zone:
                entry, kind = min(o[i], zone), "trend"
        if entry is None or np.isnan(a[i - 1]):
            i += 1
            continue
        atr0 = a[i - 1]
        stop0 = entry - 2 * atr0
        stop, hi = stop0, entry
        tp = entry + m["tp"] * atr0 if m["tp"] else None
        exit_px, j = None, i
        for j in range(i, min(i + 120, n)):
            # gün içi: önce stop, sonra hedef
            if l[j] <= stop:
                exit_px = min(o[j], stop) if j > i else stop
                break
            if tp and h[j] >= tp:
                exit_px = max(o[j], tp) if j > i else tp
                break
            hi = max(hi, h[j])
            if m["be"] and hi >= entry + m["be"] * atr0:
                stop = max(stop, entry)
            if m["trail"]:
                stop = max(stop, hi - m["trail"] * a[j])
            if sell[j] and not m.get("nosig"):
                exit_px = c[j]
                break
        if exit_px is None:
            exit_px = c[j]
        r = exit_px / entry - 1
        trades.append({"ret": r, "R": (exit_px - entry) / (entry - stop0), "days": j - i + 1, "kind": kind})
        i = j + 1
    return trades


def summarize(tr):
    if not tr:
        return {}
    r = np.array([t["ret"] for t in tr]) * 100
    R = np.array([t["R"] for t in tr])
    d = np.array([t["days"] for t in tr])
    return {"islem": len(tr), "kazanan%": round(float((r > 0).mean() * 100), 1),
            "ort_getiri%": round(float(r.mean()), 2), "medyan%": round(float(np.median(r)), 2),
            "ort_R": round(float(R.mean()), 2), "toplam_R": round(float(R.sum()), 0),
            "en_iyi%": round(float(r.max()), 1), "en_kotu%": round(float(r.min()), 1),
            "ort_gun": round(float(d.mean()), 1),
            "kazanc/kayip": round(float(r[r > 0].mean() / -r[r <= 0].mean()), 2) if (r <= 0).any() and (r > 0).any() else None}


def main():
    tick = C.CANDIDATES
    prices = D.download_prices(tick + [C.BENCHMARK], period="3y")
    bench = prices[C.BENCHMARK]["Close"]
    data = []
    for t in tick:
        if t not in prices or len(prices[t]) < 300:
            continue
        df = add_indicators(prices[t])
        sig = compute_signals(df)
        s, up = score_series(df, bench)
        data.append((df, sig, s.fillna(0), up.fillna(False)))
    res = {}
    for name, m in METHODS.items():
        allt = [t for d in data for t in simulate(*d, m)]
        res[name] = summarize(allt)
        res[name]["sadece_sinyal_girisleri"] = summarize([t for t in allt if t["kind"] == "sinyal"])
    print("::notice title=StopTest::" + json.dumps(res, ensure_ascii=False))
    print(json.dumps(res, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
