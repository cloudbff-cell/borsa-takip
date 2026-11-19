"""Orta vade (haftalar–aylar) alım kuralları – 'evre analizi' (Weinstein/Minervini çizgisi).

Kullanıcının çizdiği grafik bir hissenin dört evresini gösterir:
  1) Taban     : uzun düşüşten sonra yatay, dar bant, küçük dalgalar
  2) Yükseliş  : tabanın üstüne çıkış; her geri çekilme bir öncekinden YÜKSEK dip yapar
  3) Tepe      : yükseliş durur, fiyat yatay ve dalgalı (dağıtım)
  4) Düşüş     : sert düşüş, ardından yeniden taban
Alım yalnız 2. evrenin başında / içinde yapılır; 3. ve 4. evrede alım yok, 2. evre bozulunca satılır.

1 yıllık grafik (evre 2 teyidi):
  fiyat > 150 ve 200 günlük ortalama · 150 günlük ortalama 1 ayda yükselmiş · 50 g ort > 150 g ort
  fiyat 52 hafta dibinin en az %25 üstünde (yükseliş başlamış) ve 52 hafta zirvesinin en çok %25 altında
3 aylık grafik (giriş noktası):
  'kirilim'   : son 3 ayın tabanı (derinliği en çok OV_BASE_MAX) hacimle yukarı kırılıyor
  'geri'      : evre 2 içinde fiyat 50 günlük ortalamaya geri çekilip dünkü zirvenin üstünde kapanıyor
                (grafikteki yükselen diplerden biri)
Tepe (evre 3) koruması: fiyat 150 g ortalamanın OV_MAX_EXT'ten fazla üstündeyse alım yok (geç kalınmış).
Bu dosya hem canlı bot (engine/ortavade.py) hem geri test (tools/ortavade_test.py) tarafından kullanılır.
"""
import numpy as np
import pandas as pd

from . import config as C
from .indicators import atr


def features(df: pd.DataFrame) -> pd.DataFrame:
    d = df[["Open", "High", "Low", "Close", "Volume"]].copy()
    c, h, lo = d["Close"], d["High"], d["Low"]
    d["sma50"] = c.rolling(50).mean()
    d["sma150"] = c.rolling(150).mean()
    d["sma200"] = c.rolling(200).mean()
    d["sma150_up"] = d["sma150"] > d["sma150"].shift(21)
    d["hi252"] = h.rolling(252).max()
    d["lo252"] = lo.rolling(252).min()
    d["hi63p"] = h.shift(1).rolling(63).max()            # son 3 ayın zirvesi (bugün hariç)
    d["lo63p"] = lo.shift(1).rolling(63).min()
    d["base_depth"] = 1 - d["lo63p"] / d["hi63p"]
    d["vol50"] = d["Volume"].shift(1).rolling(50).mean()
    d["volr"] = d["Volume"] / d["vol50"]
    d["atr"] = atr(d, 14)
    d["ret63"] = c / c.shift(63) - 1
    d["ext150"] = c / d["sma150"] - 1
    d["low5"] = lo.rolling(5).min()
    d["stage2"] = ((c > d["sma150"]) & (c > d["sma200"]) & d["sma150_up"] & (d["sma50"] > d["sma150"])
                   & (c >= 1.25 * d["lo252"]) & (c >= 0.75 * d["hi252"]))
    return d


def stage(row) -> str:
    """Bilgi amaçlı evre etiketi (panel/rapor)."""
    c, s150 = row["Close"], row["sma150"]
    if pd.isna(s150):
        return "?"
    if bool(row["stage2"]):
        return "2 – yükseliş"
    up = bool(row["sma150_up"])
    if c < s150 and not up:
        return "4 – düşüş"
    if c >= 0.85 * row["hi252"]:
        return "3 – tepe/yatay"
    return "1 – taban"


def entries(d: pd.DataFrame, kind: str = None, base_max: float = None, vol_mult: float = None,
            max_ext: float = None) -> pd.Series:
    """Giriş sinyali (gün kapanışında). kind: 'kirilim' | 'geri' | 'ikisi' | 'evre2' (evre 2'ye ilk giriş günü)."""
    kind = kind or C.OV_ENTRY
    base_max = C.OV_BASE_MAX if base_max is None else base_max
    vol_mult = C.OV_VOL_MULT if vol_mult is None else vol_mult
    max_ext = C.OV_MAX_EXT if max_ext is None else max_ext
    c = d["Close"]
    s2 = d["stage2"].fillna(False).astype(bool)
    ok = s2 & (d["ext150"] <= max_ext)
    brk = ok & (c > d["hi63p"]) & (d["base_depth"] <= base_max) & (d["volr"] >= vol_mult)
    pb = ok & (d["low5"] <= d["sma50"] * 1.02) & (c > d["High"].shift(1)) & (c > d["sma50"])
    if kind == "kirilim":
        return brk
    if kind == "geri":
        return pb
    if kind == "ikisi":
        return brk | pb
    if kind == "evre2":
        return ok & ~s2.shift(1, fill_value=False)
    raise ValueError(kind)


def entry_kind(d: pd.DataFrame, i: int = -1) -> str:
    """Son gün hangi giriş türü tetiklendi ('kirilim' / 'geri' / '')."""
    if bool(entries(d, "kirilim").iloc[i]):
        return "kirilim"
    if bool(entries(d, "geri").iloc[i]):
        return "geri"
    return ""


def snapshot(df: pd.DataFrame) -> dict:
    """Canlı tarama için son gün özeti (summary.json'a yazılır)."""
    if len(df) < 260:
        return {"signal": False, "stage": "?"}
    d = features(df)
    r = d.iloc[-1]
    kind = entry_kind(d) if C.OV_ENTRY == "ikisi" else (C.OV_ENTRY if bool(entries(d).iloc[-1]) else "")

    def f(x, k=2):
        return None if pd.isna(x) else round(float(x), k)
    return {"signal": bool(kind), "kind": kind, "stage": stage(r), "close": f(r["Close"]),
            "sma50": f(r["sma50"]), "sma150": f(r["sma150"]), "hi63": f(r["hi63p"]), "lo63": f(r["lo63p"]),
            "baseDepth": f(r["base_depth"] * 100, 1), "volr": f(r["volr"]), "ext150": f(r["ext150"] * 100, 1),
            "ret63": f(r["ret63"] * 100, 1), "hi252": f(r["hi252"]), "lo252": f(r["lo252"]), "atr": f(r["atr"]),
            "limit": f(r["Close"] * (1 + C.OV_LIMIT_ABOVE))}


# ---------------------------------------------------------------------------------------------------------
# 'Düşüş sonrası yatay taban' (kullanıcının istediği): grafiğin SAĞ ucu. 1 yıl içinde güçlü yükseliş, tepe,
# sert düşüş; fiyat dipte yatay gidiyor (yeni dip yapmıyor) -> yatayda alım (ya da yataydan yukarı çıkışta).
# ---------------------------------------------------------------------------------------------------------
def features_taban(df: pd.DataFrame, side_days: int = None) -> pd.DataFrame:
    n = side_days or C.TB_SIDE_DAYS
    d = df[["Open", "High", "Low", "Close", "Volume"]].copy()
    h, lo, c = d["High"].to_numpy(), d["Low"].to_numpy(), d["Close"].to_numpy()
    N = len(c)
    hi_s = d["High"].rolling(252)
    d["peak"] = hi_s.max()
    age = hi_s.apply(lambda a: len(a) - 1 - int(np.argmax(a)), raw=True)
    d["peak_age"] = age
    lo252 = d["Low"].rolling(252).min().to_numpy()
    pre = np.full(N, np.nan)
    a_ = age.to_numpy()
    for i in range(N):
        if not np.isnan(a_[i]):
            p = i - int(a_[i])
            pre[i] = lo252[p] if p >= 0 else np.nan
    d["pre_low"] = pre                                     # tepeden önceki 1 yılın dibi (yükselişin başladığı yer)
    d["rise"] = d["peak"] / d["pre_low"] - 1               # dipten tepeye yükseliş
    d["drop"] = 1 - d["Close"] / d["peak"]                 # tepeden bugüne düşüş
    d["side_hi"] = d["High"].rolling(n).max()
    d["side_lo"] = d["Low"].rolling(n).min()
    d["side_rng"] = d["side_hi"] / d["side_lo"] - 1        # son n günün bandı
    d["no_new_low"] = d["Low"].rolling(15).min() > d["side_lo"] * 1.0001   # son 15 günde yeni dip yok
    d["pos_rng"] = (d["Close"] - d["side_lo"]) / (d["side_hi"] - d["side_lo"]).replace(0, np.nan)
    d["side_hi_p"] = d["High"].shift(1).rolling(n).max()
    d["vol50"] = d["Volume"].shift(1).rolling(50).mean()
    d["volr"] = d["Volume"] / d["vol50"]
    d["atr"] = atr(d, 14)
    d["sma150"] = d["Close"].rolling(150).mean()
    d["sma50"] = d["Close"].rolling(50).mean()
    d["ret63"] = d["Close"] / d["Close"].shift(63) - 1
    d["base"] = ((d["rise"] >= C.TB_MIN_RISE) & (d["drop"] >= C.TB_MIN_DROP) & (d["peak_age"] >= n)
                 & (d["side_rng"] <= C.TB_MAX_RANGE) & d["no_new_low"])
    return d


def entries_taban(d: pd.DataFrame, kind: str = None, max_range: float = None, min_drop: float = None,
                  min_rise: float = None) -> pd.Series:
    """'yatay': yatay taban sürerken (bandın alt yarısında) · 'kirilim': yatay bandın zirvesi yukarı kırılırken."""
    kind = kind or C.TB_ENTRY
    base = d["base"]
    if max_range is not None or min_drop is not None or min_rise is not None:
        base = ((d["rise"] >= (C.TB_MIN_RISE if min_rise is None else min_rise))
                & (d["drop"] >= (C.TB_MIN_DROP if min_drop is None else min_drop))
                & (d["peak_age"] >= C.TB_SIDE_DAYS)
                & (d["side_rng"] <= (C.TB_MAX_RANGE if max_range is None else max_range)) & d["no_new_low"])
    base = base.fillna(False).astype(bool)
    if kind == "yatay":
        return base & (d["pos_rng"] <= 0.5)
    if kind == "yatay_her":
        return base
    if kind == "kirilim":
        prev = base.shift(1, fill_value=False)
        return prev & (d["Close"] > d["side_hi_p"]) & (d["volr"] >= 1.2)
    raise ValueError(kind)


def snapshot_taban(df: pd.DataFrame) -> dict:
    if len(df) < 300:
        return {"signal": False}
    d = features_taban(df.tail(600))
    r = d.iloc[-1]
    sig = bool(entries_taban(d).iloc[-1])

    def f(x, k=2):
        return None if pd.isna(x) else round(float(x), k)
    return {"signal": sig, "kind": C.TB_ENTRY, "close": f(r["Close"]), "peak": f(r["peak"]), "preLow": f(r["pre_low"]),
            "rise": f(r["rise"] * 100, 1), "drop": f(r["drop"] * 100, 1), "peakAge": int(r["peak_age"]) if not pd.isna(r["peak_age"]) else None,
            "sideHi": f(r["side_hi"]), "sideLo": f(r["side_lo"]), "sideRng": f(r["side_rng"] * 100, 1),
            "posRng": f(r["pos_rng"] * 100, 0), "ret63": f(r["ret63"] * 100, 1), "atr": f(r["atr"]),
            "limit": f(r["Close"] * (1 + C.OV_LIMIT_ABOVE))}
