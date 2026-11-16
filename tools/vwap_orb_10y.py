"""Kullanıcı sistemi: VWAP + ORB + Hacim (+ EMA 9/20, RSI 14, 15 dk ana yön) – 10 yıl 5 dk veriyle test.

Yalnız alış (İslami kural: açığa satış yok). Her gün, her hisse için (New York saati):
  Ana yön (15 dk) : son tamamlanan 15 dk mumda kapanış > EMA20 ve EMA9 > EMA20
  Açılış aralığı  : 09:30-09:45 en yüksek (ORH) / en düşük
  Kırılım (5 dk)  : 5 dk mum ORH üstünde kapanır, hacmi önceki 20 mumun ort. >= 1.5 katı, fiyat > VWAP
  Teyit           : kırılımdan sonra fiyat ORH'yi destek olarak korur: sonraki mumlardan biri ORH'ye
                    %0.15 yaklaşır (ya da üstünde kalır) ve ORH üstünde yeşil kapanır; arada ORH altında
                    kapanış olursa kurulum bozulur. Ayrıca 5 dk EMA9 > EMA20 ve 50 <= RSI14 <= 70.
  Giriş           : teyit mumundan sonraki mumun açılışı; giriş penceresi 09:45-11:00 (ilk 90 dk)
  Stop / hedef    : stop = min(teyit mumu dibi, ORH) - %0.05; hedef = giriş + 2R; 15:50'de kapat
Risk kuralları    : işlem başı sermayenin %0.5'i (kaldıraç yok: tek pozisyon en fazla sermayenin %50'si),
                    günlük en fazla 4 işlem, günlük zarar %1.5'te dur, üst üste 2 zararda o gün dur.
Evren: İslami filtreden geçen ilk 50 (bugünkü liste – yanlılık). Kayma: giriş+çıkış %0.05 (ve %0.02).
"""
import os
import pickle
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from engine import config as C  # noqa: E402
from engine import data as D  # noqa: E402
from engine import sharia, trader  # noqa: E402
from engine.indicators import rsi  # noqa: E402
from engine.run import pick_universe  # noqa: E402

NY = "America/New_York"
CACHE = Path("cache5m")
OUT = []


def log(s=""):
    print(s, flush=True)
    OUT.append(s)


# ---------------- veri (önbellekli) ----------------

def load5m(symbols, years=10):
    """Alpaca SIP 5 dk; cache5m/<sembol>.pkl önbelleği, yalnız eksik kısım indirilir."""
    CACHE.mkdir(exist_ok=True)
    api = trader.Alpaca()
    end = pd.Timestamp.now(tz="UTC") - pd.Timedelta(minutes=20)
    first = end - pd.DateOffset(years=years)
    out = {}
    need = {}
    for s in symbols:
        p = CACHE / f"{s}.pkl"
        df = pickle.loads(p.read_bytes()) if p.exists() else None
        out[s] = df
        st = (df.index[-1].tz_convert("UTC") + pd.Timedelta(minutes=5)) if df is not None and len(df) else first
        if end - st > pd.Timedelta(hours=12):
            need[s] = st
    log(f"5 dk veri: {len(symbols) - len(need)} hisse önbellekte güncel, {len(need)} hisse indirilecek")
    t0 = time.time()
    for n, (s, st) in enumerate(need.items()):
        raw = api.bars([s], start=st.isoformat(), end=end.isoformat(), feed="sip", adjustment="all")
        rows = raw.get(s) or []
        if rows:
            df = pd.DataFrame(rows)
            df.index = pd.to_datetime(df["t"], utc=True).dt.tz_convert(NY)
            m = df.index.hour * 60 + df.index.minute
            df = df[(m >= 570) & (m < 960)][["o", "h", "l", "c", "v"]].astype("float32")
            # 'all' düzeltmesi geçmişi yeniden ölçekler: eski önbellekle birleştirirken son günü örtüştürmek yerine
            # yeni veriyi ekle (temettü farkı küçük); bölünme olursa önbellek silinip yeniden indirilmeli
            out[s] = pd.concat([out[s], df]) if out[s] is not None else df
            out[s] = out[s][~out[s].index.duplicated(keep="last")]
            (CACHE / f"{s}.pkl").write_bytes(pickle.dumps(out[s]))
        print(f"  {n+1}/{len(need)} {s}: {len(out[s]) if out[s] is not None else 0} mum ({time.time()-t0:.0f} sn)",
              flush=True)
    return {s: df for s, df in out.items() if df is not None and len(df)}


# ---------------- göstergeler ----------------

def indicators(df):
    d = df.copy()
    day = d.index.normalize()
    tp = (d["h"] + d["l"] + d["c"]) / 3
    pv = (tp * d["v"]).groupby(day).cumsum()
    d["vwap"] = pv / d["v"].groupby(day).cumsum().replace(0, np.nan)
    d["ema9"] = d["c"].ewm(span=9, adjust=False).mean()
    d["ema20"] = d["c"].ewm(span=20, adjust=False).mean()
    d["rsi"] = rsi(d["c"].astype(float), 14)
    d["vavg"] = d["v"].rolling(20).mean().shift(1)
    # 15 dk ana yön: tamamlanmış 15 dk mumlar (09:30, 09:45 ...), 5 dk mumun bitişine göre eşlenir
    q = d[["o", "h", "l", "c"]].resample("15min").agg({"o": "first", "h": "max", "l": "min", "c": "last"}).dropna()
    q["e9"] = q["c"].ewm(span=9, adjust=False).mean()
    q["e20"] = q["c"].ewm(span=20, adjust=False).mean()
    q["up15"] = (q["c"] > q["e20"]) & (q["e9"] > q["e20"])
    q["end"] = q.index + pd.Timedelta(minutes=15)
    left = pd.DataFrame({"end5": d.index + pd.Timedelta(minutes=5)}, index=d.index)
    m = pd.merge_asof(left.reset_index(), q[["end", "up15"]].reset_index(drop=True), left_on="end5", right_on="end",
                      direction="backward")
    d["up15"] = m["up15"].fillna(False).to_numpy()
    return d


# ---------------- sinyal (gün başına, hisse başına) ----------------

def day_setups(g, retest=True):
    """Bir günün 5 dk mumları -> en fazla bir kurulum dict ya da None."""
    o, h, l, c, v = (g[k].to_numpy() for k in "ohlcv")
    vw, e9, e20, r, va, up = (g[k].to_numpy() for k in ("vwap", "ema9", "ema20", "rsi", "vavg", "up15"))
    mins = ((g.index.hour * 60 + g.index.minute) - 570).to_numpy()
    if len(c) < 30 or mins[0] != 0 or mins[2] != 10:
        return None
    orh = h[:3].max()
    brk = None
    for i in range(3, len(c)):
        if mins[i] > 90:
            return None
        if brk is None:
            if c[i] > orh and va[i] > 0 and v[i] >= 1.5 * va[i] and c[i] > vw[i] and up[i]:
                brk = i
                if not retest:
                    k = i
                else:
                    continue
            else:
                continue
        else:
            if c[i] < orh:
                return None                                   # seviye korunmadı
            if not (l[i] <= orh * 1.0015 and c[i] > o[i]):
                continue
            k = i
        if not (e9[k] > e20[k] and 50 <= r[k] <= 70 and c[k] > vw[k]):
            if retest:
                continue
            return None
        if k + 1 >= len(c) or mins[k + 1] > 90:
            return None
        e = o[k + 1]
        stop = min(l[k], orh) * 0.9995
        if stop >= e:
            return None
        return {"k": k + 1, "entry": float(e), "stop": float(stop), "min": int(mins[k + 1])}
    return None


def outcome(g, s, slip):
    """Giriş k mumunun açılışı; stop/hedef 5 dk mumlarla; 15:50'de kapat."""
    o, h, l, c = (g[k].to_numpy() for k in "ohlc")
    mins = ((g.index.hour * 60 + g.index.minute) - 570).to_numpy()
    k = s["k"]
    e = s["entry"] * (1 + slip)
    R = e - s["stop"]
    tgt = e + 2 * R
    for j in range(k, len(c)):
        if mins[j] >= 380:
            return float(o[j]) * (1 - slip), j, "gün sonu", e, R
        if l[j] <= s["stop"]:
            return min(float(o[j]), s["stop"]) * (1 - slip), j, "stop", e, R
        if j > k and h[j] >= tgt:
            return max(float(o[j]), tgt) * (1 - slip), j, "hedef", e, R
    return float(c[-1]) * (1 - slip), len(c) - 1, "gün sonu", e, R


def backtest(days_idx, slip=0.0005, retest=True, rules=True):
    """days_idx: {tarih: [(sembol, gün df), ...]} -> günlük sermaye eğrisi, işlem listesi."""
    E, curve, trades = 1.0, [], []
    for d in sorted(days_idx):
        cands = []
        for s, g in days_idx[d]:
            st = day_setups(g, retest)
            if st:
                ex, j, why, e, R = outcome(g, st, slip)
                cands.append({"s": s, "t_in": st["min"], "t_out": int(((g.index[j].hour * 60 + g.index[j].minute) - 570)),
                              "e": e, "R": R, "ex": ex, "why": why})
        cands.sort(key=lambda x: x["t_in"])
        day_pl, n, losses, open_ = 0.0, 0, 0, []
        start_E = E
        for x in cands:
            # zamanı gelen pozisyonları kapat
            for p in [p for p in open_ if p["t_out"] <= x["t_in"]]:
                open_.remove(p)
                day_pl += p["pl"]
                losses = losses + 1 if p["pl"] < 0 else 0
            if rules and (n >= 4 or day_pl <= -0.015 * start_E or losses >= 2):
                break
            notional_risk = 0.005 * start_E / x["R"] * x["e"]                    # %0.5 risk
            notional = min(notional_risk, 0.5 * start_E, start_E - sum(p["notional"] for p in open_))
            if notional <= 0:
                continue
            ret = x["ex"] / x["e"] - 1
            pl = notional * ret
            open_.append({"t_out": x["t_out"], "pl": pl, "notional": notional})
            trades.append({"d": d, "s": x["s"], "r": (x["ex"] - x["e"]) / x["R"], "ret": ret, "pl_eq": pl / start_E,
                           "why": x["why"], "min": x["t_in"]})
            n += 1
        day_pl += sum(p["pl"] for p in open_)
        E += day_pl
        curve.append((pd.Timestamp(d), E))
    return pd.Series([e for _, e in curve], index=[t for t, _ in curve]), trades


def summarize(name, eq, tr, spy):
    yrs = (eq.index[-1] - eq.index[0]).days / 365.25
    cagr = (eq.iloc[-1] ** (1 / yrs) - 1) * 100
    dd = ((eq / eq.cummax()) - 1).min() * 100
    r = np.array([t["r"] for t in tr]) if tr else np.array([0.0])
    pf = r[r > 0].sum() / -r[r < 0].sum() if (r < 0).any() else float("nan")
    log(f"| {name} | {len(tr)} | {len(tr)/yrs:.0f} | {(r > 0).mean()*100:.0f} | {r.mean():+.3f} | {pf:.2f} | "
        f"**{cagr:+.1f}** | {dd:.1f} |")


def main():
    cands = C.CANDIDATES + (C.ADRS if C.INCLUDE_ADRS else [])
    prices = D.download_prices(cands + [C.BENCHMARK], period="10y")
    fund = D.fundamentals([t for t in cands if t in prices])
    sh = sharia.screen(fund, {t: df.tail(756) for t, df in prices.items()})
    top, _, _ = pick_universe(fund, set(prices), sh)
    raw = load5m(top)
    days_idx = {}
    for s, df in raw.items():
        g = indicators(df)
        for d, gd in g.groupby(g.index.date):
            days_idx.setdefault(d, []).append((s, gd))
    spy = prices[C.BENCHMARK]["Close"]
    d0, d1 = min(days_idx), max(days_idx)
    sp = spy.loc[str(d0):str(d1)]
    yrs = (sp.index[-1] - sp.index[0]).days / 365.25
    log(f"## VWAP + ORB + Hacim sistemi – {d0} – {d1} ({yrs:.1f} yıl, {len(raw)} hisse, 5 dk)")
    log(f"SPY al-tut yıllık {((sp.iloc[-1]/sp.iloc[0])**(1/yrs)-1)*100:+.1f}% · "
        f"en büyük düşüş {((sp/sp.cummax())-1).min()*100:.1f}%")
    log("")
    log("| Varyant | İşlem | Yılda | Kazanma % | Ort. R | Kâr faktörü | Yıllık getiri % | Maks. düşüş % |")
    log("|---|---|---|---|---|---|---|---|")
    eq, tr = backtest(days_idx)
    summarize("Sistem (teyitli, risk kuralları, kayma %0.05)", eq, tr, sp)
    base_eq, base_tr = eq, tr
    eq2, tr2 = backtest(days_idx, slip=0.0002)
    summarize("Aynısı, kayma %0.02", eq2, tr2, sp)
    eq3, tr3 = backtest(days_idx, retest=False)
    summarize("Teyit beklemeden (kırılımda al)", eq3, tr3, sp)
    eq4, tr4 = backtest(days_idx, rules=False)
    summarize("Risk kuralları olmadan (günlük limit/2 zarar yok)", eq4, tr4, sp)
    log("")
    log("### Yıllara göre (sistem, kayma %0.05)")
    log("| Yıl | İşlem | Kazanma % | Ort. R | Yıl getirisi % | SPY % |")
    log("|---|---|---|---|---|---|")
    for y in sorted({t["d"].year for t in base_tr}):
        t_y = [t for t in base_tr if t["d"].year == y]
        e_y = base_eq[base_eq.index.year == y]
        prev = base_eq[base_eq.index.year < y]
        y_ret = (e_y.iloc[-1] / (prev.iloc[-1] if len(prev) else 1.0) - 1) * 100
        s_y = spy[spy.index.year == y]
        s_prev = spy[spy.index.year < y]
        s_ret = (s_y.iloc[-1] / (s_prev.iloc[-1] if len(s_prev) else s_y.iloc[0]) - 1) * 100
        r = np.array([t["r"] for t in t_y])
        log(f"| {y} | {len(t_y)} | {(r > 0).mean()*100:.0f} | {r.mean():+.2f} | {y_ret:+.1f} | {s_ret:+.1f} |")
    w = pd.Series([t["why"] for t in base_tr]).value_counts()
    log("")
    log("Çıkışlar: " + ", ".join(f"{k} {v}" for k, v in w.items()))
    if os.getenv("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write("\n".join(OUT) + "\n")


if __name__ == "__main__":
    main()
