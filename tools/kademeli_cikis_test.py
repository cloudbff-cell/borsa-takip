"""Kademeli kâr koruma çıkışı – 5 dakikalık veriyle test (işlem yapmaz).

Kural (kullanıcı önerisi):
  1) Başlangıç stopu: giriş -%2.
  2) Fiyat +%1'e ulaşınca 30 dakika izlenir: 30 dk sonunda fiyat +%1'in üstündeyse pozisyon tutulur,
     değilse satılır.
  3) +%1 görüldükten sonra kâr koruma: en yüksek kâr p ise çıkış seviyesi = giriş x (1 + p - max(%1, p/2)).
     Örn. p=%1.5 -> +%0.5'te çık; p=%10 -> +%5'te çık.
  4) Trend bozulursa çık (günlük kapanışta SAT sinyali; varyant: kapanış 20 EMA altında).
Giriş: botun mevcut giriş kuralları (değişmedi). Evren: İslami filtreden geçen ilk 50.
Veri: Alpaca SIP 5 dk (son ~3 yıl) + Yahoo günlük (göstergeler). Kayma giriş/çıkış %0.05.
"""
import os
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from engine import config as C  # noqa: E402
from engine import data as D  # noqa: E402
from engine import sharia, trader  # noqa: E402
from engine.indicators import add_indicators  # noqa: E402
from engine.run import pick_universe  # noqa: E402
from engine.signals import compute_signals  # noqa: E402
from tools.entry_test import season_bonus, volume_score  # noqa: E402
from tools.olcum import score_series  # noqa: E402
from tools.yuzde_cikis_test import K, entries, portfolio  # noqa: E402

SLIP = 0.0005
YEARS = float(os.getenv("YEARS", "3"))
OUT = []


def log(s=""):
    print(s)
    OUT.append(s)


def giveback_level(entry, peak, min_gb=0.01, frac=0.5):
    p = peak / entry - 1
    return entry * (1 + p - max(min_gb, frac * p))


VARIANTS = {
    "Öneri: -%2 stop, %1'de 30 dk teyit, kâr koruma, SAT sinyalinde çık": dict(trend="sat"),
    "Öneri, trend bozulması = kapanış 20 EMA altı": dict(trend="ema20"),
    "Öneri, 30 dk teyit yok (doğrudan kâr koruma)": dict(trend="sat", confirm=False),
    "Basit: -%2 stop / +%1 kâr al": dict(simple_tp=0.01),
    "Mevcut: 2 ATR stop, 3 ATR iz süren, SAT çıkışı (5 dk ile)": dict(atr=True),
}


def load_intraday(symbols, start, end):
    api = trader.Alpaca()
    out = {}
    for i in range(0, len(symbols), 4):
        chunk = symbols[i:i + 4]
        raw = api.bars(chunk, start=start.isoformat(), end=end.isoformat(), feed="sip", adjustment="all")
        for s, rows in raw.items():
            df = pd.DataFrame(rows)
            df.index = pd.to_datetime(df["t"], utc=True).dt.tz_convert("America/New_York")
            mins = df.index.hour * 60 + df.index.minute
            df = df[(mins >= 570) & (mins < 960)]
            out[s] = df[["o", "h", "l", "c"]].astype(float)
        time.sleep(0.5)
    return out


def simulate(sym, d, ent, sell, bars, v):
    """d: günlük veri (göstergelerle), ent: {gün indeksi: (giriş, puan)}, bars: 5 dk mumlar."""
    trades = []
    days = d.index
    bday = bars.index.normalize().tz_localize(None)
    o5, h5, l5, c5 = (bars[k].to_numpy() for k in "ohlc")
    starts = {day: np.searchsorted(bday, day) for day in days}
    day_of_bar = bday
    ema20, atr = d["ema20"].to_numpy(), d["atr"].to_numpy()
    close_d = d["Close"].to_numpy()
    pos_of_day = {day: k for k, day in enumerate(days)}
    busy_until = None
    for i in sorted(ent):
        day = days[i]
        if busy_until is not None and day <= busy_until:
            continue
        b0 = starts.get(day)
        if b0 is None or b0 >= len(bars) or day_of_bar[b0] != day:
            continue
        # günlük düzeltilmiş fiyatla 5 dk ölçeği arasındaki oran
        b_end = np.searchsorted(bday, day + pd.Timedelta(days=1))
        scale = c5[b_end - 1] / close_d[i]
        lim, score = ent[i]
        lim *= scale
        from_open = abs(d["Open"].iloc[i] * scale - lim) < 1e-6 * lim
        j = b0
        if not from_open:                              # limitli giriş: fiyat seviyeye inene kadar bekle
            while j < b_end and l5[j] > lim:
                j += 1
            if j >= b_end:
                continue
            entry = min(o5[j], lim)
        else:
            entry = o5[b0]
        entry *= 1 + SLIP
        stop = entry * 0.98
        if v.get("atr"):
            stop = entry - 2 * atr[i - 1] * scale
        hi, armed, t_hit, exit_px, why = entry, False, None, None, "veri sonu"
        k = j
        first = True
        while k < len(bars):
            if not first and day_of_bar[k] != day_of_bar[k - 1]:
                # yeni gün: önceki günün kapanışında trend kontrolü
                pday = day_of_bar[k - 1]
                pi = pos_of_day.get(pday)
                if pi is not None:
                    broke = (sell[pi] if v.get("trend", "sat") == "sat" or v.get("atr") else close_d[pi] < ema20[pi])
                    if broke and not v.get("simple_tp"):
                        exit_px, why = c5[k - 1], "trend bozuldu"
                        k -= 1
                        break
            first = False
            if l5[k] <= stop:
                exit_px = min(o5[k], stop)
                why = "stop" if stop < entry else "kâr koruma"
                break
            hi = max(hi, h5[k])
            if v.get("atr"):
                pi = pos_of_day.get(day_of_bar[k])
                if pi is not None and pi > 0:
                    stop = max(stop, hi - C.TRAIL_ATR * atr[pi - 1] * scale)
                k += 1
                continue
            if v.get("simple_tp"):
                if h5[k] >= entry * (1 + v["simple_tp"]):
                    exit_px, why = max(o5[k], entry * (1 + v["simple_tp"])), "hedef"
                    break
                k += 1
                continue
            if not armed and h5[k] >= entry * 1.01:
                armed, t_hit = True, k
            if armed:
                stop = max(stop, giveback_level(entry, hi))
                if v.get("confirm", True) and t_hit is not None and k - t_hit >= 6:   # 30 dk = 6 mum
                    if c5[k] <= entry * 1.01:
                        exit_px, why = c5[k], "30 dk teyit yok"
                        break
                    t_hit = None                         # teyit edildi, artık yalnız kâr koruma
            k += 1
        if exit_px is None:
            k = len(bars) - 1
            exit_px = c5[k]
        exit_px *= 1 - SLIP
        out_day = day_of_bar[min(k, len(bars) - 1)]
        busy_until = out_day
        trades.append({"in": day, "out": out_day, "ret": exit_px / entry - 1, "why": why, "score": score,
                       "days": int(np.busday_count(day.date(), out_day.date())) + 1, "peak": hi / entry - 1, "s": sym})
    return trades


def summarize(name, trades, years):
    taken, eq = portfolio(trades)
    r = np.array([t["ret"] for t in taken]) * 100
    if not len(r):
        return f"| {name} | 0 |"
    cagr = (eq.iloc[-1] ** (1 / years) - 1) * 100 if eq.iloc[-1] > 0 else -100
    dd = ((eq / eq.cummax()) - 1).min() * 100
    return (f"| {name} | {len(taken)} | {(r > 0).mean()*100:.1f} | {r.mean():+.2f} | "
            f"{r[r > 0].mean() if (r > 0).any() else 0:+.2f} / {r[r <= 0].mean() if (r <= 0).any() else 0:+.2f} | "
            f"{np.mean([t['days'] for t in taken]):.1f} | {cagr:+.1f} | {dd:.1f} |"), taken


def main():
    cands = C.CANDIDATES + (C.ADRS if C.INCLUDE_ADRS else [])
    prices = D.download_prices(cands + [C.BENCHMARK], period="10y")
    fund = D.fundamentals([t for t in cands if t in prices])
    sh = sharia.screen(fund, {t: df.tail(756) for t, df in prices.items()})
    top, _, _ = pick_universe(fund, set(prices), sh)
    spy = prices[C.BENCHMARK]["Close"]
    end = pd.Timestamp.now(tz="UTC") - pd.Timedelta(minutes=20)
    start = end - pd.DateOffset(years=int(YEARS))
    intra = load_intraday(top, start, end)
    s0 = start.tz_convert(None).normalize()
    years = (spy.index[-1] - spy.loc[s0:].index[0]).days / 365.25
    spy_cagr = ((spy.iloc[-1] / spy.loc[s0:].iloc[0]) ** (1 / years) - 1) * 100
    data = []
    for t in top:
        if t not in prices or t not in intra or len(prices[t]) < 400:
            continue
        df = add_indicators(prices[t])
        tr_score, up = score_series(df, spy)
        total = tr_score.fillna(0) + volume_score(df) + K + season_bonus(df, spy)
        ent, sell = entries(df, compute_signals(df), total, up.fillna(False))
        ent = {i: e for i, e in ent.items() if df.index[i] >= s0}
        data.append((t, df, ent, sell, intra[t]))
    log(f"## Kademeli kâr koruma çıkışı – 5 dk veriyle ({s0:%Y-%m} – {spy.index[-1]:%Y-%m}, {years:.1f} yıl)")
    log(f"Evren: İslami filtreden geçen {len(data)} hisse · giriş: botun mevcut kuralları (değişmedi) · "
        f"portföy 5 x %20 · kayma %0.05 x 2 · SPY al-tut yıllık {spy_cagr:+.1f}%")
    log("")
    log("| Çıkış kuralı | İşlem | Kazanma % | Ort. % | Ort. kazanç / kayıp % | Ort. gün | Yıllık getiri % | Maks. düşüş % |")
    log("|---|---|---|---|---|---|---|---|")
    details = {}
    for name, v in VARIANTS.items():
        trades = [tr for t, df, ent, sell, b in data for tr in simulate(t, df, ent, sell, b, v)]
        line, taken = summarize(name, trades, years)
        log(line)
        details[name] = taken
    first = list(VARIANTS)[0]
    tk = details[first]
    if tk:
        log("")
        log(f"**Öneri – çıkış türleri:** " + ", ".join(f"{k} {v}" for k, v in
                                                   pd.Series([t['why'] for t in tk]).value_counts().items()))
        pk = np.array([t["peak"] for t in tk]) * 100
        log(f"**En yüksek kâr dağılımı:** %{(pk >= 1).mean()*100:.0f}'i +%1'e ulaştı · %{(pk >= 3).mean()*100:.0f}'i +%3 · "
            f"%{(pk >= 5).mean()*100:.0f}'i +%5 · %{(pk >= 10).mean()*100:.0f}'i +%10")
        for w in ("30 dk teyit yok", "kâr koruma", "stop", "trend bozuldu"):
            g = [t["ret"] * 100 for t in tk if t["why"] == w]
            if g:
                log(f"- {w}: {len(g)} işlem, ort. {np.mean(g):+.2f}%")
    if os.getenv("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write("\n".join(OUT) + "\n")


if __name__ == "__main__":
    main()
