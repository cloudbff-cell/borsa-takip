"""Teşhis: eldeki hisselerin son 15 gündeki tüm emirleri ve bacakları (işlem yapmaz)."""
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from engine.trader import Alpaca  # noqa: E402

api = Alpaca()
pos = {p["symbol"] for p in api.positions()}
after = (datetime.utcnow() - timedelta(days=15)).strftime("%Y-%m-%dT00:00:00Z")
for nested in ("true", "false"):
    print(f"===== nested={nested}")
    rows = api._r("GET", "/v2/orders", params={"status": "all", "nested": nested, "after": after, "limit": 500})
    for o in rows:
        if o["symbol"] not in pos:
            continue
        for x, ind in [(o, "")] + [(leg, "   └ ") for leg in (o.get("legs") or [])]:
            print(f"{ind}{x['symbol']} {x['side']} {x['type']} {x.get('order_class')} {x['status']} qty={x.get('qty')} "
                  f"lim={x.get('limit_price')} stop={x.get('stop_price')} cid={x.get('client_order_id')} "
                  f"upd={x.get('updated_at', '')[:16]}")
