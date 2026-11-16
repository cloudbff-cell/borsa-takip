"""Günlük işlem (ORB) geri testi – son ~60 işlem günü, 5 dakikalık Yahoo verisi.

İşlem yapmaz, secret kullanmaz. Evren: İslami filtreden geçen ilk 50 (bugünkü listeyle; hafif
hayatta kalma yanlılığı içerir). Her gün her hisse için engine/daytrade.py'deki kurallar aynen uygulanır,
giriş/çıkışta %0.05 kayma payı düşülür. Portföy kısıtları (en fazla 3 pozisyon, günde 4 giriş) gün içinde
sinyal saatine ve göreceli hacme göre uygulanır. Sonuç GitHub Actions özetine yazılır.
"""
import json
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from engine import config as C  # noqa: E402
from engine import data as D  # noqa: E402
from engine import daytrade as DT  # noqa: E402
from engine import sharia  # noqa: E402
from engine.indicators import add_indicators  # noqa: E402
from engine.run import pick_universe  # noqa: E402


def intraday(tickers):
    yf = D._yf()
    out = {}
    for i in range(0, len(tickers), 25):
        chunk = tickers[i:i + 25]
        raw = yf.download(chunk, period="60d", interval="5m", group_by="ticker", auto_adjust=False,
                          threads=True, progress=False, prepost=False)
        for t in chunk:
            try:
                df = raw[t] if isinstance(raw.columns, pd.MultiIndex) else raw
                df = df.dropna(subset=["Close"])
                df.index = pd.to_datetime(df.index).tz_convert(DT.NY)
                out[t] = df.rename(columns={"Open": "o", "High": "h", "Low": "l", "Close": "c", "Volume": "v"})[
                    ["o", "h", "l", "c", "v"]].astype(float)
            except Exception as e:  # noqa: BLE001
                print(t, e)
    return out


def run_bt(universe, daily, fund, intra, spy, **over):
    old = {k: getattr(C, k) for k in over}
    for k, v in over.items():
        setattr(C, k, v)
    try:
        days = sorted({d for t in universe if t in intra for d in intra[t].index.date})
        ind = {t: add_indicators(daily[t]) for t in universe if t in daily}
        trades = []
        for d in days[C.DT_HISTORY_DAYS:]:
            sday = spy[spy.index.date == d] if spy is not None else None
            cands = []
            for t in universe:
                if t not in intra or t not in ind:
                    continue
                ctx = DT.daily_context(ind[t], d, None)
                if not ctx:
                    continue
                ctx["or_vol_avg"] = DT.or_volume_avg(intra[t], d)
                b = intra[t][intra[t].index.date == d]
                if len(b) < 20:
                    continue
                r = DT.simulate_day(b, ctx, sday)
                if r:
                    cands.append(dict(r, t=t, d=str(d)))
            cands.sort(key=lambda r: (r["time"], -r["rvol"]))
            trades += cands[:min(C.DT_MAX_POSITIONS, C.DT_MAX_TRADES)]   # yaklaşık portföy kısıtı
        return trades
    finally:
        for k, v in old.items():
            setattr(C, k, v)


def stats(tr):
    if not tr:
        return {"n": 0}
    r = np.array([x["r"] for x in tr])
    pct = np.array([x["pct"] for x in tr])
    gains, losses = r[r > 0].sum(), -r[r < 0].sum()
    # Günlük-işlem sermayesine etkisi: işlem başına %1 risk -> R x %1
    by_day = pd.Series(r * C.DT_RISK * 100, index=[x["d"] for x in tr]).groupby(level=0).sum()
    eq = (1 + by_day / 100).cumprod()
    return {"n": len(tr), "win": round((r > 0).mean() * 100, 1), "avgR": round(r.mean(), 3),
            "totalR": round(r.sum(), 1), "avgPct": round(pct.mean(), 3),
            "pf": round(gains / losses, 2) if losses else None,
            "capRet": round((eq.iloc[-1] - 1) * 100, 2), "maxDD": round(((eq / eq.cummax()) - 1).min() * 100, 2),
            "days": len(by_day),
            "exits": {k: int(v) for k, v in pd.Series([x["why"] for x in tr]).value_counts().items()}}


def main():
    cands = C.CANDIDATES + (C.ADRS if C.INCLUDE_ADRS else [])
    daily = D.download_prices(cands + [C.BENCHMARK], period="3y")
    fund = D.fundamentals([t for t in cands if t in daily])
    sh = sharia.screen(fund, daily)
    top, _, _ = pick_universe(fund, set(daily), sh)
    excluded = {t: r["reason"] for t, r in sh.items() if not r["ok"]}
    print("İslami filtre:", len(top), "hisse seçildi;", len(excluded), "elendi")
    intra = intraday(top + [C.BENCHMARK])
    spy = intra.pop(C.BENCHMARK, None)
    base = run_bt(top, daily, fund, intra, spy)
    rows = [("Varsayılan", stats(base))]
    for name, over in [("RVOL ≥ 1.0", {"DT_RVOL_MIN": 1.0}), ("RVOL ≥ 2.0", {"DT_RVOL_MIN": 2.0}),
                       ("Hedef 1.5R", {"DT_TARGET_R": 1.5}), ("Hedef 3R", {"DT_TARGET_R": 3.0}),
                       ("Trend filtresi yok", {"__notrend": True})]:
        if "__notrend" in over:
            orig = DT.daily_context
            DT.daily_context = lambda *a, **k: (lambda c: dict(c, trend_ok=True) if c else c)(orig(*a, **k))
            rows.append((name, stats(run_bt(top, daily, fund, intra, spy))))
            DT.daily_context = orig
        else:
            rows.append((name, stats(run_bt(top, daily, fund, intra, spy, **over))))
    lines = ["## Günlük işlem (ORB) geri testi – son ~60 işlem günü, 5 dk veri",
             f"Evren: İslami filtreden geçen ilk {len(top)} hisse · kayma payı %0.05 x 2 · işlem başı risk %{C.DT_RISK*100:g}",
             "", "| Ayar | İşlem | Kazanma % | Ort. R | Toplam R | Ort. % | Kâr faktörü | Sermaye getirisi % | Maks. düşüş % | Çıkışlar |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for name, s in rows:
        if not s["n"]:
            lines.append(f"| {name} | 0 | | | | | | | | |")
            continue
        lines.append(f"| {name} | {s['n']} | {s['win']} | {s['avgR']} | {s['totalR']} | {s['avgPct']} | {s['pf']} | "
                     f"{s['capRet']} | {s['maxDD']} | {s['exits']} |")
    lines += ["", "Elenen büyük şirketler (ilk 25): " + ", ".join(
        f"{t} ({excluded[t]})" for t in sorted(excluded, key=lambda t: -(fund.get(t, {}).get('marketCap') or 0))[:25])]
    txt = "\n".join(lines)
    print(txt)
    if os.getenv("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write(txt + "\n")
    print("::notice title=ORB geri testi::" + json.dumps(rows[0][1], ensure_ascii=False))


if __name__ == "__main__":
    main()
