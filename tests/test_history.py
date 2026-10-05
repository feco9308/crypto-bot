from datetime import timedelta
from types import SimpleNamespace

from conftest import NOW

from crypto_bot.scanner.momentum import score_momentum


def test_asof_and_missing_history():
    def point(hours, score):
        return SimpleNamespace(
            timestamp=NOW - timedelta(hours=hours), total_score=score
        )

    history = [
        point(24.1, 22),
        point(4.1, 31),
        point(1.1, 43),
        point(0.9, 99),
        point(0.1, 50),
        point(-1, 100),
    ]
    m = score_momentum(54, NOW, history, [1, 4, 24], 20)
    assert m == dict(
        previous_score=50,
        score_1h_ago=43,
        delta_1h=11,
        score_4h_ago=31,
        delta_4h=23,
        score_24h_ago=22,
        delta_24h=32,
    )
    m = score_momentum(54, NOW, [point(2, 1)], [1, 4, 24], 20)
    assert m["delta_1h"] is None and m["delta_4h"] is None
    assert score_momentum(54, NOW, [], [1], 20)["previous_score"] is None
