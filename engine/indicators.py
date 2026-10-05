import numpy as np
import pandas as pd

from . import config as C


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    # Wilder RSI (TradingView ta.rsi ile aynı yöntem)
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    rs = up / dn.replace(0, np.nan)
    out = 100 - 100 / (1 + rs)
    return out.fillna(100.0).where(dn.notna())


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    pc = df["Close"].shift()
    tr = pd.concat([df["High"] - df["Low"], (df["High"] - pc).abs(), (df["Low"] - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    c = df["Close"]
    df["ema20"] = ema(c, C.EMA_FAST)
    df["ema50"] = ema(c, C.EMA_MID)
    df["ema200"] = ema(c, C.EMA_SLOW)
    df["rsi"] = rsi(c, C.RSI_LEN)
    macd = ema(c, 12) - ema(c, 26)
    df["macd"] = macd
    df["macd_sig"] = ema(macd, 9)
    df["macd_hist"] = macd - df["macd_sig"]
    df["atr"] = atr(df, C.ATR_LEN)
    # Önceki N günün en yüksek/düşüğü (bugün hariç) -> kırılım seviyeleri
    df["hh"] = df["High"].shift(1).rolling(C.BREAKOUT_LEN).max()
    df["ll"] = df["Low"].shift(1).rolling(C.BREAKOUT_LEN).min()
    df["vol20"] = df["Volume"].shift(1).rolling(20).mean()
    df["res60"] = df["High"].rolling(60).max()
    df["sup60"] = df["Low"].rolling(60).min()
    return df
