"""Bilanço / beklenti puanı ve mevsimsellik analizi."""
from datetime import date

import numpy as np
import pandas as pd

from . import config as C

AY = ["Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran", "Temmuz", "Ağustos", "Eylül", "Ekim", "Kasım", "Aralık"]


def _pct(x):
    return None if x is None else x * 100


def fundamental_view(f: dict, price: float) -> dict:
    """0-25 arası temel puan + okunabilir notlar."""
    sc, notes = 0, []
    hist = [h for h in (f.get("earningsHistory") or []) if h.get("est") is not None][:4]
    if hist:
        beats = sum(1 for h in hist if h["actual"] > h["est"])
        sur = [h["surprise"] for h in hist if h.get("surprise") is not None]
        sc += beats * 2
        avg_s = float(np.mean(sur)) if sur else None
        notes.append(f"Son {len(hist)} çeyrekte {beats} kez beklentiyi aştı"
                     + (f" (ort. sürpriz {avg_s:+.1f}%)" if avg_s is not None else ""))
    rg, eg = f.get("revenueGrowth"), f.get("earningsGrowth")
    if rg is not None:
        sc += 5 if rg > .20 else 4 if rg > .10 else 2 if rg > .05 else 1 if rg > 0 else 0
        notes.append(f"Gelir büyümesi (yıllık) {rg*100:+.1f}%")
    if eg is not None:
        sc += 4 if eg > .20 else 3 if eg > .10 else 1 if eg > 0 else 0
        notes.append(f"Kâr büyümesi (yıllık) {eg*100:+.1f}%")
    tgt = f.get("targetMeanPrice")
    upside = (tgt / price - 1) if (tgt and price) else None
    if upside is not None:
        sc += 5 if upside > .20 else 4 if upside > .10 else 2 if upside > .05 else 1 if upside > 0 else 0
        n = f.get("numberOfAnalystOpinions")
        notes.append(f"Analist ort. hedef ${tgt:,.2f} ({upside*100:+.1f}%)" + (f", {n} analist" if n else ""))
    tpe, fpe = f.get("trailingPE"), f.get("forwardPE")
    if tpe and fpe and 0 < fpe < tpe:
        sc += 2
        notes.append(f"İleriye dönük F/K {fpe:.1f} < cari F/K {tpe:.1f} (kâr artışı bekleniyor)")
    elif fpe:
        notes.append(f"İleriye dönük F/K {fpe:.1f}")
    if (f.get("profitMargins") or 0) > .20:
        sc += 1
    nxt = f.get("nextEarnings")
    days_to = None
    if nxt:
        try:
            days_to = (date.fromisoformat(nxt[:10]) - date.today()).days
        except ValueError:
            pass
    exp = None
    if f.get("epsEstNext") is not None:
        exp = f"Sonraki çeyrek HBK beklentisi ${f['epsEstNext']:.2f}"
        if f.get("epsEstLow") is not None and f.get("epsEstHigh") is not None:
            exp += f" (aralık ${f['epsEstLow']:.2f}–${f['epsEstHigh']:.2f})"
        if f.get("revEstNext"):
            exp += f", gelir beklentisi ${f['revEstNext']/1e9:,.1f} milyar"
    return {"score": int(min(sc, 25)), "notes": notes, "upside": _pct(upside),
            "nextEarnings": nxt, "daysToEarnings": days_to, "expectation": exp,
            "recommendation": f.get("recommendationKey"), "recMean": f.get("recommendationMean"),
            "history": f.get("earningsHistory") or []}


def monthly_returns(df: pd.DataFrame) -> pd.Series:
    m = df["Close"].resample("ME").last()
    return m.pct_change().dropna()


def seasonality(df: pd.DataFrame, bench: pd.DataFrame | None, today: date | None = None) -> dict:
    today = today or date.today()
    r = monthly_returns(df)
    r = r[r.index.year >= today.year - C.SEASONAL_YEARS]
    r = r[~((r.index.year == today.year) & (r.index.month == today.month))]  # yarım ayı çıkar
    rb = monthly_returns(bench) if bench is not None else None
    table = []
    for m in range(1, 13):
        x = r[r.index.month == m]
        if len(x) < 4:
            table.append(None)
            continue
        ex = None
        if rb is not None:
            xb = rb.reindex(x.index)
            ex = float((x - xb).mean() * 100)
        table.append({"avg": round(float(x.mean() * 100), 2), "win": round(float((x > 0).mean() * 100), 0),
                      "n": int(len(x)), "excess": None if ex is None else round(ex, 2)})

    def score(cell):
        if not cell:
            return 0.0
        ex = cell["excess"] if cell["excess"] is not None else cell["avg"]
        return float(np.clip((cell["win"] - 50) / 30, 0, 1) * 10 + np.clip(ex / 2, 0, 1) * 5)

    cur, nxt = today.month - 1, today.month % 12
    return {"table": table, "cur": table[cur], "next": table[nxt], "curName": AY[cur], "nextName": AY[nxt],
            "score": round(score(table[cur]), 1), "nextScore": round(score(table[nxt]), 1)}
