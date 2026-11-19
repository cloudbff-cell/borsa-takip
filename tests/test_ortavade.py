"""Orta vade (evre 2) stratejisi testleri – sahte Alpaca ile, ağsız."""
import os
import sys
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from engine import config as C  # noqa: E402
from engine import dip, ortavade, trader  # noqa: E402
from engine import ortavade_kural as K  # noqa: E402
from tests.test_dip import Fake  # noqa: E402

C.STRATEGY = "dip"
C.OV_ENABLED = True


def chart():
    """Kullanıcının çizdiği grafiğe benzer: taban -> yükseliş -> 3 aylık dar taban -> hacimli kırılım."""
    rng = np.random.default_rng(3)
    n1, n2, n3 = 200, 150, 63
    base1 = 50 + rng.normal(0, 0.6, n1)
    up = np.linspace(50, 90, n2) + rng.normal(0, 0.6, n2)
    base2 = 86 + 4 * np.sin(np.linspace(0, 6, n3)) + rng.normal(0, 0.4, n3)
    c = np.r_[base1, up, base2, 95.0]
    idx = pd.bdate_range(end="2026-11-13", periods=len(c))
    v = np.full(len(c), 1e6)
    v[-1] = 2.5e6
    return pd.DataFrame({"Open": c * 0.995, "High": c * 1.01, "Low": c * 0.99, "Close": c, "Volume": v}, index=idx)


def stock(t, ret63=10.0, close=100.0, top=True, ok=True, signal=True):
    return {"t": t, "isTop": top, "sharia": {"ok": ok}, "price": close, "high": close, "low": close,
            "ov": {"signal": signal, "kind": "kirilim", "stage": "2 – yükseliş", "close": close, "sma50": close * 0.95,
                   "sma150": close * 0.85, "hi63": close * 0.97, "lo63": close * 0.8, "baseDepth": 17.0, "volr": 2.0,
                   "ext150": 17.0, "ret63": ret63, "limit": round(close * 1.01, 2), "atr": 2.0}}


def main():
    os.chdir(tempfile.mkdtemp())
    Path("cache").mkdir()
    # 1) Kural: grafikteki kırılım günü sinyal, bir gün önce yok
    df = chart()
    snap = K.snapshot(df)
    assert snap["signal"] and snap["kind"] == "kirilim" and snap["stage"].startswith("2"), snap
    assert not K.snapshot(df.iloc[:-1])["signal"]
    down = df.copy()
    down["Close"] = down["Close"].to_numpy()[::-1]
    assert not K.snapshot(down)["signal"]                                    # düşüş evresinde alım yok

    # 2) Kapanış sonrası: en güçlü 4 sinyale limit + stop + kâr al; İslami/ilk 50 şartı; piyasa filtresi
    stocks = [stock("AAA", 5), stock("BBB", 30), stock("CCC", 20), stock("DDD", 10), stock("EEE", 8),
              stock("XXX", 50, ok=False), stock("YYY", 50, top=False)]
    summary = {"lastBar": "2026-11-13", "stocks": stocks, "benchmark": {"above200": True}}
    by = {s["t"]: s for s in stocks}
    api = Fake()
    state = {"sent": {}}
    alerts, _ = ortavade.run(summary, by, "post", state, api, 40000, 90000, "T")
    buys = [c for c in api.calls if c[0] == "buy"]
    assert [b[1] for b in buys] == ["BBB", "CCC", "DDD", "EEE"], buys
    b = buys[0]
    assert b[2] == int(10000 // 101) and b[3] == 101.0 and b[4] == round(101 * 0.92, 2) and b[5] == round(101 * 1.25, 2)
    assert b[6].startswith("ov-") and alerts and "Orta vade" in alerts[0]
    # aynı gün ikinci çalışma emir tekrarlamaz
    n = len(api.calls)
    ortavade.run(summary, by, "post", state, api, 40000, 90000, "T")
    assert len([c for c in api.calls[n:] if c[0] == "buy"]) == 0

    # piyasa filtresi: SPY 200 g altında -> alım yok
    api2, st2 = Fake(), {"sent": {}}
    ortavade.run(dict(summary, benchmark={"above200": False}), by, "post", st2, api2, 40000, 90000, "T")
    assert not [c for c in api2.calls if c[0] == "buy"]

    # 3) Dolum: BBB 100'den doldu -> stop/kâr al gerçek fiyata göre; dip modülü bu pozisyona dokunmaz
    api.hist = [{"id": "F1", "symbol": "BBB", "side": "buy", "client_order_id": "ov-2026-11-13-BBB", "filled_qty": "99",
                 "filled_at": "2026-11-14T14:31:00Z", "status": "filled"},
                {"id": "D0", "symbol": "BBB", "side": "buy", "client_order_id": "dip-2026-11-01-BBB", "filled_qty": "10",
                 "filled_at": "2026-11-02T14:31:00Z", "status": "filled"}]
    api.pos = [{"symbol": "BBB", "qty": "99", "avg_entry_price": "100", "current_price": "101",
                "unrealized_pl": "99", "unrealized_plpc": "0.01", "market_value": "9999"}]
    api.open = [{"id": "S1", "symbol": "BBB", "side": "sell", "type": "stop", "stop_price": "92.92"},
                {"id": "L1", "symbol": "BBB", "side": "sell", "type": "limit", "limit_price": "126.25"}]
    assert "BBB" in ortavade.ov_symbols(api) and "BBB" not in dip.dip_symbols(api)
    api.calls = []
    ortavade.run(summary, by, "intraday", state, api, 40000, 50000, "T")
    assert ("stop", "S1", 92.0) in api.calls and ("tp", "L1", 125.0) in api.calls, api.calls
    assert state["ov"]["held"]["BBB"]["entry"] == 100.0

    # 4) Eski strateji temizliği orta vade pozisyonunu kapatmaz
    api.calls = []
    trader.run({"lastBar": "2026-11-14", "stocks": [], "picks": [], "benchmark": {"above200": True}},
               {}, "intraday", {"sent": {}}, api)
    assert not [c for c in api.calls if c[0] in ("close", "close_all")], api.calls

    # 5) Süre doldu -> kapat
    state["ov"]["held"]["BBB"]["sessions"] = C.OV_MAX_DAYS
    state["sent"] = {}
    api.calls = []
    ortavade.run(dict(summary, lastBar="2026-11-15"), by, "post", state, api, 40000, 50000, "T")
    assert ("close", "BBB") in api.calls and "süre" in state["ov"]["closing"]["BBB"]
    print("Tüm orta vade testleri geçti.")


if __name__ == "__main__":
    main()
