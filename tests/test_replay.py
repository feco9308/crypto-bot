"""Replay fixtures are synthetic, fixed and never call live Binance."""

import math
from copy import deepcopy
from unittest.mock import Mock

import pytest

from crypto_bot.config.settings import Settings
from crypto_bot.data.base import Candle
from crypto_bot.replay.cache import CandleCache
from crypto_bot.replay.clock import ReplayClock, at, ms
from crypto_bot.replay.config import isolated_paths, settings_for, validate
from crypto_bot.replay.engine import ReplayEngine, Variant
from crypto_bot.replay.historical_data import HistoricalData
from crypto_bot.replay.models import ReplayStore
from crypto_bot.replay.registry import EXITS, STRATEGIES
from crypto_bot.replay.universe import HistoricalUniverse
from crypto_bot.trading.config import PaperSettings
from crypto_bot.trading.domain import D, MarketContext
from crypto_bot.trading.strategy import ReferenceStrategy

START = ms("2025-01-01")


class Synthetic:
    def __init__(self, cache=None, check=None, progress=None):
        self.check = check or (lambda: None)
        self.progress = progress or (lambda **_: None)
        self.cache = Mock()
        self.cache.revision.return_value = {"synthetic": "fixed-v1"}
        self.cache.digest.return_value = "fixed-v1"
        self.hits = 0
        self.downloaded = 0
        self.missing = 0
        self.used = set()
        self.minute_calls = []

    def catalog(self):
        return ["BTCUSDT", "ETHUSDT", "ALTUSDT"]

    def hourly(self, symbol, start, end):
        rows = []
        for t in range(start, end, 3600000):
            i = (t - START) // 3600000
            price = 110 + i * 0.025 + math.sin(i / 4) * 0.7
            previous = 110 + (i - 1) * 0.025 + math.sin((i - 1) / 4) * 0.7
            volume = 100 + 20 * math.sin(i / 3)
            rows.append(
                (
                    Candle(
                        t,
                        t + 3599999,
                        previous,
                        max(price, previous) + 1,
                        min(price, previous) - 1,
                        price,
                        volume,
                    ),
                    8000000 if symbol == "BTCUSDT" else 7000000,
                )
            )
        return rows

    def minutes(self, symbol, start, end, interval="1m"):
        self.minute_calls.append((symbol, start, end, interval))
        self.hits += 1
        step = 60000 if interval == "1m" else 300000
        result = {}
        for t in range(start, end, step):
            hour = (t - START) / 3600000
            price = 110 + (hour - 1) * 0.025 + math.sin((hour - 1) / 4) * 0.7
            result[t] = dict(
                time=t,
                close_time=t + step - 1,
                open=price,
                high=price + 0.3,
                low=price - 0.3,
                close=price + 0.02,
                interval=interval,
            )
        return result


@pytest.fixture
def replay(tmp_path):
    return ReplayStore(tmp_path / "replay.db")


def config(**values):
    return validate(
        dict(
            start="2025-01-01",
            end="2025-01-03",
            candidate_symbols=["BTCUSDT", "ETHUSDT"],
            universe_size=2,
        )
        | values
    )


def row():
    return dict(
        symbol="BTCUSDT",
        total_score=82,
        components={"trend": 25},
        reasons=["synthetic score"],
        momentum={"delta_1h": 1, "delta_4h": 4, "delta_24h": 10},
        features=dict(
            price=100,
            rsi=60,
            ema50=99,
            ema200=98,
            atr=1,
            atr_pct=1,
            relative_volume=2,
            momentum=1,
            range_position=0.7,
        ),
    )


def test_clock_closed_boundary_and_no_backward():
    clock = ReplayClock(START)
    before = Candle(START - 3600000, START - 1, 1, 1, 1, 1, 1)
    current = Candle(START, START + 3599999, 1, 1, 1, 1, 1)
    assert clock.closed([before, current]) == [before]
    clock.advance(START + 3600000)
    assert clock.closed([current]) == [current]
    with pytest.raises(ValueError):
        clock.advance(START)


@pytest.mark.parametrize(
    "score,watch,has_position",
    [
        (82, True, False),
        (40, True, False),
        (82, False, False),
        (40, True, True),
        (82, True, True),
    ],
)
def test_production_strategy_parity(score, watch, has_position):
    settings = settings_for(config()["variants"][0])
    c = MarketContext(
        1,
        "BTCUSDT",
        at(START),
        D(100),
        D(score),
        D(4),
        D(60),
        D(99),
        D(98),
        D(1),
        D(2),
        "AUTO",
        watch,
    )
    expected = ReferenceStrategy(settings).evaluate(c, has_position)
    actual = (
        STRATEGIES.get("watchlist_reference_v1")
        .factory(settings)
        .evaluate(c, has_position)
    )
    assert actual == expected


def test_registries_and_immutable_config():
    c = config(
        variants=[{}, dict(exit_policy="fixed_tp", exit_parameters={"tp_pct": 2})]
    )
    before = deepcopy(c)
    STRATEGIES.get("watchlist_reference_v1")
    EXITS.get("trailing_pct")
    assert settings_for(c["variants"][0]) == PaperSettings()
    assert c == before
    with pytest.raises(ValueError):
        EXITS.get("live_sell")


def test_historical_universe_past_volume_and_future_listing():
    source = Synthetic()
    start = START - 524 * 3600000
    histories = {
        s: source.hourly(s, start, START + 3600000) for s in ("BTCUSDT", "ETHUSDT")
    }
    universe = HistoricalUniverse(histories, Settings(number_of_markets=1))
    assert universe.ranked(START)[0].symbol == "BTCUSDT"
    histories["ETHUSDT"][-1] = (histories["ETHUSDT"][-1][0], 10**20)
    universe = HistoricalUniverse(histories, Settings(number_of_markets=1))
    assert universe.ranked(START)[0].symbol == "BTCUSDT"
    assert universe.ranked(START + 3600000)[0].symbol == "ETHUSDT"
    assert len(universe.as_of("BTCUSDT", START)) == 499
    assert all(c.close_time < START for c, v in universe.as_of("BTCUSDT", START))
    assert universe.market("BTCUSDT", start + 399 * 3600000) is None


def test_warmup_never_trades_and_replay_completes(replay):
    c = config()
    run = replay.create(c)
    provider = Synthetic()
    ReplayEngine(replay, provider).run(run)
    result = replay.get(run)
    assert result["status"] == "COMPLETED"
    assert result["progress"]["processed_timestamps"] == 48
    trades, curve = replay.data(run)
    assert trades
    assert all(ms(t["entry_time"]) >= START for t in trades)
    assert curve[0]["time"] == START
    assert curve[-1]["time"] == START + 48 * 3600000
    assert result["metadata"]["historical_data_revision"] == {"synthetic": "fixed-v1"}


def test_baseline_execution_fee_slippage_and_risk():
    v = Variant(config()["variants"][0], START)
    v.signals([row()], {"BTCUSDT": "watch"}, START)
    c = dict(
        time=START,
        close_time=START + 59999,
        open=100,
        high=100.5,
        low=99.5,
        close=100,
        interval="1m",
    )
    v.execute_signals({"BTCUSDT": {START: c}}, START, False)
    p = v.ledger.portfolio.positions()[0]
    assert p.entry_price == D("100.05")
    assert p.notional <= D(200)
    assert v.ledger.account.cash_balance == D(1000) - p.notional - p.entry_fee
    assert v.execution.fills[0]["source_timeframe"] == "1m"
    assert D(v.execution.fills[0]["fee"]) == D(p.entry_fee)
    assert v.execution.fills[0]["signal_time"] == at(START).isoformat() + "Z"


@pytest.mark.parametrize(
    "policy_name,params,reason",
    [
        ("baseline_v1", {}, "ORIGINAL_STOP"),
        ("fixed_tp", {"tp_pct": "1"}, "FIXED_TP"),
        ("r_tp", {"tp_r": "0.5"}, "R_TP"),
        ("trailing_pct", {"activation_pct": "1", "distance_pct": "0.5"}, "TRAILING"),
        ("break_even", {"activation_r": ".5", "buffer_pct": "0"}, "BREAK_EVEN"),
        ("profit_lock", {"activation_pct": "1", "lock_pct": ".2"}, "PROFIT_LOCK"),
    ],
)
def test_exit_policies(policy_name, params, reason):
    v = Variant(
        config(variants=[dict(exit_policy=policy_name, exit_parameters=params)])[
            "variants"
        ][0],
        START,
    )
    v.signals([row()], {"BTCUSDT": "watch"}, START)
    c = dict(
        time=START,
        close_time=START + 59999,
        open=100,
        high=104,
        low=97,
        close=100,
        interval="1m",
    )
    v.execute_signals({"BTCUSDT": {START: c}}, START, False)
    p = v.ledger.portfolio.positions()[0]
    v.bar("BTCUSDT", c, START + 60000, True)
    assert p.status == "CLOSED"
    # When original stop and TP are both reachable, conservative original stop wins.
    assert p.exit_reason == "ORIGINAL_STOP"
    if policy_name != "baseline_v1":
        assert p.entry_data["intrabar_ambiguity"]
    assert v.closed[0]["mfe_pct"] == pytest.approx(0)


def test_intrabar_tp_conservative_and_optimistic():
    def run(conservative):
        v = Variant(
            config(
                variants=[dict(exit_policy="fixed_tp", exit_parameters={"tp_pct": "1"})]
            )["variants"][0],
            START,
        )
        v.signals([row()], {"BTCUSDT": "watch"}, START)
        c = dict(
            time=START,
            close_time=START + 59999,
            open=100,
            high=104,
            low=97,
            close=100,
            interval="1m",
        )
        v.execute_signals({"BTCUSDT": {START: c}}, START, False)
        v.bar("BTCUSDT", c, START + 60000, conservative)
        return v.closed[0]

    assert run(True)["realized_pnl"] < run(False)["realized_pnl"]
    assert run(True)["exit_reason"] == "ORIGINAL_STOP"
    assert run(False)["exit_reason"] == "FIXED_TP"


def test_missing_candle_no_fill_and_closed_gate():
    v = Variant(config()["variants"][0], START)
    v.signals([row()], {"BTCUSDT": "watch"}, START)
    v.execute_signals({}, START, False)
    assert not v.ledger.portfolio.positions()
    assert v.events[0]["event"] == "NO_FILL"
    c = dict(
        time=START,
        close_time=START + 59999,
        open=100,
        high=104,
        low=97,
        close=100,
        interval="1m",
    )
    with pytest.raises(ValueError, match="CLOSED"):
        v.bar("BTCUSDT", c, START, True)


def test_execution_5m_fallback_is_explicit():
    v = Variant(config(fallback_5m=True)["variants"][0], START)
    v.signals([row()], {"BTCUSDT": "watch"}, START)
    c = dict(
        time=START,
        close_time=START + 299999,
        open=100,
        high=101,
        low=99,
        close=100,
        interval="5m",
    )
    v.execute_signals({"BTCUSDT": {START: c}}, START, True)
    assert v.execution.fills[0]["source_timeframe"] == "5m"


@pytest.mark.parametrize(
    "limits,reason",
    [
        ({"max_open_positions": 1}, "max_positions"),
        ({"max_total_exposure_pct": "1"}, "exposure"),
        ({"daily_loss_limit_pct": ".01"}, "daily_loss"),
        ({"max_drawdown_limit_pct": ".01"}, "drawdown"),
    ],
)
def test_risk_guards_keep_exit_available(limits, reason):
    v = Variant(config(variants=[dict(risk_parameters=limits)])["variants"][0], START)
    if reason == "max_positions":
        v.signals([row()], {"BTCUSDT": "watch"}, START)
        c = dict(open=100, interval="1m")
        v.execute_signals({"BTCUSDT": {START: c}}, START, False)
        r = row() | {"symbol": "ETHUSDT"}
        v.signals([r], {"ETHUSDT": "watch"}, START)
        v.execute_signals({"BTCUSDT": {START: c}, "ETHUSDT": {START: c}}, START, False)
    elif reason == "exposure":
        v.ledger.account.cash_balance = D("10")
        v.ledger.account.day_start_equity = D("10")
        v.ledger.account.peak_equity = D("10")
        v.signals([row()], {"BTCUSDT": "watch"}, START)
        v.execute_signals(
            {"BTCUSDT": {START: dict(open=100, interval="1m")}}, START, False
        )
    else:
        v.ledger.account.cash_balance = D("999")
        v.signals([row()], {"BTCUSDT": "watch"}, START)
        v.execute_signals(
            {"BTCUSDT": {START: dict(open=100, interval="1m")}}, START, False
        )
    assert v.blocked[reason] or v.blocked["other"]


def test_config_rejects_production_path_and_unsafe_parameters(tmp_path):
    with pytest.raises(ValueError):
        isolated_paths(tmp_path, tmp_path / "replay.db")
    with pytest.raises(ValueError):
        config(variants=[dict(risk_parameters={"pyramiding": True})])
    with pytest.raises(ValueError):
        config(live=True)
    with pytest.raises(ValueError):
        config(
            variants=[
                dict(
                    exit_policy="profit_lock",
                    exit_parameters={"activation_pct": 1, "lock_pct": 2},
                )
            ]
        )


def test_archive_microseconds_and_public_get_only(tmp_path):
    cache = CandleCache(tmp_path / "cache")
    http = Mock()
    provider = HistoricalData(cache, http, pause=0)
    rows = provider.parse(
        "1735689600000000,100,101,99,100,5,1735693199999999,500,1,2,3,0\n"
    )
    assert rows[0][0] == START and rows[0][1] == START + 3599999
    with pytest.raises(ValueError):
        provider.get("https://api.binance.com/api/v3/order")
    http.get.assert_not_called()
    http.post.assert_not_called()


def test_multi_variant_reuses_one_data_pass(replay):
    run = replay.create(config(variants=[{}, dict(exit_policy="fixed_tp")]))
    provider = Synthetic()
    ReplayEngine(replay, provider).run(run)
    assert replay.get(run)["status"] == "COMPLETED"
    assert len(provider.minute_calls) == len(set(provider.minute_calls))
