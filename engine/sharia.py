"""İslami (şeriat) uygunluk taraması.

S&P 500 Shariah / SPUS yöntemine yakın, iki katmanlı tarama:
  1) Faaliyet: konvansiyonel finans (banka, sigorta, kart ağları, aracı kurumlar), alkol, tütün, kumar,
     domuz ürünleri, eğlence (film/TV/müzik), otel-kumarhane, sağlık sigortası ve savunma sanayi elenir.
  2) Finansal oranlar: toplam borç / 36 aylık ortalama piyasa değeri < %30 ve
     (nakit + faizli menkul kıymet) / 36 aylık ortalama piyasa değeri < %30.
Veri Yahoo Finance'ten gelir; resmî bir fetva değildir. Kesin karar için Zoya / Musaffa gibi
bir tarayıcıyla ya da güvendiğin bir alimle teyit et. Elle düzeltme: config.SHARIA_ALLOW / SHARIA_EXCLUDE.
"""
from . import config as C

# Yahoo Finance sektör/endüstri adları
EXCLUDED_SECTORS = {"Financial Services"}          # banka, sigorta, kart ağları (V, MA), aracı kurum, borsa/veri
EXCLUDED_INDUSTRIES = {
    "Beverages - Brewers": "alkol",
    "Beverages - Wineries & Distilleries": "alkol",
    "Tobacco": "tütün",
    "Gambling": "kumar",
    "Resorts & Casinos": "kumar",
    "Lodging": "otel (alkol)",
    "Entertainment": "film/TV/müzik",
    "Broadcasting": "TV/yayıncılık",
    "Healthcare Plans": "konvansiyonel sigorta",
    "Insurance - Life": "konvansiyonel sigorta",
    "Insurance - Diversified": "konvansiyonel sigorta",
    "Insurance - Property & Casualty": "konvansiyonel sigorta",
    "Mortgage Finance": "faizli finans",
    "Aerospace & Defense": "savunma sanayi",
}


def _avg_mcap(mcap, closes):
    """36 aylık ortalama piyasa değeri ≈ bugünkü piyasa değeri × (3 yıllık ort. fiyat / son fiyat)."""
    if closes is None or len(closes) < 60:
        return mcap
    c = closes.tail(756)
    return mcap * float(c.mean()) / float(c.iloc[-1])


def check(t: str, f: dict, closes=None) -> dict:
    """{'ok': bool, 'reason': str, 'debt': oran|None, 'cash': oran|None, 'hard': kesin eleme mi}"""
    out = {"ok": True, "reason": "", "debt": None, "cash": None, "hard": True}
    if t in C.SHARIA_ALLOW:
        out["reason"] = "elle onaylandı"
        return out
    if t in C.SHARIA_EXCLUDE:
        return {**out, "ok": False, "reason": C.SHARIA_EXCLUDE[t]}
    sec, ind = f.get("sector"), f.get("industry")
    if sec in EXCLUDED_SECTORS:
        return {**out, "ok": False, "reason": "konvansiyonel finans"}
    if ind in EXCLUDED_INDUSTRIES:
        return {**out, "ok": False, "reason": EXCLUDED_INDUSTRIES[ind]}
    mcap = f.get("marketCap")
    debt, cash = f.get("totalDebt"), f.get("totalCash")
    if not mcap or not sec or (debt is None and cash is None):
        return {**out, "ok": False, "reason": "veri yok (oranlar hesaplanamadı)", "hard": False}
    base = _avg_mcap(float(mcap), closes)
    out["debt"] = round(float(debt or 0) / base, 3)
    out["cash"] = round(float(cash or 0) / base, 3)
    if out["debt"] >= C.SHARIA_DEBT_MAX:
        return {**out, "ok": False, "reason": f"borç/piyasa değeri %{out['debt']*100:.0f} (sınır %{C.SHARIA_DEBT_MAX*100:.0f})"}
    if out["cash"] >= C.SHARIA_CASH_MAX:
        return {**out, "ok": False, "reason": f"nakit+faizli yatırım/piyasa değeri %{out['cash']*100:.0f} (sınır %{C.SHARIA_CASH_MAX*100:.0f})"}
    return out


def screen(fund: dict, prices: dict) -> dict:
    """Tüm hisseler için kontrol sonucu. Filtre kapalıysa hepsi uygun sayılır."""
    res = {}
    for t, f in fund.items():
        df = prices.get(t)
        r = check(t, f, df["Close"] if df is not None else None)
        if not C.SHARIA_FILTER:
            r = {**r, "ok": True}
        res[t] = r
    return res
