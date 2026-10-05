"""Ağ olmadan uçtan uca test: sentetik fiyat ve temel verilerle motoru çalıştırır."""
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from engine import config as C  # noqa: E402
from engine import run  # noqa: E402

SECTORS = list(C.SECTOR_TR)


def fake_prices(seed, days=2800):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range(end=pd.Timestamp(date.today()), periods=days)
    drift = rng.normal(0.0004, 0.0004)
    regime = np.sin(np.arange(days) / rng.uniform(60, 200)) * 0.0015
    seas = np.array([0.001 if d.month in (seed % 12 + 1, (seed + 3) % 12 + 1) else 0 for d in idx])
    r = drift + regime + seas + rng.normal(0, 0.017, days)
    c = 100 * np.exp(np.cumsum(r))
    h = c * (1 + np.abs(rng.normal(0, 0.008, days)))
    l = c * (1 - np.abs(rng.normal(0, 0.008, days)))
    o = (h + l) / 2
    v = rng.lognormal(16, 0.4, days) * (1 + 2 * (np.abs(r) > 0.03))
    return pd.DataFrame({"Open": o, "High": h, "Low": l, "Close": c, "Volume": v}, index=idx)


def main():
    tick = C.CANDIDATES + [C.BENCHMARK]
    full = {t: fake_prices(i) for i, t in enumerate(tick)}
    prices = {t: df.tail(756) for t, df in full.items()}
    monthly = {t: df.resample("ME").last() for t, df in full.items()}
    rng = np.random.default_rng(1)
    fund = {}
    for i, t in enumerate(C.CANDIDATES):
        p = float(prices[t]["Close"].iloc[-1])
        ed = date.today() + timedelta(days=int(rng.integers(-20, 60)))
        fund[t] = {"ticker": t, "shortName": f"{t} Inc.", "sector": SECTORS[i % len(SECTORS)],
                   "marketCap": float(rng.lognormal(26, 1)), "trailingPE": 30.0, "forwardPE": 25.0,
                   "revenueGrowth": float(rng.normal(.1, .1)), "earningsGrowth": float(rng.normal(.12, .15)),
                   "profitMargins": .22, "targetMeanPrice": p * float(rng.normal(1.1, .1)),
                   "numberOfAnalystOpinions": 30, "recommendationKey": "buy", "recommendationMean": 1.9,
                   "nextEarnings": ed.isoformat() if ed >= date.today() else None, "epsEstNext": 1.23,
                   "epsEstLow": 1.1, "epsEstHigh": 1.4, "revEstNext": 2.5e10,
                   "earningsHistory": [{"date": "2026-07-25", "est": 1.0, "actual": 1.08, "surprise": 8.0},
                                       {"date": "2026-04-25", "est": 1.0, "actual": 0.97, "surprise": -3.0},
                                       {"date": "2026-01-25", "est": .9, "actual": .95, "surprise": 5.5},
                                       {"date": "2025-10-25", "est": .85, "actual": .9, "surprise": 5.9}]}
    now = datetime.now(run.NY).replace(hour=16, minute=30)
    summary, by = run.build("post", now, prices=prices, fund=fund, monthly=monthly)
    assert len(summary["top"]) == C.TOP_N
    print("İzlenen:", len(summary["stocks"]), "| Adaylar:", summary["picks"])
    print("Sinyal istatistikleri:", summary["stats"])
    state = {"sent": {}}
    msgs = run.alerts(summary, by, "post", state)
    print(f"{len(msgs)} sinyal mesajı\n")
    print(run.plan_message(summary, by)[:2500])
    print()
    print(msgs[0] if msgs else "sinyal yok")
    print(run.close_message(summary, by))


if __name__ == "__main__":
    main()
