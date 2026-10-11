"""Offline regression checks for distance diagnostics, not trading decisions."""

from copy import deepcopy

import pytest

from crypto_bot.data.base import Candle
from crypto_bot.indicators.technical import atr
from research.initial_risk_distance import HOUR, pre_features, recovery, stats


def candles():
    return [
        Candle(i * HOUR, (i + 1) * HOUR - 1, 100, 102, 98, 100, 1) for i in range(500)
    ]


def test_pre_entry_future_mutation_and_atr_parity():
    rows = candles()
    decision = 499 * HOUR
    before = pre_features(rows, decision, 100, 96)
    rows[-1] = Candle(499 * HOUR, 500 * HOUR - 1, 1, 9999, 0.001, 999, 1)
    assert pre_features(rows, decision, 100, 96) == before
    f, candidates, proof = before
    assert f["atr14_abs"] == atr(rows[:499], 14) == 4
    assert f["atr120_pct"] == 4
    assert proof["last_candle_close"] < decision
    assert candidates["CONTROL"]["abs"] == 8
    assert candidates["A"]["ratio_to_control"] == 1


def test_range_tr_pullback_denominators():
    f, _, _ = pre_features(candles(), 500 * HOUR, 101, 97)
    assert f["range_120h_pct"] == 4
    assert f["tr_p90_72h_pct"] == 4
    assert f["pullback_p75_72h_pct"] == pytest.approx(200 / 102)
    assert f["stop_over_pullback_p75_72h"] == pytest.approx(4 / (200 / 102))
    assert f["current_stop_distance_pct"] == pytest.approx(400 / 101)


def test_missing_or_discontinuous_history_rejected():
    with pytest.raises(ValueError):
        pre_features(candles()[:399], 399 * HOUR, 100, 96)
    rows = candles()
    del rows[300]
    with pytest.raises(ValueError):
        pre_features(rows, 500 * HOUR, 100, 96)


def trade():
    return dict(
        symbol="XUSDT",
        exit_time="2024-01-01T00:00:00Z",
        entry_fill_price="100",
        risk_amount="10",
        quantity="5",
    )


def provider(high=104):
    def candle(symbol, t):
        return dict(time=t, close_time=t + 59999, high=high)

    return candle


def test_recovery_uses_original_r_and_preserves_trade():
    t = trade()
    saved = deepcopy(t)
    r = recovery(t, provider())
    assert t == saved
    assert r["target_prices"]["0.75"] == 101.5
    assert r["target_prices"]["2"] == 104
    assert r["labels"] == dict(TYPE1=False, TYPE2=True, TYPE3=True)
    assert r["windows"]["1"]["first_hit_available_at"]["2"] == "2024-01-01T00:01:00"
    assert r["scope"] == "RETROSPECTIVE_DIAGNOSTIC_ONLY"


@pytest.mark.parametrize(
    "high,labels",
    [
        (99, dict(TYPE1=True, TYPE2=False, TYPE3=False)),
        (101.5, dict(TYPE1=False, TYPE2=True, TYPE3=False)),
        (102, dict(TYPE1=False, TYPE2=True, TYPE3=True)),
    ],
)
def test_recovery_independent_labels(high, labels):
    assert recovery(trade(), provider(high))["labels"] == labels


def test_missing_recovery_is_unknown_not_failed_entry():
    r = recovery(trade(), lambda symbol, t: None)
    assert all(v is None for v in r["labels"].values())
    assert r["windows"]["48"]["missing_minutes"] == 2880


def test_observed_hit_with_partial_coverage_retained():
    n = 0

    def partial(symbol, t):
        nonlocal n
        n += 1
        return provider()(symbol, t) if n == 1 else None

    r = recovery(trade(), partial)
    assert r["labels"]["TYPE3"] is True
    assert not r["windows"]["24"]["complete"]


def test_post_stop_data_cannot_modify_pre_features_or_candidates():
    before = pre_features(candles(), 500 * HOUR, 100, 96)
    recovery(trade(), provider(100000))
    assert pre_features(candles(), 500 * HOUR, 100, 96) == before


def test_statistics_distribution_accounts_for_all_samples():
    result = stats([0, 1, 2, 3, 4, 5, None])
    assert result["n"] == 6 and result["missing"] == 1
    assert sum(b["n"] for b in result["distribution"]) == 6


def test_recovery_window_end_is_exclusive():
    start = 1704067200000

    def candle(symbol, t):
        return provider(104 if t >= start + 12 * HOUR else 99)(symbol, t)

    result = recovery(trade(), candle)
    assert result["windows"]["12"]["hits"]["1"] is False
    assert result["windows"]["24"]["hits"]["1"] is True
    assert result["labels"]["TYPE2"] is False
    assert result["labels"]["TYPE3"] is True


def test_exit_candle_and_prior_minutes_are_never_requested():
    stamps = []

    def candle(symbol, t):
        stamps.append(t)
        return provider(99)(symbol, t)

    recovery(trade(), candle)
    assert min(stamps) == 1704067200000
    assert max(stamps) == 1704067200000 + 48 * HOUR - 60000
    assert len(stamps) == 2880


def test_actual_filled_buy_audit_matches_anchor_and_rejects_changes():
    from research.initial_risk_distance import verify_filled_entries

    t = dict(
        position_id=1,
        symbol="X",
        entry_time="t",
        entry_fill_price="100",
        quantity="2",
        notional="200",
        original_stop_price="98",
        risk_amount="4",
    )
    e = dict(t, anchor_position_id=1)
    assert verify_filled_entries([t], [e])["canonical_fill_mismatches"] == 0
    with pytest.raises(ValueError):
        verify_filled_entries([t], [dict(e, quantity="3")])
    with pytest.raises(ValueError):
        verify_filled_entries([t], [])
