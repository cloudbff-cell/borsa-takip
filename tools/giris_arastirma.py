"""Gün aşırı (1-10 gün) ALIM kuralı araştırması – 10 yıl günlük veri (işlem yapmaz).

Her strateji: yalnız alış, kapanışta giriş (MOC), kendi çıkış kuralı; bazılarında -%2 stop varyantı.
Portföy: en fazla 5 pozisyon x %20, aynı gün çok sinyal varsa stratejinin sıralama ölçütüyle seçilir.
Kayma: giriş ve çıkışta %0.05. Evren: İslami filtreden geçen ilk 50 (bugünkü liste – hayatta kalma
yanlılığı sonuçları biraz iyimser gösterir). Sağlamlık için 3 alt dönem ayrı raporlanır.
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
from engine.indicators import add_indicators, rsi  # noqa: E402
from engine.run import pick_universe  # noqa: E402
from engine.signals import compute_signals  # noqa: E402
from tools.entry_test import season_bonus, volume_score  # noqa: E402
from tools.olcum import score_series  # noqa: E402
from tools.yuzde_cikis_test import K, entries as bot_entries, simulate as bot_sim  # noqa: E402

SLIP = 0.0005
OUT = []
PERIODS = [("2017-2019", "2017-01-01", "2019-12-31"), ("2020-2022", "2020-01-01", "2022-12-31"),
           ("2023-2026", "2023-01-01", "2026-12-31")]
T_CLOSE, T_OPEN = pd.Timedelta(hours=16), pd.Timedelta(hours=9, minutes=30)


def log(s=""):
    print(s)
    OUT.append(s)


def prep(df):
    d = add_indicators(df)
    c = d["Close"]
    d["sma5"] = c.rolling(5).mean()
    d["sma10"] = c.rolling(10).mean()
    d["sma20"] = c.rolling(20).mean()
    d["sma200"] = c.rolling(200).mean()
    d["rsi2"] = rsi(c, 2)
    d["ibs"] = (c - d["Low"]) / (d["High"] - d["Low"]).replace(0, np.nan)
    d["bbl"] = d["sma20"] - 2 * c.rolling(20).std()
    d["hi50"] = d["High"].rolling(50).max()
    d["lo10"] = d["Low"].rolling(10).min().shift(1)
    d["down3"] = (c < c.shift(1)) & (c.shift(1) < c.shift(2)) & (c.shift(2) < c.shift(3))
    return d


def run_exit(d, i, exit_fn, max_days, stop_pct=None):
    """Giriş i günü kapanışı. exit_fn(d, j) True ise j kapanışında çık."""
    o, l, c = d["Open"].to_numpy(), d["Low"].to_numpy(), d["Close"].to_numpy()
    e = c[i] * (1 + SLIP)
    n = len(d)
    for j in range(i + 1, min(i + 1 + max_days, n)):
        if stop_pct and l[j] <= e * (1 - stop_pct):
            px = min(o[j], e * (1 - stop_pct))
            return j, px, T_OPEN if o[j] <= e * (1 - stop_pct) else pd.Timedelta(hours=12), "stop"
        if exit_fn(d, j):
            return j, c[j], T_CLOSE, "kural"
    j = min(i + max_days, n - 1)
    return j, c[j], T_CLOSE, "süre"


def strategy_trades(sym, d, name, stop_pct=None):
    cols = {k: d[k].to_numpy() for k in ("Open", "High", "Low", "Close", "sma5", "sma10", "sma20", "sma200", "rsi2",
                                         "ibs", "bbl", "hi50", "lo10", "down3", "ema20", "atr")}
    c, s200 = cols["Close"], cols["sma200"]
    up = c > s200
    idx = d.index
    trades, busy = [], -1
    for i in range(210, len(d) - 1):
        if i <= busy:
            continue
        sig, score = False, 0.0
        if name == "rsi2":
            sig, score = up[i] and cols["rsi2"][i] < 10, -cols["rsi2"][i]
            ex = lambda d_, j: cols["Close"][j] > cols["sma5"][j]
            md = 10
        elif name == "down3":
            sig, score = up[i] and bool(cols["down3"][i]), -(c[i] / c[i - 3] - 1)
            ex = lambda d_, j: cols["Close"][j] > cols["High"][j - 1]
            md = 10
        elif name == "ibs":
            sig, score = up[i] and cols["ibs"][i] < 0.15 and c[i] < cols["sma10"][i], -cols["ibs"][i]
            ex = lambda d_, j: cols["ibs"][j] > 0.7 or cols["Close"][j] > cols["sma5"][j]
            md = 10
        elif name == "bb":
            sig, score = up[i] and c[i] < cols["bbl"][i], (cols["bbl"][i] - c[i]) / cols["atr"][i]
            ex = lambda d_, j: cols["Close"][j] > cols["sma20"][j]
            md = 15
        elif name == "breakout":
            sig = up[i] and c[i] >= cols["hi50"][i] * 0.999 and c[i - 1] < cols["hi50"][i - 1] * 0.999
            score = c[i] / c[i - 63] - 1 if i > 63 else 0
            ex = lambda d_, j: cols["Close"][j] < cols["lo10"][j]
            md = 120
        elif name == "overnight":
            sig, score = up[i] and cols["ibs"][i] > 0.8 and c[i] > c[i - 1], cols["ibs"][i]
        if not sig or np.isnan(score):
            continue
        e = c[i] * (1 + SLIP)
        if name == "overnight":
            j, px, tt, why = i + 1, cols["Open"][i + 1], T_OPEN, "açılış"
        else:
            j, px, tt, why = run_exit(d, i, ex, md, stop_pct)
        trades.append({"in": idx[i] + T_CLOSE, "out": idx[j] + tt, "ret": px * (1 - SLIP) / e - 1, "score": score,
                       "why": why, "days": j - i, "s": sym})
        busy = j if name != "overnight" else i
    return trades


def portfolio(trades, slots=5):
    ev = sorted(trades, key=lambda t: (t["in"], -t["score"]))
    open_, eq, curve, taken = [], 1.0, [(ev[0]["in"], 1.0)] if ev else [], []
    for t in ev:
        for o in sorted([o for o in open_ if o["out"] <= t["in"]], key=lambda o: o["out"]):
            eq += o["alloc"] * o["ret"]
            curve.append((o["out"], eq))
            open_.remove(o)
        if len(open_) < slots and not any(o["s"] == t["s"] for o in open_):
            t = dict(t, alloc=eq / slots)
            open_.append(t)
            taken.append(t)
    for o in sorted(open_, key=lambda o: o["out"]):
        eq += o["alloc"] * o["ret"]
        curve.append((o["out"], eq))
    return taken, pd.Series([e for _, e in curve], index=[x for x, _ in curve]).sort_index()


def cagr(eq, a, b):
    s = eq[(eq.index >= pd.Timestamp(a)) & (eq.index <= pd.Timestamp(b) + pd.Timedelta(days=1))]
    if len(s) < 2:
        return None
    base = eq[eq.index < pd.Timestamp(a)]
    v0 = base.iloc[-1] if len(base) else s.iloc[0]
    yrs = max((s.index[-1] - pd.Timestamp(a)).days / 365.25, 0.25)
    return ((s.iloc[-1] / v0) ** (1 / yrs) - 1) * 100


def row(name, trades, spy_cagr):
    taken, eq = portfolio(trades)
    if not taken:
        return f"| {name} | 0 |"
    r = np.array([t["ret"] for t in taken]) * 100
    yrs = (eq.index[-1] - eq.index[0]).days / 365.25
    tot = ((eq.iloc[-1]) ** (1 / yrs) - 1) * 100
    dd = ((eq / eq.cummax()) - 1).min() * 100
    sub = [cagr(eq, a, b) for _, a, b in PERIODS]
    beat = sum(1 for s, sp in zip(sub, spy_cagr) if s is not None and s > sp)
    return (f"| {name} | {len(taken)} | {len(taken)/yrs:.0f} | {(r > 0).mean()*100:.0f} | {r.mean():+.2f} | "
            f"{np.mean([t['days'] for t in taken]):.1f} | **{tot:+.1f}** | {dd:.1f} | "
            + " | ".join("–" if s is None else f"{s:+.1f}" for s in sub) + f" | {beat}/3 |")


def main():
    cands = C.CANDIDATES + (C.ADRS if C.INCLUDE_ADRS else [])
    prices = D.download_prices(cands + [C.BENCHMARK], period="10y")
    fund = D.fundamentals([t for t in cands if t in prices])
    sh = sharia.screen(fund, {t: df.tail(756) for t, df in prices.items()})
    top, _, _ = pick_universe(fund, set(prices), sh)
    spy = prices[C.BENCHMARK]["Close"]
    data = {t: prep(prices[t]) for t in top if t in prices and len(prices[t]) > 400}
    start = spy.index[210]
    spy_eq = spy.loc[start:] / spy.loc[start]
    spy_eq.index = spy_eq.index + T_CLOSE
    spy_sub = [cagr(spy_eq, a, b) for _, a, b in PERIODS]
    yrs = (spy.index[-1] - start).days / 365.25
    log(f"## Gün aşırı ALIM kuralı araştırması ({start:%Y-%m} – {spy.index[-1]:%Y-%m}, {yrs:.1f} yıl)")
    log(f"Evren: İslami filtreden geçen {len(data)} hisse · portföy 5 x %20 · kapanışta giriş · kayma %0.05 x 2")
    hdr = ("| Strateji | İşlem | Yılda | Kazanma % | Ort. % | Ort. gün | Yıllık getiri % | Maks. düşüş % | "
           + " | ".join(p[0] for p in PERIODS) + " | SPY'ı geçtiği dönem |")
    log("")
    log(hdr)
    log("|" + "---|" * (9 + len(PERIODS)))
    spy_dd = ((spy_eq / spy_eq.cummax()) - 1).min() * 100
    log(f"| SPY al-tut | – | – | – | – | – | **{(spy_eq.iloc[-1] ** (1 / yrs) - 1) * 100:+.1f}** | {spy_dd:.1f} | "
        + " | ".join(f"{s:+.1f}" for s in spy_sub) + " | – |")
    # mevcut bot
    spy_s = spy
    bot = []
    for t, d in data.items():
        tr_score, up = score_series(d, spy_s)
        total = tr_score.fillna(0) + volume_score(d) + K + season_bonus(d, spy_s)
        ent, sell = bot_entries(d, compute_signals(d), total, up.fillna(False))
        for x in bot_sim(d, ent, sell, {"mode": "atr"}):
            bot.append(dict(x, s=t, **{"in": x["in"] + T_OPEN, "out": x["out"] + T_CLOSE}))
    log(row("Mevcut bot (giriş + ATR çıkış)", bot, spy_sub))
    strategies = [
        ("RSI(2) < 10 aşırı satım, 200 gün üstü → 5 gün ort. üstünde sat", "rsi2", None),
        ("  aynısı + -%2 stop", "rsi2", 0.02),
        ("3 gün üst üste düşüş, 200 gün üstü → dünkü zirve üstünde sat", "down3", None),
        ("  aynısı + -%2 stop", "down3", 0.02),
        ("Gün dibinde kapanış (IBS < 0.15), 200 gün üstü", "ibs", None),
        ("  aynısı + -%2 stop", "ibs", 0.02),
        ("Bollinger alt bandı altında, 200 gün üstü → 20 gün ort. üstünde sat", "bb", None),
        ("50 günlük zirve kırılımı → 10 günlük dip kırılınca sat (trend takibi)", "breakout", None),
        ("Gece tutma: güçlü kapanış al, ertesi açılışta sat", "overnight", None),
    ]
    res = {}
    for label, name, sp in strategies:
        tr = [x for t, d in data.items() for x in strategy_trades(t, d, name, sp)]
        res[label] = tr
        log(row(label, tr, spy_sub))
    log("")
    log("Not: 'Yıllık getiri' portföyün tamamı içindir; nakitte beklenen günler dahil. "
        "Alt dönem sütunları o dönemdeki yıllık getiridir.")
    if os.getenv("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write("\n".join(OUT) + "\n")


if __name__ == "__main__":
    main()
