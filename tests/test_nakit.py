"""Nakit yetmeyince emir verilmez ama 'para olsaydı alırdım' bildirimi gider – sahte Alpaca ile, ağsız."""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_dip as T  # noqa: E402
from engine import trader  # noqa: E402


class Poor(T.Fake):
    def account(self):
        return {"equity": "90000", "last_equity": "90000", "cash": "50", "buying_power": "50"}


def main():
    os.chdir(tempfile.mkdtemp())
    Path("komutlar").mkdir()
    Path("cache").mkdir()
    stocks = [T.stock("AAA", drop=-5), T.stock("BBB", drop=-7, close=40000)]
    summary = {"lastBar": "2026-11-13", "stocks": stocks, "picks": []}
    api = Poor()
    msgs, _ = trader.run(summary, {s["t"]: s for s in stocks}, "post", {"sent": {}}, api)
    assert not [c for c in api.calls if c[0] == "buy"], api.calls
    m = [x for x in msgs if "para olsaydı" in x]
    assert len(m) == 1 and "AAA" in m[0] and "adet @ $98.00" in m[0] and "Stop $96.04" in m[0], msgs
    assert "BBB" in m[0] and "bile pozisyon bütçesini aşıyor" in m[0], m[0]
    print(m[0])
    print("Nakit yok bildirimi testi geçti.")


if __name__ == "__main__":
    main()
