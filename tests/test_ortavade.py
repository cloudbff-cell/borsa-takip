"""Orta vade (düşüş sonrası dipte yatay + kademeli alım) testleri – sahte Alpaca ile, ağsız."""
import os
import sys
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from engine import config as C  # noqa: E402
from engine import dip, ortavade, run, trader  # noqa: E402
from engine import ortavade_kural as K  # noqa: E402
from tests.test_dip import Fake  # noqa: E402

C.STRATEGY = "dip"
C.OV_ENABLED = True
C.TB_TRANCHES = [0, 3, 6]


class Fake2(Fake):
    def oco_sell(self, s, q, tp, stop, cid):
        self.calls.append(("oco", s, q, tp, stop))


def chart(flat_days):
    """Kullanıcının grafiği: taban -> yükseliş (%80) -> tepe -> sert düşüş (%40) -> dipte flat_days seans yatay."""
    rng = np.random.default_rng(5)
    c = np.r_[50 + rng.normal(0, 0.3, 150), np.linspace(50, 90, 120), 90 + rng.normal(0, 0.5, 30),
              np.linspace(90, 54, 40)]
    c = np.r_[c, 55 + np.abs(rng.normal(0, 0.5, flat_days))]       # dip 54'ün üstünde yatay
    idx = pd.bdate_range(end="2026-11-13", periods=len(c))
    return pd.DataFrame({"Open": c, "High": c * 1.005, "Low": c * 0.995, "Close": c, "Volume": 1e6}, index=idx)


def stock(t, ret63=-20.0, close=100.0, top=True, ok=True, signal=True, bottom=95.0):
    return {"t": t, "isTop": top, "sharia": {"ok": ok}, "price": close, "high": close, "low": close,
            "ov": {"signal": signal, "close": close, "bottom": bottom, "rise": 60.0, "drop": 35.0, "near": 5.0,
                   "flat": 4, "ret63": ret63, "limit": round(close * 1.01, 2),
                   "stop": round(bottom * (1 - C.TB_STOP_BELOW), 2)}}


def main():
    os.chdir(tempfile.mkdtemp())
    Path("cache").mkdir()
    # 1) Kural: dipten sonra 2 seans yatay -> henüz yok; 3 seans -> sinyal
    s2, s3 = K.snapshot_taban(chart(2)), K.snapshot_taban(chart(3))
    assert not s2["signal"] and s3["signal"], (s2, s3)
    assert s3["rise"] >= 40 and s3["drop"] >= 25 and s3["flat"] >= 3 and s3["near"] <= 10, s3
    up = chart(3)
    up["Close"] = np.linspace(50, 150, len(up))
    up["High"], up["Low"], up["Open"] = up["Close"] * 1.005, up["Close"] * 0.995, up["Close"]
    assert not K.snapshot_taban(up)["signal"]                       # hiç düşmemiş hisse

    # 2) İlk parça: 5 pozisyon, her biri bütçe/3; İslami/ilk 50 şartı; güçlü olan önce
    stocks = [stock(t, r) for t, r in [("AAA", -5), ("BBB", -1), ("CCC", -9), ("DDD", -3), ("EEE", -7), ("FFF", -8)]]
    stocks += [stock("XXX", 9, ok=False), stock("YYY", 9, top=False)]
    summary = {"lastBar": "2026-11-13", "stocks": stocks, "benchmark": {}}
    by = {s["t"]: s for s in stocks}
    api, state = Fake2(), {"sent": {}}
    alerts, _ = ortavade.run(summary, by, "post", state, api, 50000, 90000, "T")
    buys = [c for c in api.calls if c[0] == "buy"]
    assert [b[1] for b in buys] == ["BBB", "DDD", "AAA", "EEE", "FFF"], buys
    b = buys[0]
    assert b[2] == int(50000 / 5 / 3 // 101) and b[4] == round(95 * 0.9, 2) and b[5] == round(101 * 1.3, 2), b
    assert b[6] == "ov-2026-11-13-BBB-1" and "1/3. parça" in alerts[0]

    # 3) BBB 1. parça doldu -> izleniyor; 3. seansta hâlâ yataydaysa 2. parça
    api.hist = [{"id": "F1", "symbol": "BBB", "side": "buy", "client_order_id": "ov-2026-11-13-BBB-1", "filled_qty": "33",
                 "filled_avg_price": "100", "filled_at": "2026-11-16T14:31:00Z", "status": "filled"}]
    api.pos = [{"symbol": "BBB", "qty": "33", "avg_entry_price": "100", "current_price": "100",
                "unrealized_pl": "0", "unrealized_plpc": "0", "market_value": "3300"}]
    api.open = [{"id": "S1", "symbol": "BBB", "side": "sell", "type": "stop", "stop_price": "85.5", "qty": "33"},
                {"id": "L1", "symbol": "BBB", "side": "sell", "type": "limit", "limit_price": "131.3", "qty": "33"}]
    api.calls = []
    ortavade.run(summary, by, "intraday", state, api, 50000, 50000, "T")
    h = state["ov"]["held"]["BBB"]
    assert h["parts"] == 1 and h["qty"] == 33 and h["bottom"] == 95.0
    assert ("tp", "L1", 130.0) in api.calls and not [c for c in api.calls if c[0] == "oco"], api.calls
    assert "BBB" in ortavade.ov_symbols(api) and "BBB" not in dip.dip_symbols(api)
    for d_, n_buy in [("2026-11-16", 0), ("2026-11-17", 0), ("2026-11-18", 0), ("2026-11-19", 1)]:
        api.calls = []
        api.open = [o for o in api.open if o["side"] == "sell"]
        ortavade.run(dict(summary, lastBar=d_, stocks=[by["BBB"]]), by, "post", state, api, 50000, 50000, "T")
        got = [c for c in api.calls if c[0] == "buy" and c[1] == "BBB"]
        assert len(got) == n_buy, (d_, api.calls)
    assert got[0][6].endswith("-BBB-2") and state["ov"]["pending"]["BBB"]["part"] == 2

    # 2. parça doldu: adet arttı -> parça 2, korumasız adet için OCO
    api.pos[0].update(qty="66", avg_entry_price="99")
    api.hist.insert(0, {"id": "F2", "symbol": "BBB", "side": "buy", "client_order_id": "ov-2026-11-19-BBB-2",
                        "filled_qty": "33", "filled_avg_price": "98", "filled_at": "2026-11-20T14:31:00Z", "status": "filled"})
    api.open = [o for o in api.open if o["side"] == "sell"]
    api.calls = []
    ortavade.run(dict(summary, lastBar="2026-11-20"), by, "intraday", state, api, 50000, 50000, "T")
    assert state["ov"]["held"]["BBB"]["parts"] == 2
    assert ("oco", "BBB", 33, round(99 * 1.3, 2), 85.5) in api.calls, api.calls

    # 4) Hâlâ yatayda değilse (sinyal yok) 3. parça alınmaz
    by["BBB"]["ov"]["signal"] = False
    for d_ in ("2026-11-20", "2026-11-23", "2026-11-24"):
        api.calls = []
        ortavade.run(dict(summary, lastBar=d_, stocks=[by["BBB"]]), by, "post", state, api, 50000, 50000, "T")
        assert not [c for c in api.calls if c[0] == "buy"], api.calls

    # 5) Eski strateji temizliği / dip modülü orta vade pozisyonuna dokunmaz
    api.calls = []
    trader.run({"lastBar": "2026-11-25", "stocks": [], "picks": [], "benchmark": {}}, {}, "intraday", {"sent": {}}, api)
    assert not [c for c in api.calls if c[0] in ("close", "close_all")], api.calls

    # 6) Kapanış: pozisyon gitti -> SATIŞ kaydı (stop)
    api.pos = []
    api.hist.insert(0, {"id": "X1", "symbol": "BBB", "side": "sell", "type": "stop", "client_order_id": "x",
                        "filled_qty": "66", "filled_avg_price": "85.5", "filled_at": "2026-11-26T15:00:00Z"})
    ortavade.run(dict(summary, lastBar="2026-11-26"), by, "intraday", state, api, 50000, 50000, "T")
    assert "BBB" not in state["ov"]["held"]
    assert any("dip kırıldı" in n for n in state["ov"]["notes"]["2026-11-26"])

    # 7) Gün sonu raporu hata vermeden orta vade satırlarını üretir
    rep = run.daily_report(dict(summary, ovSignals=["AAA"]), {"equity": 1e5, "dayPLusd": 0, "dayPL": 0, "cash": 5e4,
                                                              "positions": []}, state)
    assert "Orta vade" in rep
    print("Tüm orta vade testleri geçti.")


if __name__ == "__main__":
    main()
