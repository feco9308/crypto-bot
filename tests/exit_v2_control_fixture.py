import json

from test_institutional_replay import Adapter
from test_replay import START, config, row

from crypto_bot.replay.engine import Variant
from crypto_bot.replay.stale import QUARANTINE


def control_snapshot(name):
    cfg = config(variants=[dict(strategy="institutional_style_v1", exit_policy=name)])
    v = Variant(cfg["variants"][0], START, QUARANTINE)
    v.research_context = Adapter()
    v.signals([row()], ["BTCUSDT"], START)
    v.execute_signals({"BTCUSDT": {START: dict(open=100, interval="1m")}}, START, False)
    for i, (o, h, lo, c) in enumerate([(100.1, 105, 100, 103), (103, 106, 97, 98)]):
        t = START + i * 60000
        v.bar(
            "BTCUSDT",
            dict(
                time=t,
                close_time=t + 59999,
                open=o,
                high=h,
                low=lo,
                close=c,
                interval="1m",
            ),
            t + 60000,
            True,
        )
    assert v.closed
    return dict(
        trades=v.closed,
        cash=str(v.ledger.account.cash_balance),
        orders=v.execution.orders,
        fills=v.execution.fills,
    )


if __name__ == "__main__":
    print(
        json.dumps(
            {p: control_snapshot(p) for p in ["baseline_v1", "adaptive_peak_lock_v1"]},
            indent=2,
            default=str,
        )
    )
