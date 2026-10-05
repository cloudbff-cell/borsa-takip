"""Kural tabanlı al/sat sinyalleri. Kurallar Pine Script dosyasıyla aynıdır."""
import numpy as np
import pandas as pd

from . import config as C

SIGNALS = {
    # kod: (yön, Türkçe ad, açıklama)
    "B_BREAKOUT": ("AL", "Hacimli kırılım", f"Kapanış son {C.BREAKOUT_LEN} günün zirvesini {C.VOLUME_MULT}x hacimle aştı"),
    "B_PULLBACK": ("AL", "Trend içi geri çekilme", f"Yükselen trendde RSI {C.RSI_PULLBACK} üstüne döndü"),
    "B_OVERSOLD": ("AL", "Aşırı satımdan dönüş", f"RSI {C.RSI_OVERSOLD} üstüne döndü, fiyat 200 EMA üstünde"),
    "B_GOLDEN": ("AL", "Altın kesişim", "50 EMA, 200 EMA'yı yukarı kesti"),
    "S_BREAKDOWN": ("SAT", "Destek kırılımı", f"Kapanış son {C.BREAKOUT_LEN} günün dibinin altına indi"),
    "S_TREND": ("SAT", "Trend bozulması", "Fiyat 50 EMA altına indi, MACD negatif"),
    "S_OVERBOUGHT": ("SAT", "Aşırı alımdan dönüş (kâr al)", f"RSI {C.RSI_OVERBOUGHT} üstünden aşağı döndü"),
    "S_DEATH": ("SAT", "Ölüm kesişimi", "50 EMA, 200 EMA'yı aşağı kesti"),
}


def _xup(a, b):
    return (a > b) & (a.shift(1) <= b.shift(1))


def _xdn(a, b):
    return (a < b) & (a.shift(1) >= b.shift(1))


def compute_signals(df: pd.DataFrame, last_volume_scale: float = 1.0) -> pd.DataFrame:
    """df: add_indicators çıktısı. last_volume_scale: seans içindeyse son barın
    hacmini tam güne ölçeklemek için geçen seans oranı (0-1)."""
    c = df["Close"]
    vol = df["Volume"].astype(float).copy()
    if 0 < last_volume_scale < 1 and len(vol):
        vol.iloc[-1] = vol.iloc[-1] / max(last_volume_scale, 0.15)
    volr = vol / df["vol20"]
    trend_up = (c > df["ema50"]) & (df["ema50"] > df["ema200"])
    trend_dn = (c < df["ema50"]) & (df["ema50"] < df["ema200"])
    rsi = df["rsi"]
    lvl = pd.Series(C.RSI_PULLBACK, index=df.index)

    s = pd.DataFrame(index=df.index)
    s["B_BREAKOUT"] = (c > df["hh"]) & (c.shift(1) <= df["hh"].shift(1)) & (volr >= C.VOLUME_MULT) & ~trend_dn
    s["B_PULLBACK"] = trend_up & _xup(rsi, lvl)
    s["B_OVERSOLD"] = _xup(rsi, lvl * 0 + C.RSI_OVERSOLD) & (c > df["ema200"])
    s["B_GOLDEN"] = _xup(df["ema50"], df["ema200"])
    s["S_BREAKDOWN"] = (c < df["ll"]) & (c.shift(1) >= df["ll"].shift(1))
    s["S_TREND"] = _xdn(c, df["ema50"]) & (df["macd_hist"] < 0)
    s["S_OVERBOUGHT"] = _xdn(rsi, lvl * 0 + C.RSI_OVERBOUGHT)
    s["S_DEATH"] = _xdn(df["ema50"], df["ema200"])
    # 200 EMA oturmadan önceki ilk günlerde sinyal üretme
    s.iloc[: C.EMA_SLOW] = False
    s["volr"] = volr
    s["trend"] = np.where(trend_up, "Yükseliş", np.where(trend_dn, "Düşüş", "Yatay"))
    return s


def signal_events(df: pd.DataFrame, sig: pd.DataFrame):
    """Tüm geçmiş sinyalleri listeler ve FORWARD_DAYS sonraki getiriyi ekler."""
    fwd = df["Close"].shift(-C.FORWARD_DAYS) / df["Close"] - 1
    out = []
    for code in SIGNALS:
        idx = sig.index[sig[code].fillna(False).astype(bool)]
        for t in idx:
            out.append({"date": t, "code": code, "price": float(df.at[t, "Close"]),
                        "fwd": None if pd.isna(fwd.at[t]) else float(fwd.at[t])})
    out.sort(key=lambda e: e["date"])
    return out


def trade_levels(row: pd.Series, side: str) -> dict:
    c, a = float(row["Close"]), float(row["atr"])
    if side == "AL":
        stop = c - C.STOP_ATR * a
        t1, t2 = c + C.T1_ATR * a, c + C.T2_ATR * a
        res = float(row["res60"])
        return {"entry": c, "stop": stop, "t1": t1, "t2": t2,
                "resistance": res if res > c * 1.005 else None,
                "risk_pct": (c - stop) / c * 100, "rr_t2": (t2 - c) / (c - stop)}
    # SAT: elde varsa çıkış / kâr alma; yeniden alım için izlenecek seviyeler
    return {"entry": c, "reentry_support": float(row["sup60"]),
            "invalidation": c + C.STOP_ATR * a}


def technical_score(row: pd.Series, rs_excess: float) -> dict:
    c = row["Close"]
    sc = 0
    if c > row["ema50"] and row["ema50"] > row["ema200"]:
        sc += 15
    elif c > row["ema50"] or c > row["ema200"]:
        sc += 7
    r = row["rsi"]
    sc += 8 if 45 <= r <= 65 else 4 if (35 <= r < 45 or 65 < r <= 72) else 0
    sc += 7 if row["macd_hist"] > 0 else 0
    sc += 5 if c > row["ema20"] else 0
    sc += 5 if c >= row["res60"] * 0.95 else 0
    rs = float(np.clip((rs_excess + 0.15) / 0.30, 0, 1) * 20) if not np.isnan(rs_excess) else 10
    return {"tech": int(sc), "rs": round(rs, 1)}


def aggregate_stats(events_by_ticker: dict) -> dict:
    """Tüm evrende sinyal türü başına geçmiş başarı (AL: getiri>0, SAT: getiri<0)."""
    out = {}
    for code, (side, *_r) in SIGNALS.items():
        f = [e["fwd"] for evs in events_by_ticker.values() for e in evs if e["code"] == code and e["fwd"] is not None]
        if not f:
            continue
        arr = np.array(f)
        hit = (arr > 0).mean() if side == "AL" else (arr < 0).mean()
        out[code] = {"n": int(len(arr)), "hit": round(float(hit) * 100, 1),
                     "avg": round(float(arr.mean()) * 100, 2), "median": round(float(np.median(arr)) * 100, 2)}
    return out
